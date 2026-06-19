"""Phone masking + PII fingerprinting for admin-plane response bodies and logs.

Extracted bit-identical from ``routers/admin_contacts.py::_mask`` so both the contacts
roster and the calls read model render phones the same way (spec §6.3).
"""

import hashlib


def mask_phone(phone: str | None) -> str:
    """'***' + last 4 digits; 'unknown' when absent. The ONLY phone rendering
    permitted in admin-plane response bodies (spec §6.3)."""
    return "***" + phone[-4:] if phone else "unknown"


def email_fingerprint(email: str | None) -> str:
    """A short, stable, NON-reversible fingerprint of an email for log correlation.

    Email is PII (and PHI-adjacent for workforce members). Auth events must not bind the
    raw address into the retained log sink — the authoritative record is the actor_email
    column on the admin_audit row. This 12-hex SHA-256 prefix lets operators correlate
    repeated attempts from the same identity without exposing the address itself.
    """
    if not email:
        return "unknown"
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:12]
