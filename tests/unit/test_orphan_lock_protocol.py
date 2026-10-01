"""Independent regression coverage for orphan sidecar ownership and retry semantics."""

import fcntl
import os
from pathlib import Path

import pytest

import store


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("nb", [False, True])
@pytest.mark.parametrize("replacement", [False, True])
def test_stale_opener_retries_current_inode(tmp_path, monkeypatch, shared, nb, replacement):
    target = tmp_path / "notes" / "ns" / "key.txt"
    lock = Path(f"{target}.lock")
    opened = []
    real_open = open

    def open_then_remove(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        if Path(path) == lock:
            opened.append(handle)
            if len(opened) == 1:
                lock.unlink()
                if replacement:
                    lock.touch()
                else:
                    # An emptied directory can also vanish before the retry.
                    lock.parent.rmdir()
        return handle

    monkeypatch.setattr(store, "open", open_then_remove, raising=False)
    with store._locked(target, shared=shared, nb=nb):
        assert len(opened) == 2
        assert opened[0].closed
        assert os.path.samefile(opened[1].fileno(), lock)
        with real_open(lock, "a+b") as other:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if shared:
                fcntl.flock(other, fcntl.LOCK_SH | fcntl.LOCK_NB)
    assert all(handle.closed for handle in opened)


def test_nb_stale_opener_does_not_wait_on_replacement(tmp_path, monkeypatch):
    target = tmp_path / "key.txt"
    lock = Path(f"{target}.lock")
    opened = []
    holders = []
    real_open = open

    def open_then_replace(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        if Path(path) == lock:
            opened.append(handle)
            if len(opened) == 1:
                lock.unlink()
                holder = real_open(lock, "a+b")
                holders.append(holder)
                fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle

    monkeypatch.setattr(store, "open", open_then_replace, raising=False)
    try:
        with pytest.raises(BlockingIOError), store._locked(target, nb=True):
            pytest.fail("a stale inode must never admit the waiter")
        assert len(opened) == 2
        assert all(handle.closed for handle in opened)
    finally:
        for holder in holders:
            holder.close()


@pytest.mark.parametrize("shared", [False, True])
def test_critical_section_failure_releases_lock(tmp_path, shared):
    target = tmp_path / "key.txt"
    with pytest.raises(ValueError, match="body failed"):
        with store._locked(target, shared=shared):
            raise ValueError("body failed")
    with store._locked(target, nb=True):
        pass


@pytest.mark.parametrize("failure", ["flock", "identity"])
def test_acquisition_failure_closes_descriptor(tmp_path, monkeypatch, failure):
    target = tmp_path / "key.txt"
    opened = []
    real_open = open

    def recording_open(*args, **kwargs):
        handle = real_open(*args, **kwargs)
        opened.append(handle)
        return handle

    def fail(*args, **kwargs):
        raise PermissionError("injected acquisition failure")

    monkeypatch.setattr(store, "open", recording_open, raising=False)
    if failure == "flock":
        monkeypatch.setattr(store.fcntl, "flock", fail)
    else:
        monkeypatch.setattr(store.os.path, "samefile", fail)
    with pytest.raises(PermissionError), store._locked(target):
        pytest.fail("failed acquisition entered the critical section")
    assert opened and all(handle.closed for handle in opened)


def _parked_lock_opener(target, connection, shared):
    """A separate interpreter parks after opening, before flocking, the old inode."""
    real_open = open
    parked = False
    lock = Path(f"{target}.lock")

    def parked_open(path, *args, **kwargs):
        nonlocal parked
        handle = real_open(path, *args, **kwargs)
        if Path(path) == lock and not parked:
            parked = True
            connection.send("opened")
            assert connection.recv() == "continue"
        elif Path(path) == lock:
            connection.send("reopened")
        return handle

    pytest.MonkeyPatch().setattr(store, "open", parked_open, raising=False)
    try:
        with store._locked(target, shared=shared):
            connection.send("entered")
    except BaseException as exc:
        connection.send((type(exc).__name__, str(exc)))
        raise
    finally:
        connection.close()


@pytest.mark.parametrize("shared", [False, True])
def test_process_waiter_joins_recreated_lock(tmp_path, shared):
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    target = tmp_path / "notes" / "ns" / "key.txt"
    lock = Path(f"{target}.lock")
    with store._locked(target):
        pass
    parent, child = context.Pipe()
    process = context.Process(target=_parked_lock_opener, args=(target, child, shared))
    process.start()
    child.close()
    try:
        assert parent.poll(10), "child did not open the old sidecar"
        assert parent.recv() == "opened"
        touched = {"rooms": set(), "notes": set()}
        store._sweep_orphan_locks(tmp_path, touched)
        assert not lock.exists()
        assert touched["notes"] == {str(target.parent)}
        with store._locked(target):
            parent.send("continue")
            assert parent.poll(10), "child did not retry the detached inode"
            assert parent.recv() == "reopened"
            assert not parent.poll(0.2), "child entered while replacement lock was held"
        assert parent.poll(10), "child did not acquire the replacement after its release"
        assert parent.recv() == "entered"
        process.join(10)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(10)
        parent.close()
