"""The delay label must describe every response, not a separate client knob."""

import asyncio
import json
import sys
from pathlib import Path

import httpx2 as httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bench import multi_room_reads as reads  # noqa: E402
from bench.multi_room_delay_app import DelayRoomGets  # noqa: E402


@pytest.mark.parametrize("delay", [-1, float("nan"), float("inf"), -float("inf")])
def test_wrapper_rejects_invalid_delay(delay):
    with pytest.raises(ValueError, match="finite and non-negative"):
        DelayRoomGets(None, delay)


@pytest.mark.parametrize("delay", [0, 2.5])
@pytest.mark.parametrize("status", [200, 404, 429, 500])
def test_wrapper_reports_configured_delay_for_every_room_response(monkeypatch, delay, status):
    slept, sent = [], []

    async def sleep(seconds):
        slept.append(seconds)

    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"X-Benchmark-Delay-Ms", b"999"), (b"other", b"kept")],
            }
        )
        await send({"type": "http.response.body", "body": b"content"})

    async def send(message):
        sent.append(message)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    asyncio.run(
        DelayRoomGets(app, delay)({"type": "http", "method": "GET", "path": "/r/alpha"}, None, send)
    )
    assert slept == ([delay / 1000] if delay else [])
    assert sent[0]["headers"] == [
        (b"other", b"kept"),
        (b"x-benchmark-delay-ms", str(delay).encode()),
    ]
    assert sent[1]["body"] == b"content"


@pytest.mark.parametrize(
    "scope",
    [
        {"type": "http", "method": "GET", "path": "/healthz"},
        {"type": "http", "method": "POST", "path": "/r/alpha"},
        {"type": "lifespan"},
    ],
)
def test_wrapper_leaves_other_requests_unchanged(monkeypatch, scope):
    sent = []
    message = {"type": "http.response.start", "status": 200, "headers": []}

    async def sleep(seconds):
        raise AssertionError("non-room request delayed")

    async def app(scope, receive, send):
        await send(message)

    async def send(value):
        sent.append(value)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    asyncio.run(DelayRoomGets(app, 25)(scope, None, send))
    assert sent == [message]
    assert sent[0] is message


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)


def _response(request, delay="25", status=200):
    headers = {} if delay is None else {"x-benchmark-delay-ms": delay}
    return httpx.Response(
        status,
        headers=headers,
        json={
            "room": "alpha",
            "count": 1,
            "first_seq": 1,
            "last_seq": 1,
            "generation": 1,
            "messages": [{"seq": 1}],
        },
        request=request,
    )


@pytest.mark.parametrize("reported", [None, "0", "26", "nan", "inf", "-1", "bad", "25,25"])
@pytest.mark.parametrize("status", [200, 429, 500])
def test_client_refuses_missing_malformed_or_mismatched_delay(reported, status):
    calls = []

    def handler(request):
        calls.append(request)
        return _response(request, reported, status)

    with _client(handler) as client, pytest.raises(reads.DelayProvenanceError):
        reads.fetch_room(client, "http://test", reads.RoomSpec("alpha", 0, 1), 1, 25)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "status, expected", [(200, "ok"), (429, "rate_limited"), (500, "http_error")]
)
def test_verified_responses_keep_error_isolation_and_no_retries(status, expected):
    calls = []

    def handler(request):
        calls.append(request)
        return _response(request, status=status)

    with _client(handler) as client:
        result = reads.fetch_room(client, "http://test", reads.RoomSpec("alpha", 0, 1), 1, 25)
    assert result["status"] == expected
    assert len(calls) == 1


def _argv(tmp_path, delay="25", warmups="1"):
    rooms = tmp_path / "rooms.json"
    rooms.write_text('[{"room":"alpha","since":0,"limit":1}]')
    return [
        "multi_room_reads",
        "--base-url",
        "http://test",
        "--rooms-file",
        str(rooms),
        "--output",
        str(tmp_path / "raw.jsonl"),
        "--delay-ms",
        delay,
        "--upstream-commit",
        "test",
        "--warmups",
        warmups,
        "--repetitions",
        "2",
    ]


@pytest.mark.parametrize("bad_call", [1, 2, 3, 4, 6])
def test_cli_refuses_drift_in_either_arm_and_warmups_without_overwriting(
    monkeypatch, tmp_path, bad_call
):
    calls = []

    def handler(request):
        calls.append(request)
        return _response(request, "0" if len(calls) == bad_call else "25")

    output = tmp_path / "raw.jsonl"
    output.write_text("prior results\n")
    monkeypatch.setattr(sys, "argv", _argv(tmp_path))
    monkeypatch.setattr(reads, "_client", lambda _: _client(handler))
    with pytest.raises(SystemExit) as exc:
        reads.main()
    assert exc.value.code == 4
    assert output.read_text() == "prior results\n"
    assert len(calls) == bad_call


@pytest.mark.parametrize("delay", ["0", "2.5", "25"])
def test_cli_records_verified_fixed_snapshot_run(monkeypatch, tmp_path, delay):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.params["since"] == "0"
        return _response(request, delay)

    monkeypatch.setattr(sys, "argv", _argv(tmp_path, delay))
    monkeypatch.setattr(reads, "_client", lambda _: _client(handler))
    assert reads.main() == 0
    rows = [json.loads(line) for line in (tmp_path / "raw.jsonl").read_text().splitlines()]
    assert rows[0]["schema"] == 2
    assert rows[0]["delay_ms"] == float(delay)
    assert "every received room response" in rows[0]["delay_verification"]
    assert len(rows) == 5
    assert len(calls) == 6
    assert {row["dataset_digest"] for row in rows[1:]} != {None}


@pytest.mark.parametrize("delay", ["-1", "nan", "inf", "-inf"])
def test_cli_rejects_invalid_delay_before_requests(monkeypatch, tmp_path, delay):
    monkeypatch.setattr(sys, "argv", _argv(tmp_path, delay))
    monkeypatch.setattr(reads, "_client", lambda _: pytest.fail("client created"))
    with pytest.raises(SystemExit) as exc:
        reads.main()
    assert exc.value.code == 2
    assert not (tmp_path / "raw.jsonl").exists()
