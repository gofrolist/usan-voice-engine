"""Self-test for the oracle conformance assertion helpers.

Proves the helpers are correct (not just strict):
  - REJECT case: missing required fields must FAIL
  - ACCEPT case: valid payload with a nullable field set to null must PASS
    (this is the exact case naive jsonschema/Draft202012Validator fails on
     OpenAPI 3.0 nullable:true schemas)
"""

import pytest
from jsonschema.exceptions import ValidationError

from tests.compat.conformance import assert_conforms, assert_sdk_roundtrip


def test_assert_conforms_rejects_bad_voice() -> None:
    """Empty dict must fail — VoiceResponse requires voice_id, voice_name, provider, gender."""
    with pytest.raises(ValidationError):
        assert_conforms({}, "VoiceResponse")


def test_assert_conforms_accepts_valid_voice() -> None:
    """A minimal valid VoiceResponse must pass with no exception."""
    assert_conforms(
        {
            "voice_id": "retell-Cimo",
            "voice_name": "Adrian",
            "provider": "elevenlabs",
            "gender": "male",
        },
        "VoiceResponse",
    )


def test_assert_conforms_accepts_nullable_field_as_null() -> None:
    """ChatResponse.version is nullable:true — passing None must PASS.

    This is the exact case naive Draft202012Validator wrongly rejects for
    OpenAPI 3.0 schemas (it does not understand nullable:true).
    """
    # ChatResponse required: chat_id, agent_id, chat_status
    # version + end_timestamp are nullable:true
    assert_conforms(
        {
            "chat_id": "chat_abc",
            "agent_id": "agent_xyz",
            "chat_status": "ongoing",
            "version": None,  # nullable:true — must be accepted
            "end_timestamp": None,  # nullable:true — must be accepted
        },
        "ChatResponse",
    )


def test_assert_sdk_roundtrip_voice() -> None:
    """retell.types.VoiceResponse must parse a valid voice payload."""
    assert_sdk_roundtrip(
        {
            "voice_id": "retell-Cimo",
            "voice_name": "Adrian",
            "provider": "elevenlabs",
            "gender": "male",
        },
        "retell.types:VoiceResponse",
    )
