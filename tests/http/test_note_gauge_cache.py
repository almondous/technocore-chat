"""The note gauge must retain its own freshness policy inside a cached room listing."""

from copy import deepcopy
from types import SimpleNamespace

import _client
import pytest
from _client import _age

client = _client.client


@pytest.mark.parametrize("rooms_ttl,notes_ttl", [(3, 30), (60, 0)])
@pytest.mark.parametrize("writer", ["http", "store"])
def test_note_writes_refresh_the_gauge_without_rewalking_rooms(
    client, monkeypatch, rooms_ttl, notes_ttl, writer
):
    import app
    import config
    import store

    monkeypatch.setattr(app, "time", SimpleNamespace(monotonic=lambda: 120.0))
    monkeypatch.setattr(store, "_time_bucket", lambda now, ttl: int(now // ttl))
    assert client.get("/r/gauge-room/say/bot/first").status_code == 200
    room_views = []
    real = store.room_stats

    def counted(*args, **kwargs):
        view = real(*args, **kwargs)
        room_views.append(view)
        return view

    monkeypatch.setattr(store, "room_stats", counted)
    with config.override(ROOMS_CACHE_SECONDS=rooms_ttl, NOTE_STATS_CACHE_SECONDS=notes_ttl):
        before = client.get("/rooms?format=json").json()
        retained = deepcopy(room_views[0])
        seen = before["engagement"]["windowed_messages"]
        assert before["notes"]["total"] == 0 and seen > 0
        assert client.get("/r/gauge-room/say/bot/second").status_code == 200

        if writer == "http":
            assert client.get("/kv/plans/next/set/ship-it").status_code == 200
        else:
            store.note_set(config.ROOT, "plans", "next", "ship-it")

        after = client.get("/rooms?format=json").json()
        text = client.get("/rooms").text
        assert after["notes"]["total"] == 1
        assert after["notes"]["bytes"] == len(b"ship-it")
        assert after["engagement"]["windowed_note_to_message_ratio"] == round(1 / seen, 4)
        assert "# notes 1 of" in text and f"notes/msg {1 / seen:.2f}" in text
        assert after["rooms"] == before["rooms"], "room recency still uses its own cache"
        assert len(room_views) == 1, "ordinary notes must not invalidate the costly room walk"
        assert room_views[0] == retained, "rendering must not mutate a shared cached view"


def test_disabling_the_note_cache_takes_effect_inside_a_cached_listing(client, monkeypatch):
    import app
    import config
    import store

    monkeypatch.setattr(app, "time", SimpleNamespace(monotonic=lambda: 120.0))
    monkeypatch.setattr(store, "_time_bucket", lambda now, ttl: int(now // ttl))
    calls = []
    real = store.note_stats

    def counted(root):
        calls.append(root)
        return real(root)

    monkeypatch.setattr(store, "note_stats", counted)
    with config.override(ROOMS_CACHE_SECONDS=60, NOTE_STATS_CACHE_SECONDS=30):
        client.get("/rooms")
        client.get("/rooms")
        assert len(calls) == 1
        with config.override(NOTE_STATS_CACHE_SECONDS=0):
            client.get("/rooms")
            client.get("/rooms")
        assert len(calls) == 3, "zero must bypass the note cache on each room-cache hit"


def test_reaped_notes_expire_on_the_note_timer_without_rewalking_rooms(client, monkeypatch):
    import app
    import config
    import store

    clock = [120.0]
    monkeypatch.setattr(app, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(store, "_time_bucket", lambda now, ttl: int(now // ttl))
    with config.override(ROOMS_CACHE_SECONDS=60, NOTE_STATS_CACHE_SECONDS=10):
        store.note_set(config.ROOT, "plans", "old", "v")
        assert client.get("/rooms?format=json").json()["notes"]["total"] == 1
        cache = app._rooms_walk.cache_info()
        _age(store.note_path(config.ROOT, "plans", "old"), store.IDLE_SECONDS + 60)
        store._reap_pass(config.ROOT, store.time.time())
        assert store.note_stats(config.ROOT)["total"] == 0

        clock[0] = 129.0
        assert client.get("/rooms?format=json").json()["notes"]["total"] == 1
        clock[0] = 131.0
        assert client.get("/rooms?format=json").json()["notes"]["total"] == 0
        assert app._rooms_walk.cache_info().misses == cache.misses
