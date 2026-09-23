"""A seq-shard form must have the same meaning before and after a cached check."""

from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("raw", "floor", "gen"),
    [
        pytest.param(b'{"kept":\t{"floor":5,"gen":2,"t":1}}', 5, 2, id="tab-after-colon"),
        pytest.param(b'{"kept":\n{"floor":5,"gen":2,"t":1}}', 5, 2, id="lf-after-colon"),
        pytest.param(b'{"kept":\r{"floor":5,"gen":2,"t":1}}', 5, 2, id="cr-after-colon"),
        pytest.param(b'{"kept"\t:{"floor":5,"gen":2,"t":1}}', 5, 2, id="tab-before-colon"),
        pytest.param(rb'{"\u006bept":{"floor":5,"gen":2,"t":1}}', 5, 2, id="escaped-room-key"),
        pytest.param(
            b'{"kept":{"floor":5,"gen":2,"t":1},"kept":{"floor":9,"gen":4,"t":2}}',
            9,
            4,
            id="duplicate-room-key-keeps-last-wins",
        ),
        pytest.param(
            b'{"kept":{"}":0,"floor":5,"gen":2,"t":1}}',
            5,
            2,
            id="brace-in-first-inner-key",
        ),
        pytest.param(
            b'{"kept":{"floor":5,"gen":2,"t":1,"}":0}}',
            5,
            2,
            id="brace-in-last-inner-key",
        ),
    ],
)
def test_a_successful_parse_cannot_admit_a_different_warm_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: bytes, floor: int, gen: int
) -> None:
    """Without any edit, cold and warm reads keep the whole parser's gen and floor.

    JSON validity does not prove literal key spelling, unique keys, or safe object-end
    delimiters. A byte form the search cannot read equivalently must keep the whole-map
    fallback. Duplicate keys exercise the existing parser's last-wins compatibility,
    not a format that writers should emit.
    """
    import store

    monkeypatch.setattr(store, "_SEQ_CHECKED", {}, raising=False)
    path = store._seq_state_path(tmp_path, "kept")
    path.write_bytes(raw)
    for attempt in range(3):
        assert store.room_generation(tmp_path, "kept") == gen, (attempt, raw)
        assert store.last_seq(tmp_path, "kept") == floor, (attempt, raw)
    assert path.read_bytes() == raw, "read-only probes must not rewrite the shard"
