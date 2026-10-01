"""Independent failure/race checks against the production store implementation."""

import asyncio
import multiprocessing
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

import store

PENDING = ".snapshot-pending"
INFLIGHT = ".snapshot-inflight"


@pytest.fixture(autouse=True)
def always_due(monkeypatch):
    monkeypatch.setattr(store, "SNAPSHOT_EVERY", 0)


def seed(root):
    store.append(root, "p-review", "bot", "first")


def test_marker_failure_does_not_commit_or_forget_counter_batch(tmp_path, monkeypatch):
    real_touch = Path.touch

    def fail_marker(path, *args, **kwargs):
        if path.name == PENDING:
            raise OSError("intent unavailable")
        return real_touch(path, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(Path, "touch", fail_marker)
        seed(tmp_path)
        assert store.read_messages(tmp_path, "p-review")["last_seq"] == 1
        assert store.counters(tmp_path)["messages"] == 0
        assert store._PENDING[tmp_path]["messages"] == 1
    store._snapshot(tmp_path)
    assert store.snapshots(tmp_path)[-1]["counters"]["messages"] == 1
    assert tmp_path not in store._PENDING


def test_failed_batch_republishes_after_another_sampler_acknowledges_intent(tmp_path):
    real_replace = store._replace

    def fail_counter(path, data, **kwargs):
        if path.name == store.COUNTERS_FILE:
            raise OSError("counter unavailable")
        return real_replace(path, data, **kwargs)

    with patch.object(store, "_replace", fail_counter):
        seed(tmp_path)
    assert (tmp_path / PENDING).exists()
    assert store._PENDING[tmp_path]["messages"] == 1

    # A different process has no access to this process's uncommitted bucket.
    # Prevent only the local flush so the real claim/collect/publish/ack still runs.
    with patch.object(store, "_bump"):
        store._snapshot(tmp_path)
    assert not (tmp_path / PENDING).exists()
    assert not (tmp_path / INFLIGHT).exists()
    assert store.snapshots(tmp_path)[-1]["counters"]["messages"] == 0

    store._snapshot(tmp_path)
    assert store.snapshots(tmp_path)[-1]["counters"]["messages"] == 1
    assert len(store.snapshots(tmp_path)) == 2
    assert tmp_path not in store._PENDING


def test_failed_snapshot_replace_keeps_old_history_and_inflight(tmp_path):
    seed(tmp_path)
    store._snapshot(tmp_path)
    original = (tmp_path / store.SNAPSHOTS_FILE).read_bytes()
    store.append(tmp_path, "p-review", "bot", "second")
    real_replace = store._replace

    def fail_history(path, data, **kwargs):
        if path.name == store.SNAPSHOTS_FILE:
            raise OSError("history unavailable")
        return real_replace(path, data, **kwargs)

    with patch.object(store, "_replace", fail_history):
        store._snapshot(tmp_path)
    assert (tmp_path / store.SNAPSHOTS_FILE).read_bytes() == original
    assert (tmp_path / INFLIGHT).exists()
    assert not (tmp_path / PENDING).exists()
    store._snapshot(tmp_path)
    assert store.snapshots(tmp_path)[-1]["counters"]["messages"] == 2
    assert not (tmp_path / INFLIGHT).exists()


def test_failed_ack_replays_at_least_once_and_then_becomes_idle(tmp_path, monkeypatch):
    seed(tmp_path)
    real_unlink = Path.unlink

    def fail_ack(path, *args, **kwargs):
        if path.name == INFLIGHT:
            raise OSError("ack unavailable")
        return real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(Path, "unlink", fail_ack)
        store._snapshot(tmp_path)
    assert len(store.snapshots(tmp_path)) == 1
    assert (tmp_path / INFLIGHT).exists()
    store._snapshot(tmp_path)
    assert len(store.snapshots(tmp_path)) == 2
    store._snapshot(tmp_path)
    assert len(store.snapshots(tmp_path)) == 2


def test_marker_and_counter_io_never_hold_pending_lock(tmp_path, monkeypatch):
    real_touch, real_replace = Path.touch, store._replace
    checked = []

    def assert_free():
        assert store._PENDING_LOCK.acquire(blocking=False), "I/O owns _PENDING_LOCK"
        store._PENDING_LOCK.release()

    def check_touch(path, *args, **kwargs):
        if path.name == PENDING:
            assert_free()
            checked.append("intent")
        return real_touch(path, *args, **kwargs)

    def check_replace(path, data, **kwargs):
        if path.name == store.COUNTERS_FILE:
            assert_free()
            checked.append("counters")
        return real_replace(path, data, **kwargs)

    monkeypatch.setattr(Path, "touch", check_touch)
    monkeypatch.setattr(store, "_replace", check_replace)
    seed(tmp_path)
    assert checked == ["intent", "counters"]


def test_snapshot_claim_waits_for_counter_commit_without_blocking_plain_append(
    tmp_path,
):
    seed(tmp_path)
    store._snapshot(tmp_path)
    store.append(tmp_path, "p-review", "bot", "second")
    entered, release, sampler_done = (threading.Event() for _ in range(3))
    real_replace = store._replace
    errors = []

    def hold_counter(path, data, **kwargs):
        if path.name == store.COUNTERS_FILE and threading.current_thread().name == "flusher":
            entered.set()
            assert release.wait(10)
        return real_replace(path, data, **kwargs)

    def flush():
        try:
            store._bump(tmp_path)
        except BaseException as error:  # noqa: BLE001 - report worker failures to the test
            errors.append(error)

    def snapshot():
        try:
            store._snapshot(tmp_path)
        except BaseException as error:  # noqa: BLE001 - report worker failures to the test
            errors.append(error)
        finally:
            sampler_done.set()

    flusher = threading.Thread(target=flush, name="flusher", daemon=True)
    sampler = threading.Thread(target=snapshot, name="sampler", daemon=True)
    with patch.object(store, "_replace", hold_counter):
        flusher.start()
        try:
            assert entered.wait(5)
            sampler.start()
            assert not sampler_done.wait(0.05)
            assert (tmp_path / PENDING).exists()
            assert not (tmp_path / INFLIGHT).exists()
            assert store.append(tmp_path, "p-review", "bot", "third")["seq"] == 3
        finally:
            release.set()
            flusher.join(5)
            sampler.join(5)
    assert not flusher.is_alive() and not sampler.is_alive()
    assert not errors
    assert store.snapshots(tmp_path)[-1]["counters"]["messages"] == 3


def test_claim_lock_covers_a_batch_started_after_samplers_local_flush(tmp_path):
    seed(tmp_path)
    history_entered, history_release, commit_entered, commit_release, claim_entered = (
        threading.Event() for _ in range(5)
    )
    real_snapshots, real_replace, real_locked = (
        store.snapshots,
        store._replace,
        store._locked,
    )
    errors = []

    def hold_history(root):
        result = real_snapshots(root)
        if threading.current_thread().name == "sampler":
            history_entered.set()
            assert history_release.wait(10)
        return result

    def hold_commit(path, data, **kwargs):
        if path.name == store.COUNTERS_FILE and threading.current_thread().name == "flusher":
            commit_entered.set()
            assert commit_release.wait(10)
        return real_replace(path, data, **kwargs)

    @contextmanager
    def observe_claim(target, **kwargs):
        if (
            target.name == store.COUNTERS_FILE
            and threading.current_thread().name == "sampler"
            and history_entered.is_set()
        ):
            claim_entered.set()
        with real_locked(target, **kwargs):
            yield

    def attempt(operation):
        try:
            operation(tmp_path)
        except BaseException as error:  # noqa: BLE001 - report worker failures to the test
            errors.append(error)

    sampler = threading.Thread(target=attempt, args=(store._snapshot,), name="sampler", daemon=True)
    flusher = threading.Thread(target=attempt, args=(store._bump,), name="flusher", daemon=True)
    with (
        patch.object(store, "snapshots", hold_history),
        patch.object(store, "_replace", hold_commit),
        patch.object(store, "_locked", observe_claim),
    ):
        sampler.start()
        try:
            assert history_entered.wait(5)
            store.append(tmp_path, "p-review", "bot", "after local flush")
            flusher.start()
            assert commit_entered.wait(5)
            history_release.set()
            assert claim_entered.wait(5), "sampler never takes the commit/claim lock"
            assert not (tmp_path / INFLIGHT).exists()
            assert not (tmp_path / store.SNAPSHOTS_FILE).exists()
            assert store.append(tmp_path, "p-review", "bot", "while claim waits")["seq"] == 3
        finally:
            history_release.set()
            commit_release.set()
            sampler.join(5)
            if flusher.ident is not None:
                flusher.join(5)
    assert not sampler.is_alive() and not flusher.is_alive() and not errors
    assert store.snapshots(tmp_path)[-1]["counters"]["messages"] == 2
    store._snapshot(tmp_path)
    assert store.snapshots(tmp_path)[-1]["counters"]["messages"] == 3


def test_racing_flush_after_collection_is_not_acknowledged(tmp_path):
    seed(tmp_path)
    real_stats = store.service_stats

    def sample_then_flush(root):
        sample = real_stats(root)
        store.append(root, "p-review", "bot", "during scan")
        store._bump(root)
        return sample

    with patch.object(store, "service_stats", sample_then_flush):
        store._snapshot(tmp_path)
    assert store.snapshots(tmp_path)[-1]["counters"]["messages"] == 1
    assert (tmp_path / PENDING).exists()
    assert not (tmp_path / INFLIGHT).exists()
    store._snapshot(tmp_path)
    assert store.snapshots(tmp_path)[-1]["counters"]["messages"] == 2
    store._snapshot(tmp_path)
    assert len(store.snapshots(tmp_path)) == 2


def test_counter_reset_and_aba_still_triggers_a_sample(tmp_path):
    seed(tmp_path)
    store._snapshot(tmp_path)
    (tmp_path / store.COUNTERS_FILE).write_bytes(b"[]")
    store.append(tmp_path, "p-review", "bot", "second")
    store._snapshot(tmp_path)
    assert store.read_messages(tmp_path, "p-review")["last_seq"] == 2
    assert len(store.snapshots(tmp_path)) == 2
    assert store.snapshots(tmp_path)[-1]["counters"]["messages"] == 1


def _crash_sampler(root, phase):
    store._PENDING.clear()
    if phase == "claim":
        with patch.object(store, "service_stats", lambda root: os._exit(73)):
            store._snapshot(root)
    else:
        real_replace = store._replace

        def publish_then_exit(path, data, **kwargs):
            real_replace(path, data, **kwargs)
            if path.name == store.SNAPSHOTS_FILE:
                os._exit(73)

        with patch.object(store, "_replace", publish_then_exit):
            store._snapshot(root)
    os._exit(74)


@pytest.mark.parametrize("phase,initial_samples", [("claim", 0), ("publish", 1)])
def test_real_process_exit_retains_work_for_replacement_worker(tmp_path, phase, initial_samples):
    seed(tmp_path)
    process = multiprocessing.get_context("fork").Process(
        target=_crash_sampler, args=(tmp_path, phase)
    )
    process.start()
    process.join(10)
    if process.is_alive():
        process.terminate()
        process.join(5)
    assert process.exitcode == 73
    assert len(store.snapshots(tmp_path)) == initial_samples
    assert (tmp_path / INFLIGHT).exists()
    store._snapshot(tmp_path)
    assert len(store.snapshots(tmp_path)) == initial_samples + 1
    assert not (tmp_path / INFLIGHT).exists()
    store._snapshot(tmp_path)
    assert len(store.snapshots(tmp_path)) == initial_samples + 1


def test_unlock_failure_cannot_requeue_a_committed_counter_batch(tmp_path):
    real_locked = store._locked

    @contextmanager
    def failed_unlock(target, **kwargs):
        with real_locked(target, **kwargs):
            yield
        if target.name == store.COUNTERS_FILE:
            raise OSError("post-commit lock close failed")

    with patch.object(store, "_locked", failed_unlock):
        seed(tmp_path)
    assert store.counters(tmp_path)["messages"] == 1
    assert store._PENDING.get(tmp_path, {}).get("messages", 0) == 0
    store._snapshot(tmp_path)
    assert store.counters(tmp_path)["messages"] == 1


def test_exceptional_lifespan_stops_jobs_and_flushes_its_captured_root(tmp_path):
    started, stops, flushed = [], [], []

    class FakeThread:
        def __init__(self, *, target, args, daemon):
            assert target is store._maintain and daemon is True
            root, stop, operation, interval = args
            started.append((root, operation, interval))
            stops.append(stop)

        def start(self):
            pass

    async def run_sync(operation, root):
        assert all(stop.is_set() for stop in stops)
        operation(root)
        flushed.append(root)

    async def run():
        with pytest.raises(RuntimeError, match="lifespan body"):
            async with store.maintenance(tmp_path, run_sync):
                seed(tmp_path)
                store.append(tmp_path, "p-review", "bot", "batched")
                raise RuntimeError("lifespan body")

    with patch.object(store.threading, "Thread", FakeThread):
        asyncio.run(run())
    assert len(started) == 2 and flushed == [tmp_path]
    assert len({id(stop) for stop in stops}) == 1
    assert store.counters(tmp_path)["messages"] == 2
