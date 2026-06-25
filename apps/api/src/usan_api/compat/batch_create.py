"""Compat batch-create service (feature 003, T040).

Maps a RetellAI ``create-batch-call`` payload onto the native one-off batch model:
per-task number→Contact lazy upsert (reusing US1's T022 shim), then a single
``batches_repo.create_batch_with_targets`` insert. The batch lands ``scheduled``;
the existing schedule poller materializes and **gates each target per-target**
(DNC / quiet-hours / window / daily-cap) at dial time — faithful to RetellAI,
where blocked numbers are skipped per-target, not a batch-level reject.

All-or-nothing validation: every task is checked (E.164, reserved-var keys,
in-batch duplicates, override liveness) BEFORE any Contact is materialized, so a
bad payload persists nothing and returns a clean RetellAI ``{status,message}`` 422.

Idempotency (Constitution V — no double-dial on a CRM retry): RetellAI exposes no
batch idempotency, so a deterministic key is synthesized from the call targets +
schedule (NOT the label). An identical resubmit replays the same batch instead of
dialing the same people twice.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, time
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat.call_create import upsert_contact_for_number
from usan_api.compat.errors import CompatError
from usan_api.compat.ids import decode_agent_id
from usan_api.compat.schemas.batch import CallTimeWindow, CreateBatchCallRequest
from usan_api.compat.serialization import RESERVED_VAR_PREFIX, pack_dynamic_vars
from usan_api.db.models import CallBatch
from usan_api.phone import to_e164
from usan_api.repositories import agent_profiles as agent_profiles_repo
from usan_api.repositories import call_batches as batches_repo
from usan_api.schedule_windows import days_to_mask
from usan_api.schemas.batch import BatchTargetIn, BatchWindow, CreateBatchRequest, payload_digest

_IDEM_PREFIX = "compat-batch:"

# Oracle DayOfWeek (full English names) → native 3-letter lowercase day abbreviations.
# Oracle convention: "Monday"=week start (ISO 8601); no int mapping — the oracle's
# ``day`` field carries full-name strings directly (DayOfWeek enum).
_ORACLE_DAY_TO_NATIVE: dict[str, str] = {
    "Monday": "mon",
    "Tuesday": "tue",
    "Wednesday": "wed",
    "Thursday": "thu",
    "Friday": "fri",
    "Saturday": "sat",
    "Sunday": "sun",
}


def _map_call_time_window(ctw: CallTimeWindow | None) -> BatchWindow | None:
    """Map a typed RetellAI ``CallTimeWindow`` onto the native ``BatchWindow``.

    Partial-map gaps (documented; none silently dropped):
      1. Only ``windows[0]`` maps — native supports a single window.
         Additional windows are echoed in the compat response but not applied.
      2. ``timezone`` cannot be expressed — ``BatchWindow`` is per-contact-local
         with no tz field. Echoed in the response; not applied here.
      3. Cross-midnight (slot.start >= slot.end) is rejected by ``BatchWindow``
         (``start_local >= end_local``). When this occurs, return None so the
         native window is left unset; the typed value is still echoed.
      4. Unknown/invalid oracle day names are silently skipped (extra="allow" on
         the oracle schema means forward-compat unknown names may appear).
    """
    if ctw is None:
        return None

    slot = ctw.windows[0]

    # Oracle uses minutes-since-midnight; native uses time objects.
    start_min = slot.start
    end_min = slot.end

    # Cross-midnight guard: oracle says startMin < endMin is required, but a
    # client could send an invalid window; native would reject it with a 422 deep
    # in BatchWindow validation. Intercept here to echo the typed value cleanly.
    if start_min >= end_min:
        # Gap: cross-midnight or zero-length window — cannot map; leave native unset.
        return None

    start_local = time(hour=start_min // 60, minute=start_min % 60)
    end_local = time(hour=end_min // 60, minute=end_min % 60)

    days_of_week: list[str] | None = None
    if ctw.day:
        mapped = [_ORACLE_DAY_TO_NATIVE[d] for d in ctw.day if d in _ORACLE_DAY_TO_NATIVE]
        days_of_week = mapped if mapped else None

    try:
        return BatchWindow(
            start_local=start_local,
            end_local=end_local,
            days_of_week=days_of_week,
        )
    except ValueError:
        # Gap: BatchWindow validator rejected the window (e.g. no quiet-hours
        # intersection). Echo the typed value; leave native window unset.
        return None


class _PreparedTask:
    """A task that passed pass-1 validation, carrying everything pass 2 needs."""

    __slots__ = ("index", "phone", "packed", "override_id", "metadata")

    def __init__(
        self,
        index: int,
        phone: str,
        packed: dict[str, Any],
        override_id: uuid.UUID | None,
        metadata: dict[str, Any] | None,
    ) -> None:
        self.index = index
        self.phone = phone
        self.packed = packed
        self.override_id = override_id
        self.metadata = metadata


async def _validate_tasks(db: AsyncSession, body: CreateBatchCallRequest) -> list[_PreparedTask]:
    """Pass 1 — validate every task, collect ALL failures, mutate nothing.

    Raises ``CompatError(422)`` naming each offending ``target_index`` if anything
    fails, so no Contact is upserted on a doomed batch (all-or-nothing).
    """
    errors: list[str] = []
    prepared: list[_PreparedTask] = []
    seen: dict[str, int] = {}
    seen_ext: dict[str, int] = {}
    for index, task in enumerate(body.tasks):
        phone = to_e164(task.to_number)
        if phone is None:
            errors.append(f"task[{index}]: invalid to_number")
            continue
        if any(
            str(k).startswith(RESERVED_VAR_PREFIX)
            for k in (task.retell_llm_dynamic_variables or {})
        ):
            errors.append(f"task[{index}]: dynamic-variable keys must not start with '__meta'")
            continue
        first = seen.setdefault(phone, index)
        if first != index:
            errors.append(f"task[{index}]: duplicate to_number (target_index {first})")
            continue
        # Two tasks sharing one external_id would collide on uq_contacts_external_id_org at
        # upsert time; catch it here so the batch fails all-or-nothing with a clean 422.
        ext = (task.metadata or {}).get("external_id")
        if ext is not None:
            first_ext = seen_ext.setdefault(str(ext), index)
            if first_ext != index:
                errors.append(f"task[{index}]: duplicate external_id (target_index {first_ext})")
                continue
        override_id: uuid.UUID | None = None
        if task.override_agent_id:
            try:
                override_id = decode_agent_id(task.override_agent_id)
            except CompatError:
                errors.append(f"task[{index}]: invalid override_agent_id")
                continue
            if not await agent_profiles_repo.is_live_profile(db, override_id):
                errors.append(f"task[{index}]: override_agent_id must reference a published agent")
                continue
        packed = pack_dynamic_vars(task.retell_llm_dynamic_variables, task.metadata)
        prepared.append(_PreparedTask(index, phone, packed, override_id, task.metadata))
    if errors:
        raise CompatError(422, "; ".join(errors))
    return prepared


def _synth_idempotency_key(
    organization_id: uuid.UUID,
    *,
    from_number: str,
    trigger_ms: int | None,
    concurrency: int | None,
    prepared: list[_PreparedTask],
) -> str:
    """Deterministic key over the call TARGETS + schedule (NOT the label): an
    identical resubmit replays the same batch instead of dialing twice. The org +
    ``compat-batch:`` namespace keep it from ever colliding with other batch keys.
    """
    payload = json.dumps(
        {
            "org": str(organization_id),
            "from": from_number,
            "trigger_ms": trigger_ms,
            "concurrency": concurrency,
            "tasks": [
                {
                    "to": t.phone,
                    "vars": t.packed,
                    "override": None if t.override_id is None else str(t.override_id),
                }
                for t in prepared
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return _IDEM_PREFIX + hashlib.sha256(payload.encode()).hexdigest()


async def create_compat_batch(
    db: AsyncSession,
    settings: Any,
    body: CreateBatchCallRequest,
    *,
    organization_id: uuid.UUID,
) -> CallBatch:
    """Validate, upsert a Contact per task, and persist the batch; returns the
    created (or replayed) ``CallBatch``."""
    from_e164 = to_e164(body.from_number)
    if from_e164 is None:
        raise CompatError(422, "invalid from_number")

    prepared = await _validate_tasks(db, body)

    idem_key = _synth_idempotency_key(
        organization_id,
        from_number=from_e164,
        trigger_ms=body.trigger_timestamp,
        concurrency=body.reserved_concurrency,
        prepared=prepared,
    )
    # Replay BEFORE any side effect: an identical resubmit returns the original
    # batch (no second batch, no contact churn) — Constitution V.
    existing = await batches_repo.get_by_idempotency_key(db, idem_key)
    if existing is not None:
        return existing

    # Pass 2 — materialize Contacts and build the native targets (order preserved
    # so target_index matches the submitted array).
    target_ins: list[BatchTargetIn] = []
    try:
        for t in prepared:
            contact = await upsert_contact_for_number(db, settings, t.phone, t.metadata)
            try:
                target_ins.append(
                    BatchTargetIn(
                        contact_id=contact.id,
                        dynamic_vars=t.packed,
                        profile_override=t.override_id,
                    )
                )
            except ValidationError as exc:  # e.g. dynamic_vars over the 8 KB cap
                raise CompatError(
                    422, f"task[{t.index}]: invalid retell_llm_dynamic_variables or metadata"
                ) from exc
    except IntegrityError as exc:
        # A task's external_id collides with an EXISTING contact in this org
        # (uq_contacts_external_id_org) — the intra-batch case is caught in pass 1, this
        # guards the pre-existing-contact case. Keep the all-or-nothing/clean-422 contract
        # instead of leaking a 500; roll back the poisoned session first.
        await db.rollback()
        raise CompatError(422, "a task's external_id conflicts with an existing contact") from exc

    trigger_at = (
        None
        if body.trigger_timestamp is None
        else datetime.fromtimestamp(body.trigger_timestamp / 1000, tz=UTC)
    )
    # body.name is a label; a missing one is synthesized from the (label-free) key so
    # it stays stable across retries (PHI-free — never an contact's name, by convention).
    name = (body.name or "").strip() or f"batch-{idem_key[len(_IDEM_PREFIX) :][:12]}"
    # Map call_time_window onto the native dial window (partial map: only windows[0],
    # no timezone, cross-midnight → None; see _map_call_time_window for all gaps).
    native_window = _map_call_time_window(body.call_time_window)

    try:
        native_req = CreateBatchRequest(
            name=name,
            idempotency_key=idem_key,
            trigger_at=trigger_at,
            window=native_window,
            max_concurrency=body.reserved_concurrency,
            profile_override=None,  # RetellAI overrides are per-task, never batch-level
            targets=target_ins,
        )
    except ValidationError as exc:
        raise CompatError(422, "invalid batch payload") from exc

    try:
        batch = await batches_repo.create_batch_with_targets(
            db,
            name=native_req.name,
            idempotency_key=idem_key,
            payload_digest=payload_digest(native_req),
            trigger_at=native_req.trigger_at,
            window_start_local=native_req.window.start_local if native_req.window else None,
            window_end_local=native_req.window.end_local if native_req.window else None,
            days_of_week=(
                days_to_mask(native_req.window.days_of_week)
                if native_req.window and native_req.window.days_of_week
                else None
            ),
            max_concurrency=native_req.max_concurrency,
            profile_override=None,
            targets=native_req.targets,
        )
        await db.commit()
    except IntegrityError as exc:
        # UNIQUE (idempotency_key, organization_id) race with a concurrent identical
        # POST: the other request won — replay its batch rather than 500.
        await db.rollback()
        raced = await batches_repo.get_by_idempotency_key(db, idem_key)
        if raced is None:
            raise
        del exc
        return raced
    await db.refresh(batch)
    return batch
