"""Contract-freeze test for the compat Voice catalog (Task 10).

Verifies that every voice returned by GET /list-voices and GET /get-voice/{voice_id}
conforms to the captured RetellAI oracle VoiceResponse component:
  - provider == "cartesia"  (oracle enum member)
  - gender in {"male","female"}  (oracle required enum, non-null)
  - full shape validated by assert_conforms
"""

from __future__ import annotations

import pytest

from tests.compat.conformance import assert_conforms

pytestmark = pytest.mark.frozen


def test_list_voices_conforms_to_oracle(compat_client, compat_headers):
    voices = compat_client.get("/list-voices", headers=compat_headers).json()
    assert voices, "expected a non-empty curated catalog"
    for v in voices:
        assert v["provider"] == "cartesia", f"wrong provider: {v['provider']!r}"
        assert v["gender"] in ("male", "female"), (
            f"gender {v['gender']!r} not in oracle enum {{male,female}}"
        )
        assert_conforms(v, "VoiceResponse")


def test_get_voice_conforms_to_oracle(compat_client, compat_headers):
    """Individual get-voice also returns an oracle-conformant object."""
    voices = compat_client.get("/list-voices", headers=compat_headers).json()
    assert voices, "expected a non-empty curated catalog"
    # Check first voice via get-voice endpoint
    voice_id = voices[0]["voice_id"]
    v = compat_client.get(f"/get-voice/{voice_id}", headers=compat_headers).json()
    assert v["provider"] == "cartesia", f"wrong provider: {v['provider']!r}"
    assert v["gender"] in ("male", "female"), (
        f"gender {v['gender']!r} not in oracle enum {{male,female}}"
    )
    assert_conforms(v, "VoiceResponse")
