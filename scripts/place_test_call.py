#!/usr/bin/env python3
"""Place a single outbound test call against a running USAN API.

Usage:
    python3 scripts/place_test_call.py --elder-id <UUID> [--base-url URL] [--key KEY]

Stdlib only — runnable anywhere Python 3 exists, no project/uv needed.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request


def build_request(
    *,
    base_url: str,
    elder_id: str,
    idempotency_key: str,
    dynamic_vars: dict,
) -> urllib.request.Request:
    """Build the POST /v1/calls request (pure — no network)."""
    url = base_url.rstrip("/") + "/v1/calls"
    payload = {
        "elder_id": elder_id,
        "idempotency_key": idempotency_key,
        "dynamic_vars": dynamic_vars,
    }
    return urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Place a USAN outbound test call.")
    parser.add_argument("--elder-id", required=True, help="Elder UUID to call.")
    parser.add_argument(
        "--base-url", default="http://localhost:8000", help="API base URL."
    )
    parser.add_argument("--key", default="smoke-1", help="Idempotency key.")
    parser.add_argument(
        "--var",
        action="append",
        default=[],
        metavar="K=V",
        help="dynamic_vars entry (repeatable).",
    )
    args = parser.parse_args()

    dynamic_vars: dict[str, str] = {}
    for pair in args.var:
        k, _, v = pair.partition("=")
        dynamic_vars[k] = v

    req = build_request(
        base_url=args.base_url,
        elder_id=args.elder_id,
        idempotency_key=args.key,
        dynamic_vars=dynamic_vars,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 (trusted operator URL)
            print(resp.read().decode())
        return 0
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()}")
        return 1
    except urllib.error.URLError as e:
        print(f"request failed: {e.reason}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
