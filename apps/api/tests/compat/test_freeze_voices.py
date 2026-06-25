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


def test_every_catalog_voice_gender_is_oracle_mappable():
    """Catalog-side guard: every non-deprecated voice must have a gender mappable
    to the oracle VoiceGender enum. Prevents future catalog additions (e.g.,
    gender_neutral) from causing 500 errors in /list-voices or /get-voice."""
    from usan_api.compat.routers.catalog import _GENDER_MAP
    from usan_api.schemas.voice_catalog import VOICE_CATALOG

    for spec in VOICE_CATALOG:
        if spec.deprecated:
            continue
        assert spec.gender in _GENDER_MAP, (
            f"catalog voice {spec.name!r} has gender {spec.gender!r} with no oracle mapping; "
            f"/list-voices would 500. Add a mapping or fix the catalog entry."
        )
