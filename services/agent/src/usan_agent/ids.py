"""Shared validation for the server-issued call_id.

call_id flows into both API URL paths (api_client) and GCS object keys (recording).
A single validator keeps that defense-in-depth consistent across every sink, so a
malformed id can never reach a URL path or a storage key.
"""

import re

# call_id is a server-issued UUID; reject anything else before it reaches a URL path
# or a GCS object key — defense-in-depth against path traversal / SSRF, and a fail-fast
# on a malformed id rather than emitting a garbled request or a stray object.
_CALL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate_call_id(call_id: str) -> str:
    """Return ``call_id`` unchanged if it is a safe id, else raise ``ValueError``."""
    if not isinstance(call_id, str) or not _CALL_ID_RE.fullmatch(call_id):
        raise ValueError("call_id must match ^[A-Za-z0-9_-]{1,64}$")
    return call_id
