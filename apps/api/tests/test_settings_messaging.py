from usan_api.settings import Settings

_BASE = {
    "DATABASE_URL": "postgresql://u:p@localhost/db",
    "LIVEKIT_API_KEY": "k",
    "LIVEKIT_API_SECRET": "a" * 32,
    "LIVEKIT_URL": "ws://livekit:7880",
    "JWT_SIGNING_KEY": "s" * 32,
    "OPERATOR_API_KEY": "o" * 32,
}


def test_messaging_defaults_disabled():
    s = Settings(**_BASE)
    assert s.telnyx_messaging_enabled is False
    assert s.telnyx_messaging_api_key is None
    assert s.telnyx_messaging_profile_id is None
    assert s.telnyx_from_number is None
    assert s.telnyx_messaging_api_url == "https://api.telnyx.com/v2"
    assert s.telnyx_messaging_timeout_s == 10


def test_messaging_blank_aliases_coerce_to_none():
    s = Settings(
        **_BASE,
        TELNYX_MESSAGING_API_KEY="   ",
        TELNYX_MESSAGING_PROFILE_ID="",
        TELNYX_FROM_NUMBER="",
    )
    assert s.telnyx_messaging_api_key is None
    assert s.telnyx_messaging_profile_id is None
    assert s.telnyx_from_number is None


def test_messaging_enabled_and_secret_set():
    s = Settings(
        **_BASE,
        TELNYX_MESSAGING_ENABLED="true",
        TELNYX_MESSAGING_API_KEY="KEY123",
        TELNYX_MESSAGING_PROFILE_ID="mp1",
        TELNYX_FROM_NUMBER="+15551230000",
    )
    assert s.telnyx_messaging_enabled is True
    assert s.telnyx_messaging_api_key.get_secret_value() == "KEY123"
    assert s.telnyx_messaging_profile_id == "mp1"
    assert s.telnyx_from_number == "+15551230000"
