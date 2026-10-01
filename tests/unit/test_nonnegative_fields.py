"""The common integer reader preserves counter and lifecycle compatibility."""

import orjson
import pytest

import store


@pytest.mark.parametrize("value", [None, [], {}, "5", -1, 0, 7, 3.5, False, True])
def test_counter_and_lifecycle_field_validation_is_unchanged(tmp_path, value):
    expected = value if isinstance(value, int) and value >= 0 else 0
    (tmp_path / store.COUNTERS_FILE).write_bytes(
        orjson.dumps(dict.fromkeys(store.COUNTER_KEYS, value))
    )
    shard = store._seq_state_path(tmp_path, "gone")
    shard.write_bytes(orjson.dumps({"gone": {"floor": value, "gen": value}}))
    for actual in [
        *store.counters(tmp_path).values(),
        store.last_seq(tmp_path, "gone"),
        store.room_generation(tmp_path, "gone"),
    ]:
        assert actual == expected
        assert type(actual) is type(expected), "existing bool/int behavior must not change"


@pytest.mark.parametrize("malformed", [None, [], "bad", 7, False])
def test_malformed_counter_map_still_reads_as_zero(tmp_path, malformed):
    (tmp_path / store.COUNTERS_FILE).write_bytes(orjson.dumps(malformed))
    assert store.counters(tmp_path) == dict.fromkeys(store.COUNTER_KEYS, 0)
