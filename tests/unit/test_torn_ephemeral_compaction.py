"""A healed torn write must not become an age boundary at the next rotation."""

import pytest

import store


@pytest.mark.parametrize("room", ["e-torn-rotation", "ordinary-torn-rotation"])
@pytest.mark.parametrize("fragment", [b'{"seq":4,"text":"unfinished', b"\xff\xfe cut short"])
def test_append_rotation_keeps_live_records_before_a_torn_line(
    tmp_path, monkeypatch, room, fragment
):
    monkeypatch.setattr(store, "MAX_ROOM_BYTES", 4096)
    # This older large record rolls off normally. The two records after it and the next
    # append fit comfortably in the half-ring, even counting the torn fragment's bytes.
    store.append(tmp_path, room, "bot", "padding" + "x" * 3000)
    store.append(tmp_path, room, "bot", "older-one")
    store.append(tmp_path, room, "bot", "older-two")
    path = store.room_path(tmp_path, room)
    with path.open("ab") as stream:
        stream.write(fragment)  # simulated interrupted write, without its newline

    before = store.read_messages(tmp_path, room, limit=10)
    assert [record["text"] for record in before["messages"]][-2:] == ["older-one", "older-two"]
    before_size = path.stat().st_size
    newest = "after" + "y" * 1000
    appended = store.append(tmp_path, room, "bot", newest)
    assert appended["seq"] == 4
    assert path.stat().st_size < before_size, "the append must actually trigger rotation"
    assert path.stat().st_size <= store.MAX_ROOM_BYTES // 2

    after = store.read_messages(tmp_path, room, limit=10)
    assert [(record["seq"], record["text"]) for record in after["messages"]] == [
        (2, "older-one"),
        (3, "older-two"),
        (4, newest),
    ]
    following = store.append(tmp_path, room, "bot", "next")
    assert following["seq"] == 5
