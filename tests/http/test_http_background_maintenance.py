"""Every HTTP write lane stays outside whole-store maintenance."""

import threading
from unittest.mock import patch

import _client
import pytest

import config
import store

client = _client.client


@pytest.mark.parametrize("signed", [False, True], ids=["unsigned", "signed"])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_message_response_returns_while_background_aggregation_is_blocked(client, signed, method):
    root = config.ROOT
    store.append(root, "p-background", "bot", "seed")
    entered, release = threading.Event(), threading.Event()
    aggregate = store.service_stats

    def blocked(path):
        entered.set()
        assert release.wait(10)
        return aggregate(path)

    with patch.object(store, "service_stats", blocked):
        sampler = threading.Thread(target=store._snapshot, args=(root,), daemon=True)
        sampler.start()
        try:
            assert entered.wait(5)
            room, body = "p-response", "message"
            if signed:
                did, sign = _client._keypair()
                if method == "GET":
                    response = _client._say_signed(client, room, did, sign, body)
                else:
                    response = client.post(
                        f"/r/{room}",
                        json={
                            "did": did,
                            "nonce": "1",
                            "sig": sign(f"{room}|1|{body}"),
                            "text": body,
                        },
                    )
            elif method == "GET":
                response = client.get(f"/r/{room}/say/bot/{body}")
            else:
                response = client.post(f"/r/{room}", json={"from": "bot", "text": body})
            assert response.status_code == 200, response.text
            assert sampler.is_alive(), "the response did not return until aggregation completed"
            assert store.read_messages(root, room)["last_seq"] == 1
        finally:
            release.set()
            sampler.join(5)
    assert not sampler.is_alive()


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_due_global_maintenance_never_runs_in_a_note_request(client, method):
    with (
        patch.object(store, "_reap_pass", side_effect=AssertionError("inline reap")),
        patch.object(store, "service_stats", side_effect=AssertionError("inline snapshot")),
    ):
        if method == "GET":
            response = client.get("/kv/notes/key/set/value")
        else:
            response = client.post("/kv/notes/key", json={"value": "value"})
    assert response.status_code == 200, response.text
    assert store.note_get(config.ROOT, "notes", "key") == "value"
    assert not (config.ROOT / store.SNAPSHOTS_FILE).exists()


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_signed_note_gate_never_reintroduces_inline_maintenance(client, method):
    did, sign = _client._keypair()
    ns, key = "room-owners", "d-signed-note"
    with (
        patch.object(store, "_reap_pass", side_effect=AssertionError("inline reap")),
        patch.object(store, "service_stats", side_effect=AssertionError("inline snapshot")),
    ):
        if method == "GET":
            response = _client._set_signed(client, ns, key, did, sign, did)
        else:
            response = client.post(
                f"/kv/{ns}/{key}",
                json={"did": did, "nonce": "1", "sig": sign(f"{ns}|{key}|1|{did}"), "value": did},
            )
    assert response.status_code == 200, response.text
    assert store.note_get(config.ROOT, ns, key) == did
