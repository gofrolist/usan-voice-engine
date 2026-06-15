"""Phone masking for admin-plane response bodies.

Extracted bit-identical from ``routers/admin_contacts.py::_mask`` so both the contacts
roster and the calls read model render phones the same way (spec §6.3).
"""


def mask_phone(phone: str | None) -> str:
    """'***' + last 4 digits; 'unknown' when absent. The ONLY phone rendering
    permitted in admin-plane response bodies (spec §6.3)."""
    return "***" + phone[-4:] if phone else "unknown"
