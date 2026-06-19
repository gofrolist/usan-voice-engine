"""Presigned recording URLs — the single signing path for both planes (spec §4.2).

Extracted verbatim from ``routers/calls.py::_presigned_recording_url``. The operator
plane calls with neither ``actor`` nor ``max_ttl_s``, so its behavior and locked-sink
log records stay bit-identical; the admin plane passes both, reusing the same keyless
V4 signing (``asyncio.to_thread`` offload, ``expected_bucket`` fail-closed) with a
TTL ceiling. The signer is called via module attribute so tests can monkeypatch
``object_storage.generate_signed_url``.
"""

import asyncio

from loguru import logger

from usan_api import object_storage, phi_audit
from usan_api.db.models import Call
from usan_api.settings import Settings

# Admin-plane TTL ceiling (spec §4.2/§8): a signed URL is IP-unbound — it defeats the
# CIDR gate once issued — so the admin plane caps exposure at 10 minutes. The settings
# default (3600) is the MAX of its 60–3600 range, not "short". Constant, not an env key.
ADMIN_RECORDING_URL_MAX_TTL_S = 600


async def presigned_recording_url(
    call: Call,
    settings: Settings,
    *,
    client_host: str,
    actor: str | None = None,
    max_ttl_s: int | None = None,
) -> str | None:
    """Sign a short-lived GET URL for the call's recording, or None if absent/disabled."""
    if not call.recording_uri or not settings.gcs_bucket:
        return None
    ttl = (
        settings.recording_signed_url_ttl_s
        if max_ttl_s is None
        else min(settings.recording_signed_url_ttl_s, max_ttl_s)
    )
    try:
        url = await asyncio.to_thread(
            object_storage.generate_signed_url,
            call.recording_uri,
            ttl,
            expected_bucket=settings.gcs_bucket,
        )
    except ValueError:
        # Bucket-mismatch / path rejection: generate_signed_url's message embeds the
        # gs:// URI (which encodes call identity / is PHI-adjacent), so log only the
        # call_id + a static reason — never the traceback — into the locked sink.
        logger.bind(call_id=str(call.id)).warning(
            "Failed to sign recording URL (rejected by bucket/path validation)"
        )
        return None
    except Exception as exc:
        # Transient GCS / credential failure: keep the silent-None fallback and record
        # the exception TYPE NAME only (no traceback, no URI) so operators can still
        # distinguish failure modes without leaking the gs:// path.
        logger.bind(call_id=str(call.id), err=type(exc).__name__).warning(
            "Failed to sign recording URL"
        )
        return None
    # Access log: every issued recording URL is audit-logged with the caller's host
    # (spec §10). The gs:// URI itself is PHI-adjacent and the signed URL is a bearer
    # secret, so both are omitted from the locked-sink line.
    phi_audit.log_recording_url_accessed(call_id=call.id, client=client_host, actor=actor)
    return url
