"""Current-main audit: a sweep must not turn create-if-absent into two winners."""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import store


def test_sweep_preserves_single_cas_winner(tmp_path, monkeypatch):
    # A past create died after taking its sidecar, before its atomic note replacement.
    # The aged orphan is exactly what the real reaper eventually encounters.
    path = store.note_path(tmp_path, "audit", "claim")
    with store._locked(path):
        pass
    lock = path.with_suffix(".txt.lock")
    old = time.time() - store.IDLE_SECONDS - 60
    os.utime(lock, (old, old))
    (tmp_path / ".reaped").touch()
    staged = threading.Event()
    release = threading.Event()
    replace = store._replace

    def pause_replace(target, data, fsync=False):
        if target == path and data == b"first":
            staged.set()
            assert release.wait(10), "first writer was not released"
        return replace(target, data, fsync)

    monkeypatch.setattr(store, "_replace", pause_replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(store.note_set, tmp_path, "audit", "claim", "first", expect_absent=True)
        try:
            assert staged.wait(10), "first writer did not enter note replacement"
            store._sweep_orphan_locks(tmp_path, {"rooms": set(), "notes": set()})
            second = pool.submit(
                store.note_set, tmp_path, "audit", "claim", "second", expect_absent=True
            )
            # On main the sweep unlinks the lock, so the second write finishes while
            # the first owns the original inode. On a fixed store it stays blocked.
            try:
                result = second.result(timeout=0.2)
            except TimeoutError:
                result = None
        finally:
            release.set()
        first.result(timeout=10)
        assert result is None, "both create-if-absent writes succeeded"
        with pytest.raises(store.StoreConflictError):
            second.result(timeout=10)


def test_sweep_preserves_unique_message_sequences(tmp_path, monkeypatch):
    path = store.room_path(tmp_path, "p-audit")
    with store._locked(path):
        pass
    lock = path.with_suffix(".jsonl.lock")
    old = time.time() - store.IDLE_SECONDS - 60
    os.utime(lock, (old, old))
    (tmp_path / ".reaped").touch()
    staged = threading.Event()
    release = threading.Event()
    last_seq = store.last_seq

    def pause_seq(root, room):
        seq = last_seq(root, room)
        if room == "p-audit" and not staged.is_set():
            staged.set()
            assert release.wait(10)
        return seq

    monkeypatch.setattr(store, "last_seq", pause_seq)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(store.append, tmp_path, "p-audit", "one", "first")
        try:
            assert staged.wait(10)
            store._sweep_orphan_locks(tmp_path, {"rooms": set(), "notes": set()})
            second = pool.submit(store.append, tmp_path, "p-audit", "two", "second")
            try:
                second.result(timeout=0.2)
            except TimeoutError:
                pass
        finally:
            release.set()
        records = [first.result(timeout=10), second.result(timeout=10)]
    assert sorted(record["seq"] for record in records) == [1, 2]
    assert [record["seq"] for record in store.read_messages(tmp_path, "p-audit")["messages"]] == [
        1,
        2,
    ]
