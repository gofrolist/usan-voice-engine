import pytest

from usan_api.schemas._validators import (
    TIMEZONE_MAX_LENGTH,
    validate_iana_timezone,
)


def test_valid_iana_zone_returned_unchanged():
    assert validate_iana_timezone("America/New_York") == "America/New_York"
    assert validate_iana_timezone("Pacific/Honolulu") == "Pacific/Honolulu"


@pytest.mark.parametrize("bad", ["EST5", "", "Mars/Phobos", "america/new_york "])
def test_invalid_iana_zone_raises_valueerror(bad):
    with pytest.raises(ValueError, match="unknown IANA timezone"):
        validate_iana_timezone(bad)


def test_timezone_max_length_constant():
    assert TIMEZONE_MAX_LENGTH == 64
