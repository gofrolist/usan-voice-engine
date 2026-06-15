"""Unit tests for ``usan_api.masking`` — the admin-plane phone masking helper.

``mask_phone`` is the extraction of ``routers/admin_contacts.py::_mask`` and must stay
bit-identical to it: ``'***' + last 4`` when a phone is present, ``'unknown'`` when
absent. It is the ONLY phone rendering permitted in admin-plane response bodies
(spec §6.3).
"""

from usan_api.masking import mask_phone


def test_mask_phone_last4():
    assert mask_phone("+15551234567") == "***4567"


def test_mask_phone_none_and_empty_unknown():
    assert mask_phone(None) == "unknown"
    assert mask_phone("") == "unknown"
