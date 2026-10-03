"""Local-only latency wrapper for the production ASGI app.

BENCH_DELAY_MS is applied once, immediately before each room GET reaches the app. This is
an application-layer latency proxy, not a claim about real WAN/TLS/proxy behaviour. It adds
no route and must not be deployed.
"""

from __future__ import annotations

import asyncio
import math
import os

from app import app as production_app


class DelayRoomGets:
    def __init__(self, app, delay_ms: float) -> None:
        if not math.isfinite(delay_ms) or delay_ms < 0:
            raise ValueError("BENCH_DELAY_MS must be finite and non-negative")
        self.delay_header = str(delay_ms).encode("ascii")
        self.app = app
        self.delay_seconds = delay_ms / 1000

    async def __call__(self, scope, receive, send) -> None:
        room_get = (
            scope["type"] == "http"
            and scope.get("method") == "GET"
            and scope.get("path", "").startswith("/r/")
        )
        if room_get and self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)

        async def send_with_delay(message):
            if room_get and message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"x-benchmark-delay-ms"
                ]
                message = {
                    **message,
                    "headers": headers + [(b"x-benchmark-delay-ms", self.delay_header)],
                }
            await send(message)

        await self.app(scope, receive, send_with_delay)


app = DelayRoomGets(production_app, float(os.environ.get("BENCH_DELAY_MS", "0")))
