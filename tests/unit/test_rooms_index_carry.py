"""PR #633: compaction must fold post-snapshot updates in append order."""

import os
import time

import pytest

import roomsindex
import store


@pytest.mark.parametrize("times", [(20, 30), (20, 20), (30, 20)])
@pytest.mark.parametrize("existing", [False, True])
def test_compaction_keeps_the_last_post_snapshot_update(tmp_path, times, existing):
    """Repeated updates, including tied/rollback mtimes, cannot select the first write."""
    base = time.time() + 100
    store.append(tmp_path, "anchor", "bot", "seed")
    if existing:
        store.append(tmp_path, "target", "bot", "seed")
        path = store.room_path(tmp_path, "target")
        os.utime(path, (base + 10, base + 10))
    roomsindex.compact(tmp_path)
    snapshot = roomsindex.collect(tmp_path)
    for stamp, text in zip(times, ("first update", "second update"), strict=True):
        store.append(tmp_path, "target", "bot", text)
        path = store.room_path(tmp_path, "target")
        os.utime(path, (base + stamp, base + stamp))
        roomsindex.observe_file(tmp_path, "target")
    expected = (path.stat().st_mtime, path.stat().st_size)
    roomsindex.compact(tmp_path, snapshot)
    assert roomsindex.read(tmp_path)["target"] == expected


def test_snapshot_wins_over_older_index_lines(tmp_path):
    store.append(tmp_path, "target", "bot", "seed")
    roomsindex.compact(tmp_path)
    path = store.room_path(tmp_path, "target")
    base = time.time() + 100
    os.utime(path, (base + 30, base + 30))
    snapshot = roomsindex.collect(tmp_path)
    roomsindex.observe(tmp_path, "target", base + 10, 1)
    roomsindex.observe(tmp_path, "target", base + 20, 2)
    roomsindex.compact(tmp_path, snapshot)
    assert roomsindex.read(tmp_path)["target"] == snapshot["target"]


def test_carry_does_not_resurrect_deleted_room(tmp_path):
    store.append(tmp_path, "anchor", "bot", "seed")
    roomsindex.compact(tmp_path)
    snapshot = roomsindex.collect(tmp_path)
    store.append(tmp_path, "target", "bot", "first")
    store.append(tmp_path, "target", "bot", "second")
    store.room_path(tmp_path, "target").unlink()
    roomsindex.compact(tmp_path, snapshot)
    assert "target" not in roomsindex.read(tmp_path)


@pytest.mark.parametrize("shrink", [False, True])
def test_equal_snapshot_timestamp_uses_current_size_in_either_direction(tmp_path, shrink):
    store.append(tmp_path, "target", "bot", "seed")
    roomsindex.compact(tmp_path)
    path = store.room_path(tmp_path, "target")
    stamp = path.stat().st_mtime
    if shrink:
        # A rotation can make a fresh snapshot smaller than the old index line.
        path.write_bytes(b"{}\n")
        os.utime(path, (stamp, stamp))
        snapshot = roomsindex.collect(tmp_path)
    else:
        snapshot = roomsindex.collect(tmp_path)
        store.append(tmp_path, "target", "bot", "new content")
        os.utime(path, (stamp, stamp))
        roomsindex.observe_file(tmp_path, "target")
    expected = (path.stat().st_mtime, path.stat().st_size)
    roomsindex.compact(tmp_path, snapshot)
    assert roomsindex.read(tmp_path)["target"] == expected


def test_latest_rejected_line_cannot_revive_an_earlier_update(tmp_path):
    store.append(tmp_path, "target", "bot", "seed")
    roomsindex.compact(tmp_path)
    path = store.room_path(tmp_path, "target")
    stamp = path.stat().st_mtime + 100
    os.utime(path, (stamp, stamp))
    snapshot = roomsindex.collect(tmp_path)
    roomsindex.observe(tmp_path, "target", stamp + 10, 100)
    roomsindex.observe(tmp_path, "target", stamp - 10, 200)
    roomsindex.compact(tmp_path, snapshot)
    assert roomsindex.read(tmp_path)["target"] == snapshot["target"]
