"""Read-only diagnostic for #545: execute its documented loop with synthetic holes."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace


class EndOfPollsError(Exception):
    """Stop the example after one synthetic page."""


def probe(script, exported):
    checkpoint = {"generation": 1, "last_delivered_seq": 1}
    delivered, saved = [], []
    polls = exports = 0
    messages = [{"seq": seq, "from": "other", "text": "synthetic"} for seq in (2, 4)]

    def get(url, **_kwargs):
        nonlocal polls, exports
        if url.endswith("/export"):
            exports += 1
            return SimpleNamespace(
                headers={"X-Room-Generation": "1"},
                text="".join(json.dumps(message) + "\n" for message in messages),
            )
        polls += 1
        if polls > 1:
            raise EndOfPollsError
        page = [messages[-1]] if exported else messages
        return SimpleNamespace(
            json=lambda: {
                "generation": 1,
                "first_seq": page[0]["seq"],
                "last_seq": 4,
                "messages": page,
            }
        )

    error = None
    try:
        exec(
            script,
            {
                "BASE": "https://example.invalid",
                "BRIDGE_DID": "bridge-did",
                "room": "bridge",
                "get": get,
                "load_checkpoint": lambda _room: checkpoint.copy(),
                "save_checkpoint": lambda _room, value: saved.append(value.copy()),
                "deliver_to_far_side": lambda message: delivered.append(message["seq"]),
            },
        )
    except EndOfPollsError:
        pass
    except RuntimeError as exc:
        error = str(exc)
    safe = not delivered and not saved and error is not None
    return {
        "case": "export_internal_hole" if exported else "read_internal_hole",
        "delivered": delivered,
        "saved": saved,
        "export_calls": exports,
        "error": error,
        "refused_before_delivery_and_checkpoint": safe,
    }


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    document = (root / "src" / "interop.md").read_text(encoding="utf-8")
    script = compile(document.split("```python", 1)[1].split("```", 1)[0], "interop.md", "exec")
    results = [probe(script, exported=False), probe(script, exported=True)]
    print(json.dumps(results, indent=2))
    # Exit 1 is expected at c3c3170e: both cases violate the proposed fail-closed invariant.
    return 0 if all(item["refused_before_delivery_and_checkpoint"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
