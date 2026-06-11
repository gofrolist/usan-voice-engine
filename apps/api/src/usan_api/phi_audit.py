"""Single source of the load-bearing locked-sink PHI-access log lines (spec §6.1).

The 6-year locked Cloud Logging sink (``infra/terraform/observability.tf:47``, bucket
``usan-phi-audit``, ``locked = true``) substring-matches the two message constants
below. Both planes (operator ``/v1/calls`` and admin ``/v1/admin/calls``) emit their
PHI accesses through these helpers; extra bound fields are safe — the sink filter is
a contains match — but the messages must NEVER change: renaming them silently breaks
the immutable 6-year audit trail.

Bound fields are ids, client host, actor email, counts, and the ``has_recording``
flag only. URLs are bearer secrets and are never passed to or logged by these
helpers; transcript content never leaves the database via logs.
"""

import uuid
from typing import Any

from loguru import logger

TRANSCRIPT_ACCESSED = "Transcript accessed"
RECORDING_URL_ACCESSED = "Recording URL accessed"


def log_transcript_accessed(
    *, call_id: uuid.UUID, client: str, segments: int, actor: str | None = None
) -> None:
    """Emit the locked-sink transcript-access line (ids/host/count only — never content)."""
    bound: dict[str, Any] = {"call_id": str(call_id), "client": client, "segments": segments}
    if actor is not None:  # bind only when present: operator-plane records stay bit-identical
        bound["actor"] = actor
    logger.bind(**bound).info(TRANSCRIPT_ACCESSED)


def log_recording_url_accessed(
    *, call_id: uuid.UUID, client: str, actor: str | None = None
) -> None:
    """Emit the locked-sink recording-URL-access line (ids/host/flag only — never the URL)."""
    bound: dict[str, Any] = {"call_id": str(call_id), "client": client, "has_recording": True}
    if actor is not None:  # bind only when present: operator-plane records stay bit-identical
        bound["actor"] = actor
    logger.bind(**bound).info(RECORDING_URL_ACCESSED)
