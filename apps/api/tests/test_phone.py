import pytest

from usan_api.phone import to_e164


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The bug: Telnyx delivers a bare US national caller-ID (no +1).
        ("6692388604", "+16692388604"),
        # US with country code, no plus.
        ("16692388604", "+16692388604"),
        # Already E.164 -> unchanged.
        ("+16692388604", "+16692388604"),
        # Separators are stripped.
        ("(669) 238-8604", "+16692388604"),
        ("+1 669-238-8604", "+16692388604"),
        # Non-US E.164 passes through.
        ("+447911123456", "+447911123456"),
        # Blank / None -> None.
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_to_e164(raw, expected):
    assert to_e164(raw) == expected


def test_to_e164_is_idempotent():
    once = to_e164("6692388604")
    assert to_e164(once) == once
