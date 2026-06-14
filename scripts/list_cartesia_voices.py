#!/usr/bin/env python3
"""List the live Cartesia voice library and emit paste-ready VoiceSpec entries.

The voice catalog (``apps/api/src/usan_api/schemas/voice_catalog.py``) is a
hand-curated allow-list of *real* Cartesia voice ids. Never hand-type a UUID into
it — a wrong id ships a broken voice into live calls. Run this against your
Cartesia account to get REAL ids and to verify the ones already in the catalog
still resolve.

Stdlib only — runnable anywhere Python 3 exists, no project/uv needed:

    CARTESIA_API_KEY=... python3 scripts/list_cartesia_voices.py --lang en
    # optional filters / model hint:
    #   --lang en  --gender feminine  --search calm  --model sonic-2

It prints, to stderr, a validation report for the ids currently in the catalog
plus ``DEFAULT_CARTESIA_VOICE_ID`` (if set), and to stdout the filtered voices as
``VoiceSpec(...)`` blocks ready to paste into ``VOICE_CATALOG``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

API_URL = os.environ.get("CARTESIA_API_URL", "https://api.cartesia.ai").rstrip("/")
VERSION = os.environ.get("CARTESIA_VERSION", "2024-11-13")

# Ids currently referenced in the repo, so the script can flag drift.
CATALOG_IDS: dict[str, str] = {
    "a0e99841-438c-4a64-b679-ae501e7d6091": "Barbershop Man",
    "729651dc-c6c3-4ee5-97fa-350da1f88600": "Sweet Lady",
    "a167e0f3-df7e-4d52-a9c3-f949145efdab": "Friendly Reading Man",
    "b7d50908-b17c-442d-ad8d-810c63997ed9": "Calm Lady",
}

_GENDERS = {"masculine", "feminine", "gender_neutral"}

# (url, headers) -> parsed JSON. Injected in tests so pagination is unit-testable
# without a network call.
JsonGetter = Callable[[str, "dict[str, str]"], Any]


def _get_json(url: str, headers: dict[str, str]) -> Any:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (https Cartesia API)
        return json.loads(resp.read().decode())


def fetch_voices(key: str, *, get_json: JsonGetter = _get_json) -> list[dict[str, Any]]:
    """Fetch every voice, following Cartesia's cursor pagination if present.

    Stops (and warns) rather than under-fetching silently if a page claims
    ``has_more`` but exposes no cursor id — the exact /voices pagination shape is
    not pinned by the repo, so surface drift instead of hiding it.
    """
    headers = {"Authorization": f"Bearer {key}", "Cartesia-Version": VERSION}
    params: dict[str, str] = {"limit": "100"}
    voices: list[dict[str, Any]] = []
    while True:
        body = get_json(f"{API_URL}/voices?{urllib.parse.urlencode(params)}", headers)
        page = body["data"] if isinstance(body, dict) and "data" in body else body
        if not isinstance(page, list):
            raise ValueError(
                f"unexpected /voices response shape: {type(body).__name__}"
            )
        voices.extend(page)
        if not (isinstance(body, dict) and body.get("has_more")):
            break
        cursor = page[-1].get("id") if page else None
        if not cursor:
            print(
                "warning: response has_more=true but no cursor id found; stopping — "
                "the list may be incomplete (verify the /voices pagination shape).",
                file=sys.stderr,
            )
            break
        params["starting_after"] = str(cursor)
    return voices


def _languages(v: dict[str, Any]) -> list[str]:
    """Normalize a voice's language(s) to a list (Cartesia may return a scalar or list)."""
    lang = v.get("language")
    if isinstance(lang, list):
        return [str(x) for x in lang if x]
    return [str(lang)] if lang else []


def _first_line(text: str | None) -> str:
    return (text or "").splitlines()[0].replace('"', "'").strip() if text else ""


def as_voicespec(v: dict[str, Any], model: str) -> str:
    gender = v.get("gender")
    gender_line = f'        gender="{gender}",\n' if gender in _GENDERS else ""
    langs = _languages(v)
    return (
        "    VoiceSpec(\n"
        f'        cartesia_voice_id="{v.get("id", "")}",\n'
        f'        name="{_first_line(v.get("name")) or "Unnamed"}",\n'
        f'        language="{langs[0] if langs else "en"}",\n'
        f"{gender_line}"
        f'        description="{_first_line(v.get("description"))}",\n'
        f'        tts_model_hint="{model}",\n'
        "    ),"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="List Cartesia voices for the catalog.")
    ap.add_argument("--lang", help="filter by language code, e.g. en")
    ap.add_argument("--gender", choices=sorted(_GENDERS), help="filter by gender")
    ap.add_argument("--search", help="case-insensitive substring on name/description")
    ap.add_argument(
        "--model", default="sonic-2", help="tts_model_hint to emit (default: sonic-2)"
    )
    args = ap.parse_args(argv)

    key = os.environ.get("CARTESIA_API_KEY")
    if not key:
        print("CARTESIA_API_KEY is not set in the environment.", file=sys.stderr)
        return 2

    try:
        voices = fetch_voices(key)
    except urllib.error.HTTPError as exc:
        hint = " (check CARTESIA_API_KEY)" if exc.code in (401, 403) else ""
        print(f"Cartesia request failed: HTTP {exc.code}{hint}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, ValueError) as exc:
        print(f"Cartesia request failed: {exc}", file=sys.stderr)
        return 1

    live_ids = {v.get("id") for v in voices}
    print(f"# {len(voices)} voices in your Cartesia library", file=sys.stderr)
    print("# Catalog id validation (does each still resolve?):", file=sys.stderr)
    for vid, label in CATALOG_IDS.items():
        mark = "OK     " if vid in live_ids else "MISSING"
        print(f"#   {mark}  {label}  ({vid})", file=sys.stderr)
    default_id = os.environ.get("DEFAULT_CARTESIA_VOICE_ID")
    if default_id:
        mark = "OK     " if default_id in live_ids else "MISSING"
        print(f"#   {mark}  DEFAULT_CARTESIA_VOICE_ID  ({default_id})", file=sys.stderr)
    print("", file=sys.stderr)

    filtered = voices
    if args.lang:
        filtered = [v for v in filtered if args.lang in _languages(v)]
    if args.gender:
        filtered = [v for v in filtered if v.get("gender") == args.gender]
    if args.search:
        needle = args.search.lower()
        filtered = [
            v
            for v in filtered
            if needle in (v.get("name") or "").lower()
            or needle in (v.get("description") or "").lower()
        ]

    print(
        f"# {len(filtered)} voices after filters — paste the ones you want into VOICE_CATALOG:"
    )
    for v in sorted(
        filtered, key=lambda x: ((_languages(x) or [""])[0], (x.get("name") or ""))
    ):
        print(as_voicespec(v, args.model))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
