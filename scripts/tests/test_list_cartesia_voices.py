"""Unit tests for scripts/list_cartesia_voices.py.

Runs in the `pytest (scripts)` CI job (Python 3.12 + pytest + pyyaml). Stdlib-only
target, so pagination is tested via an injected JSON getter — no network, no httpx.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from list_cartesia_voices import (  # noqa: E402
    _first_line,
    _languages,
    as_voicespec,
    fetch_voices,
)


def _getter(pages):
    """Return (get_json, calls): each call returns the next canned page; URLs recorded."""
    calls: list[str] = []

    def get_json(url, headers):
        calls.append(url)
        return pages[len(calls) - 1]

    return get_json, calls


# --- formatting -------------------------------------------------------------


def test_first_line_takes_first_line_escapes_quotes_and_trims():
    assert _first_line('  he said "hi"\nsecond  ') == "he said 'hi'"
    assert _first_line(None) == ""
    assert _first_line("") == ""


def test_as_voicespec_includes_known_gender_and_fields():
    out = as_voicespec(
        {
            "id": "abc",
            "name": "Calm Lady",
            "language": "en",
            "gender": "feminine",
            "description": "Soothing voice",
        },
        "sonic-2",
    )
    assert 'cartesia_voice_id="abc"' in out
    assert 'name="Calm Lady"' in out
    assert 'gender="feminine"' in out
    assert 'language="en"' in out
    assert 'description="Soothing voice"' in out
    assert 'tts_model_hint="sonic-2"' in out


def test_as_voicespec_omits_unknown_gender_and_defaults_language():
    out = as_voicespec({"id": "x", "name": "N", "gender": "robot"}, "sonic-2")
    assert "gender=" not in out  # unknown gender dropped, not emitted invalid
    assert 'language="en"' in out  # missing language -> en fallback


def test_as_voicespec_language_list_uses_first():
    out = as_voicespec({"id": "x", "name": "N", "language": ["fr", "en"]}, "sonic-2")
    assert 'language="fr"' in out


def test_languages_normalizes_scalar_and_list():
    assert _languages({"language": "en"}) == ["en"]
    assert _languages({"language": ["en", "fr"]}) == ["en", "fr"]
    assert _languages({}) == []


# --- pagination -------------------------------------------------------------


def test_fetch_voices_bare_list_single_call():
    get_json, calls = _getter([[{"id": "a"}, {"id": "b"}]])
    out = fetch_voices("k", get_json=get_json)
    assert [v["id"] for v in out] == ["a", "b"]
    assert len(calls) == 1


def test_fetch_voices_follows_cursor_until_has_more_false():
    pages = [
        {"data": [{"id": "a"}], "has_more": True},
        {"data": [{"id": "b"}], "has_more": False},
    ]
    get_json, calls = _getter(pages)
    out = fetch_voices("k", get_json=get_json)
    assert [v["id"] for v in out] == ["a", "b"]
    assert len(calls) == 2
    assert "starting_after=a" in calls[1]  # cursor threaded from page 1's last id


def test_fetch_voices_stops_and_warns_when_cursor_missing(capsys):
    # has_more=true but the last item has no id: stop (don't loop forever / under-fetch silently).
    get_json, calls = _getter([{"data": [{"name": "no-id"}], "has_more": True}])
    out = fetch_voices("k", get_json=get_json)
    assert len(out) == 1
    assert len(calls) == 1
    assert "has_more" in capsys.readouterr().err


def test_fetch_voices_rejects_unexpected_shape():
    get_json, _ = _getter([{"data": {"not": "a list"}}])
    with pytest.raises(ValueError):
        fetch_voices("k", get_json=get_json)
