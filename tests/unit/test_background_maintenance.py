"""Response isolation and independent scheduling, using real locks and explicit barriers."""

import os
import threading
import time
from unittest.mock import patch

import pytest

import store


def test_due_maintenance_is_never_run_by_a_write(tmp_path):
    with (
        patch.object(store, "_reap_pass", side_effect=AssertionError("inline reap")),
        patch.object(store, "service_stats", side_effect=AssertionError("inline sample")),
    ):
        assert store.append(tmp_path, "p-room", "bot", "message")["seq"] == 1
        store.note_set(tmp_path, "notes", "key", "value")
    assert store.read_messages(tmp_path, "p-room")["last_seq"] == 1
    assert store.note_get(tmp_path, "notes", "key") == "value"
    assert not (tmp_path / ".reaped").exists()
    assert not (tmp_path / store.SNAPSHOTS_FILE).exists()


def test_append_returns_while_a_background_snapshot_scan_is_blocked(tmp_path):
    store.append(tmp_path, "p-room", "bot", "seed")
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    original = store.service_stats
    result = []

    def held_stats(root):
        entered.set()
        assert release.wait(10)
        return original(root)

    def write():
        result.append(store.append(tmp_path, "p-room", "bot", "durable"))
        done.set()

    with patch.object(store, "service_stats", held_stats):
        sampler = threading.Thread(target=store._snapshot, args=(tmp_path,), daemon=True)
        writer = threading.Thread(target=write, daemon=True)
        sampler.start()
        try:
            assert entered.wait(5)
            writer.start()
            assert done.wait(5), "the append response waited for the whole-store scan"
            assert sampler.is_alive(), "the barrier did not hold the background scan"
            assert result[0]["seq"] == 2
            assert store.read_messages(tmp_path, "p-room")["last_seq"] == 2
        finally:
            release.set()
            sampler.join(5)
            if writer.ident is not None:
                writer.join(5)
    assert not sampler.is_alive()


def test_a_long_reap_does_not_block_the_sampler(tmp_path):
    entered, release, sampled, stop = (threading.Event() for _ in range(4))
    calls = []

    def reap(root):
        entered.set()
        assert release.wait(10)
        calls.append("reap")

    def snapshot(root):
        sampled.set()

    reaper = threading.Thread(target=store._maintain, args=(tmp_path, stop, reap, 0.01))
    sampler = threading.Thread(target=store._maintain, args=(tmp_path, stop, snapshot, 0.01))
    reaper.start()
    try:
        assert entered.wait(5)
        sampler.start()
        assert sampled.wait(5), "the reap serialized the independently due snapshot"
        stop.set()
        sampler.join(5)
        assert not sampler.is_alive()
        assert reaper.is_alive(), "stop cannot cancel an in-flight operation"
    finally:
        stop.set()
        release.set()
        reaper.join(5)
        if sampler.ident is not None:
            sampler.join(5)
    assert calls == ["reap"], "shutdown scheduled another pass"


@pytest.mark.parametrize("operation", [store._reap, store._snapshot])
def test_an_idle_job_stops_before_its_first_pass(tmp_path, operation):
    stop = threading.Event()
    stop.set()
    with patch.object(store, "_locked", side_effect=AssertionError("work after stop")):
        store._maintain(tmp_path, stop, operation, 300)
    assert not list(tmp_path.iterdir())


def test_background_sample_flushes_local_messages_but_skips_idle(tmp_path):
    store.append(tmp_path, "p-room", "bot", "one")
    store.append(tmp_path, "p-room", "bot", "two")
    assert store._PENDING[tmp_path]["messages"] == 1
    store._snapshot(tmp_path)
    assert store.snapshots(tmp_path)[-1]["counters"]["messages"] == 2
    assert tmp_path not in store._PENDING
    marker = tmp_path / store.SNAPSHOTS_FILE
    when = time.time() - store.SNAPSHOT_EVERY - 1
    os.utime(marker, (when, when))
    store._snapshot(tmp_path)
    assert len(store.snapshots(tmp_path)) == 1
    assert store.snapshots(tmp_path)[-1]["counters"]["messages"] == 2


def test_message_arriving_after_collection_is_retained_for_the_next_sample(tmp_path):
    store.append(tmp_path, "p-room", "bot", "before")
    original = store.service_stats

    def collect_then_write(root):
        sample = original(root)
        store.append(root, "p-room", "bot", "concurrent")
        return sample

    with patch.object(store, "service_stats", collect_then_write):
        store._snapshot(tmp_path)
    assert store.snapshots(tmp_path)[-1]["counters"]["messages"] == 1
    assert store._PENDING[tmp_path]["messages"] == 1
    with patch.object(store, "SNAPSHOT_EVERY", 0):
        store._snapshot(tmp_path)
        assert store.snapshots(tmp_path)[-1]["counters"]["messages"] == 2
        count = len(store.snapshots(tmp_path))
        store._snapshot(tmp_path)
        assert len(store.snapshots(tmp_path)) == count


def test_failed_counter_flush_retains_activity_until_recovery(tmp_path):
    store.append(tmp_path, "p-room", "bot", "one")
    store._snapshot(tmp_path)
    store.append(tmp_path, "p-room", "bot", "two")
    original = store._replace

    def fail_counters(path, value):
        if path == tmp_path / store.COUNTERS_FILE:
            raise OSError("counter write unavailable")
        return original(path, value)

    with patch.object(store, "SNAPSHOT_EVERY", 0):
        with patch.object(store, "_replace", fail_counters):
            store._snapshot(tmp_path)
        # The early intent may be consumed before its failed counter commit retries.
        # That conservative sample must not consume the process's uncommitted delta.
        assert store.snapshots(tmp_path)[-1]["counters"]["messages"] == 1
        assert store._PENDING[tmp_path]["messages"] == 1
        store._snapshot(tmp_path)
        assert store.snapshots(tmp_path)[-1]["counters"]["messages"] == 2
        assert tmp_path not in store._PENDING


def _quiet_worker(root, pipe):
    store._PENDING.clear()  # a separately started worker has its own empty process bucket
    store.append(root, "p-room", "bot", "quiet-worker")
    pipe.send("pending")
    pipe.recv()
    store._snapshot(root)
    pipe.send(root not in store._PENDING)
    pipe.close()


def test_worker_losing_the_sample_still_flushes_its_pending_generation(tmp_path):
    import multiprocessing

    store.append(tmp_path, "p-room", "bot", "one")
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    worker = context.Process(target=_quiet_worker, args=(tmp_path, child))
    worker.start()
    try:
        assert parent.poll(5) and parent.recv() == "pending"
        store._snapshot(tmp_path)
        assert store.snapshots(tmp_path)[-1]["counters"]["messages"] == 1
        parent.send("flush")
        assert parent.poll(5) and parent.recv() is True
        assert store.counters(tmp_path)["messages"] == 2
        assert len(store.snapshots(tmp_path)) == 1, "a recent sample was duplicated"
        with patch.object(store, "SNAPSHOT_EVERY", 0):
            store._snapshot(tmp_path)
            assert store.snapshots(tmp_path)[-1]["counters"]["messages"] == 2
            store._snapshot(tmp_path)
            assert len(store.snapshots(tmp_path)) == 2, "no new activity means no idle sample"
    finally:
        worker.join(5)
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
        parent.close()
        child.close()
    assert worker.exitcode == 0


def test_no_messages_means_no_history_or_store_scan(tmp_path):
    with patch.object(store, "service_stats", side_effect=AssertionError("idle scan")):
        store._snapshot(tmp_path)
        store._snapshot(tmp_path)
    assert not (tmp_path / store.SNAPSHOTS_FILE).exists()


def test_failed_sample_replace_does_not_acknowledge_activity(tmp_path):
    store.append(tmp_path, "p-room", "bot", "one")
    original = store._replace

    def fail_history(path, value):
        if path == tmp_path / store.SNAPSHOTS_FILE:
            raise OSError("history unavailable")
        return original(path, value)

    with patch.object(store, "_replace", fail_history):
        store._snapshot(tmp_path)
    assert store.snapshots(tmp_path) == []
    store._snapshot(tmp_path)
    assert store.snapshots(tmp_path)[-1]["counters"]["messages"] == 1


def test_append_announces_before_publishing_its_sampling_work(tmp_path):
    entered, release = threading.Event(), threading.Event()
    announce = store._log_event
    posted = []

    def held_announcement(root, line):
        entered.set()
        assert release.wait(10)
        announce(root, line)

    def write():
        posted.append(store.append(tmp_path, "public-room", "bot", "hello"))

    with patch.object(store, "_log_event", held_announcement):
        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        try:
            assert entered.wait(5)
            assert store.read_messages(tmp_path, "public-room")["last_seq"] == 1
            store._snapshot(tmp_path)
            assert store.snapshots(tmp_path) == [], (
                "append work was acknowledged before announcement"
            )
        finally:
            release.set()
            writer.join(5)
    assert not writer.is_alive() and posted[0]["seq"] == 1
    store._snapshot(tmp_path)
    assert store.snapshots(tmp_path)[-1]["rooms"]["total"] == 2
    assert store.read_messages(tmp_path, store.EVENTS_ROOM)["last_seq"] == 1


def test_partial_lifespan_startup_stops_the_worker_already_started(tmp_path):
    import asyncio

    make_thread = threading.Thread
    workers = []

    def start_one(**kwargs):
        if workers:
            raise RuntimeError("second thread unavailable")
        worker = make_thread(**kwargs)
        workers.append(worker)
        return worker

    async def run_sync(operation, root):
        operation(root)

    async def run():
        with pytest.raises(RuntimeError, match="second thread unavailable"):
            async with store.maintenance(tmp_path, run_sync):
                pytest.fail("failed startup entered the lifespan")

    with patch.object(store.threading, "Thread", start_one):
        asyncio.run(run())
    assert len(workers) == 1
    workers[0].join(5)
    assert not workers[0].is_alive()
