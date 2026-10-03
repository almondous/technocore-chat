# Bounded multi-room read benchmark

This benchmark answers a narrow client question: when an agent must read several known
rooms, how does one persistent sequential connection compare with one persistent connection
pool and a bounded number of concurrent `GET /r/{room}` requests?

It does **not** add a server endpoint, write data over HTTP, or claim that separate room
responses form an atomic snapshot. It repeatedly uses fixed fixture cursors and records
returned generations; it is not a stateful polling client and does not advance cursors or
recover after room recreation. A room failure does not cancel other rooms, a `429` is
recorded without retry, and a possible gap
between the requested cursor and returned tail is reported.

## Reproduce

Use a fresh checkout and a disposable directory. These commands need no identity or key.

```bash
uv sync --frozen
uv run python bench/multi_room_fixture.py --root .bench-data --rooms 16 --messages 20 --text-bytes 128
CHAT_ROOT=.bench-data CHAT_FSYNC=0 CHAT_RATE_READ=10000 BENCH_DELAY_MS=25 \
  PYTHONPATH=.:src uv run uvicorn bench.multi_room_delay_app:app \
  --host 127.0.0.1 --port 8765 --workers 1 --no-access-log
```

In a second shell:

```bash
uv run python bench/multi_room_reads.py \
  --base-url http://127.0.0.1:8765 \
  --rooms-file .bench-data/benchmark-rooms.json \
  --output multi-room-raw.jsonl \
  --concurrency 4 --timeout 5 --delay-ms 25 --warmups 2 --repetitions 20 \
  --upstream-commit "$(git rev-parse HEAD)"
uv run python bench/summarize_multi_room_reads.py multi-room-raw.jsonl \
  --output multi-room-summary.json
```

The delay wrapper sleeps once immediately before every room GET reaches the production ASGI
app, then reports its configured delay in `X-Benchmark-Delay-Ms` on each room response,
including HTTP errors. `--delay-ms` is an expectation, not a server setting: the client
verifies this header on every received response in both arms, including warmups. A missing,
invalid or mismatched header aborts with exit 4 before creating or overwriting the output.
Both delay settings must be finite and non-negative; zero-delay runs are verified too.
This is local experimental provenance, not authentication of an untrusted server or proof
of wall-clock timing. Use the wrapper directly, without a caching proxy. Production routes
and headers are unchanged; the wrapper remains local-only.

That controls an application-layer delay; it does not reproduce DNS, TCP/TLS handshakes,
a reverse proxy, packet loss, or a real WAN. Both arms request the same rooms, cursors, limits,
and immutable fixture. Both reuse connections. The parallel arm is capped by `--concurrency`.

The raw JSONL contains one metadata record and one record per measured arm/repetition. It
includes wall latency, client CPU time, client RSS on Windows, body bytes, errors, gap flags,
and room-level public metadata. HTTP headers/framing and server resource usage are marked
`NOT_MEASURED`. The summary reports median, p95, min, max, and sample standard deviation.
The command exits non-zero on any measured room error or response-data mismatch.
Schema 2 records successful response-header verification. Historical schema 1 files did
not verify server delay; their labels cannot be retroactively validated by summarizing them.

Delete `.bench-data` only after confirming it is the disposable path you created. The scripts
never delete it for you.
