"""Ownership and reap-integration regressions for safe orphan lock collection."""

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

import store


@pytest.mark.parametrize("sub,suffix", [("rooms", ".jsonl"), ("notes", ".txt")])
@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("aged", [False, True])
def test_sweep_skips_held_lock_regardless_of_age(tmp_path, sub, suffix, shared, aged):
    target = tmp_path / sub / "bucket" / f"key{suffix}"
    lock = Path(f"{target}.lock")
    touched = {"rooms": set(), "notes": set()}
    with store._locked(target, shared=shared):
        if aged:
            old = time.time() - store.IDLE_SECONDS - 60
            os.utime(lock, (old, old))
        before = lock.stat()
        store._sweep_orphan_locks(tmp_path, touched)
        assert os.path.samestat(before, lock.stat())
        assert touched == {"rooms": set(), "notes": set()}
        with open(lock, "a+b") as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    store._sweep_orphan_locks(tmp_path, touched)
    assert not lock.exists()
    assert touched[sub] == {str(target.parent)}


def test_sweep_rechecks_data_after_acquisition(tmp_path, monkeypatch):
    target = tmp_path / "notes" / "ns" / "key.txt"
    lock = Path(f"{target}.lock")
    with store._locked(target):
        pass
    real_locked = store._locked
    calls = []

    @contextmanager
    def create_before_lock(path, *args, **kwargs):
        if path == target:
            # The unlocked existence filter has passed, but a writer completes first.
            target.write_text("a newly completed write")
            calls.append(path)
        with real_locked(path, *args, **kwargs):
            yield

    monkeypatch.setattr(store, "_locked", create_before_lock)
    touched = {"rooms": set(), "notes": set()}
    store._sweep_orphan_locks(tmp_path, touched)
    assert calls == [target]
    assert target.read_text() == "a newly completed write"
    assert lock.exists()
    assert touched == {"rooms": set(), "notes": set()}


def test_full_reap_reclaims_fresh_sidecars_and_reconciles_counts(tmp_path):
    store.append(tmp_path, "p-expired", "bot", "old room")
    store.note_set(tmp_path, "expired", "key", "old note")
    room = store.room_path(tmp_path, "p-expired")
    note = store.note_path(tmp_path, "expired", "key")
    old = time.time() - store.IDLE_SECONDS - 60
    for target in (room, note):
        os.utime(target, (old, old))
        assert Path(f"{target}.lock").stat().st_mtime > old
    (tmp_path / ".reaped").unlink(missing_ok=True)
    store._reap(tmp_path)
    for target in (room, note):
        assert not target.exists()
        assert not Path(f"{target}.lock").exists()
        assert not target.parent.exists()
    assert not (tmp_path / "notes" / "expired").exists()
    assert store._read_counts(tmp_path, name=store.NOTES_FILE) == (0, 0)
    assert store._read_counts(tmp_path, name=store.USAGE_FILE) == (0, 0)
    # Cleanly recreating the same namespace and bucket reserves exactly one each.
    store.append(tmp_path, "p-expired", "bot", "new room")
    store.note_set(tmp_path, "expired", "key", "new note")
    notes = store._read_counts(tmp_path, name=store.NOTES_FILE)
    rooms = store._read_counts(tmp_path, name=store.USAGE_FILE)
    assert notes is not None and notes[0] == 1
    assert rooms is not None and rooms[0] == 1


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires inherited POSIX flock descriptor")
def test_context_exit_unlocks_descriptor_inherited_by_child(tmp_path):
    read_fd, write_fd = os.pipe()
    child_pid = None
    target = tmp_path / "key.txt"
    try:
        with store._locked(target):
            child_pid = os.fork()
            if child_pid == 0:
                os.close(write_fd)
                os.read(read_fd, 1)  # Keep inherited sidecar open until parent has probed.
                os._exit(0)
        with store._locked(target, nb=True):
            pass
    finally:
        os.close(write_fd)
        os.close(read_fd)
        if child_pid:
            _, status = os.waitpid(child_pid, 0)
            assert os.waitstatus_to_exitcode(status) == 0
