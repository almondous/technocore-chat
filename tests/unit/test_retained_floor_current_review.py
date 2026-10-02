"""PR 800 snapshot supplement, adapted from manukyancloud/floor-snapshot.

Force lifecycle replacement after the tail has started too: tail-first opens made
the earlier floor-first interleaving alone insufficient to catch this race.
"""

import errno
import os
import time
from pathlib import Path

import pytest

import store


def _interleave_after_tail_snapshot(monkeypatch, action):
    real_reverse_lines = store.reverse_lines
    fired = []

    def race(*args, **kwargs):
        for raw in real_reverse_lines(*args, **kwargs):
            if not fired:
                fired.append(True)
                action()
            yield raw

    monkeypatch.setattr(store, "reverse_lines", race)
    return fired


@pytest.mark.parametrize("mutation", ["compact", "reap", "recreate"])
def test_retained_floor_belongs_to_held_tail_snapshot(tmp_path, monkeypatch, mutation):
    room = "review-floor"
    records = [store.append(tmp_path, room, "bot", f"old-{i}") for i in range(3)]
    path = store.room_path(tmp_path, room)
    old_generation = store.room_generation(tmp_path, room)

    def replace_snapshot():
        if mutation == "compact":
            with store._locked(path):
                store._compact(path, keep=1)
        else:
            old = time.time() - store.IDLE_SECONDS - 60
            os.utime(path, (old, old))
            (tmp_path / ".reaped").unlink(missing_ok=True)
            store._reap(tmp_path)
            assert not path.exists()
            if mutation == "recreate":
                assert store.append(tmp_path, room, "bot", "new")["seq"] == 4

    fired = _interleave_after_tail_snapshot(monkeypatch, replace_snapshot)
    view = store.read_messages(tmp_path, room)
    assert fired == [True]
    assert view["messages"] == records
    assert view["first_retained_seq"] == records[0]["seq"], view
    assert view["first_retained_ts"] == records[0]["ts"]
    assert view["generation"] == old_generation


@pytest.mark.parametrize("since", [0, 999])
@pytest.mark.parametrize("legacy", [False, True], ids=["shard", "legacy-fallback"])
def test_reaped_cursor_does_not_convert_floor_io_failure_to_zero(
    tmp_path, monkeypatch, since, legacy
):
    """The since=0 high-water path introduced in #800 must preserve #950's EIO refusal."""
    room = "reaped-cursor"
    state_path = store._seq_state_path(tmp_path, "" if legacy else room)
    original = store.orjson.dumps({room: {"floor": 100, "gen": 8}})
    state_path.write_bytes(original)
    real_open = Path.open

    def fail_state_read(path, *args, **kwargs):
        if path == state_path:
            raise OSError(errno.EIO, "temporary floor read failure")
        return real_open(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "open", fail_state_read)
        with pytest.raises(OSError) as raised:
            store.read_messages(tmp_path, room, since=since)
        assert raised.value.errno == errno.EIO
    assert state_path.read_bytes() == original
    assert not store.room_path(tmp_path, room).exists()
    view = store.read_messages(tmp_path, room, since=since)
    assert view["last_seq"] == 100
    assert view["first_retained_seq"] is None
    assert view["generation"] == 8
    assert store.read_messages(tmp_path, room)["last_seq"] == 0


def test_generation_io_failure_closes_the_room_snapshot(tmp_path, monkeypatch):
    room = "generation-failure"
    record = store.append(tmp_path, room, "bot", "kept")
    path = store.room_path(tmp_path, room)
    state_path = store._seq_state_path(tmp_path, room)
    real_open = Path.open
    handles = []

    def fail_generation_read(target, *args, **kwargs):
        if target == state_path:
            raise OSError(errno.EIO, "temporary generation read failure")
        handle = real_open(target, *args, **kwargs)
        if target == path:
            handles.append(handle)
        return handle

    with monkeypatch.context() as fault:
        fault.setattr(Path, "open", fail_generation_read)
        with pytest.raises(OSError) as raised:
            store.read_messages(tmp_path, room)
        assert raised.value.errno == errno.EIO
    assert len(handles) == 1 and handles[0].closed
    assert store.read_messages(tmp_path, room)["messages"] == [record]
