"""A failed carry read must not publish a snapshot taken before a real append."""

import errno
from pathlib import Path
from unittest.mock import patch

import roomsindex
import store


def _append_after_snapshot(root):
    store.append(root, "anchor", "bot", "seed")
    roomsindex.compact(root)
    snapshot = roomsindex.collect(root)
    store.append(root, "late", "bot", "after the snapshot")
    store.append(root, "late", "bot", "latest message")
    return snapshot


def _assert_complete_listing(root):
    expected = roomsindex.collect(root)
    assert set(expected) == {"anchor", "late", store.EVENTS_ROOM}
    assert roomsindex.read(root) == expected
    stats = store.room_stats(root, limit=10)
    assert {room["room"]: room["bytes"] for room in stats["rooms"]} == {
        room: size for room, (_, size) in expected.items()
    }
    assert stats["total"] == len(expected)
    assert stats["bytes"] == sum(size for _, size in expected.values())


def test_carry_read_error_preserves_index_until_retry(tmp_path):
    snapshot = _append_after_snapshot(tmp_path)
    path = tmp_path / roomsindex.ROOMS_INDEX_FILE
    before = path.read_bytes()
    real_read_bytes = Path.read_bytes
    failed = False

    def fail_index_read_once(target):
        nonlocal failed
        if target == path and not failed:
            failed = True
            raise OSError(errno.EIO, "transient carry read failure", str(target))
        return real_read_bytes(target)

    with patch.object(store, "_replace", wraps=store._replace) as replace:
        with patch.object(Path, "read_bytes", fail_index_read_once):
            roomsindex.compact(tmp_path, snapshot)
        assert failed
        replace.assert_not_called()
        assert path.read_bytes() == before
        _assert_complete_listing(tmp_path)

        # Retry the same stale snapshot: the still-present append records must carry.
        roomsindex.compact(tmp_path, snapshot)
        replace.assert_called_once()
    _assert_complete_listing(tmp_path)


def test_successful_carry_read_publishes_post_snapshot_append(tmp_path):
    snapshot = _append_after_snapshot(tmp_path)
    with patch.object(store, "_replace", wraps=store._replace) as replace:
        roomsindex.compact(tmp_path, snapshot)
        replace.assert_called_once()
    _assert_complete_listing(tmp_path)


def test_absent_index_can_still_be_seeded(tmp_path):
    _append_after_snapshot(tmp_path)
    path = tmp_path / roomsindex.ROOMS_INDEX_FILE
    path.unlink()
    assert not path.exists()
    with patch.object(store, "_replace", wraps=store._replace) as replace:
        roomsindex.compact(tmp_path)
        replace.assert_called_once()
    _assert_complete_listing(tmp_path)
