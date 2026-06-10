# Batch & Scheduled Calling — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the platform an in-repo owner for outbound call initiation at scale: recurring per-elder `CallSchedule` rows and one-off `CallBatch` campaigns that materialize ordinary `Call` rows (deterministic `sched:`/`batch:` idempotency keys) into the existing retry-poller dial path — plus the global concurrency gate, the emergency pause, and three dial-path hardenings (dial-time quiet-hours re-check, `elder_missing` FAILED guard, `schedule_retry` profile-override copy + batch-cancellation awareness).

**Architecture:** Everything in `apps/api` (zero `services/agent` changes). New: migration `0012`, three tables + `idx_calls_in_flight`, `schedule_windows.py` (pure zoneinfo math), `schedule_orchestrator.py` (3rd lifespan poller, 6-phase cycle, FOR UPDATE SKIP LOCKED, one row per txn), `schemas/schedule.py` + `schemas/batch.py`, `repositories/call_schedules.py` + `call_batches.py`, `routers/schedules.py` + `batches.py`. Touched: `retry_orchestrator.py` (gate, count+claim one txn), `livekit_dispatch.py`, `repositories/calls.py`, `ratelimit.py`, `retention.py`, `schemas/call.py`, `main.py`, `settings.py`, `observability/custom_metrics.py`, `infra/*`, `infra/grafana/provisioning/alerting/usan_alerts.yml`. Ships inert: `SCHEDULER_POLLER_ENABLED=false` + `CONCURRENCY_GATE_ENABLED=false` defaults.

**Tech Stack:** Python 3.14 (FastAPI, Pydantic v2, SQLAlchemy 2 async, Alembic raw-SQL, prometheus_client, loguru lazy `{}` ids-only, zoneinfo); testcontainers Postgres; Grafana alerts-as-code.

**Source spec:** `docs/superpowers/specs/2026-06-10-batch-scheduled-calling-design.md` (Final, adversarial review applied). This plan additionally carries a second adversarial review pass (integration + test-strategy) — see the **Review disposition** section at the end.

**Executor notes (read before starting):**
1. **Two dependency-driven placements, deliberate:** (a) the **8 Settings fields land in Task D1** — the Part-D poller/gate code reads them, so they cannot wait for Part E; Part E owns the infra mirroring + env contract tests. (b) **All metric objects and `.inc()`/`.set()` wiring land in Part E** — Part D delivers behavior with DB-state assertions only; Part E instruments it (failing-test-first works because the metrics don't exist until E). Do not define any new metric in Part D.
2. **Same-file sequencing (strict):** `repositories/calls.py` is edited by C4 → D2 → D3 → D5 → D6 → D7 → D9 (in that order); `livekit_dispatch.py` by D4 → D5 → E1; `schemas/call.py` by B4 → C5; `retry_orchestrator.py` by D6 → E1; `schedule_orchestrator.py` by D7 → D8 → D9 → E2; `routers/batches.py` by C4 → E2; `db/models.py` only A2; `main.py` by C3 → C4 → D10; `tests/conftest.py` by A1 → E1. Never parallelize tasks sharing a file.
3. House rules: repos take the request session, `flush()+refresh()`, **never commit**; routers commit. Raw `op.execute` migrations. Loguru lazy `{}` placeholders; **bind ids only — never `name`, phones, or var contents**. ruff line-length 100; run `uv run mypy .` before every push (CI runs it even though CLAUDE.md omits it). Naive `trigger_at` → UTC (precedent `ScheduleCallbackRequest._assume_utc`).
4. Statuses are TEXT + CHECK (house style, mirrors `follow_up_flags`) — plain `str` in models, `Literal` in schemas. No new members in `CallStatus`; `CANCELLED` already exists and gains its first writer here.
5. **Every verify command in this plan starts from the repo root** (`/Users/evgenii.vasilenko/gofrolist/usan-voice-engine`). Subagent executors get their cwd reset between bash calls, so every apps/api command is written with an explicit `cd apps/api && ` prefix and the scripts-tests commands run from the root. Do not strip the prefixes.

---

## Part A — Migration 0012 + models + conftest + contract tests

### Task A1: Migration `0012_batch_scheduled_calling.py` + conftest TRUNCATE + migration contract test

**Files:**
- Create: `apps/api/migrations/versions/0012_batch_scheduled_calling.py`
- Modify: `apps/api/tests/conftest.py` (TRUNCATE list, line 91–94)
- Test: `apps/api/tests/test_batch_migration.py`

- [ ] Step 1: Write the failing test — `apps/api/tests/test_batch_migration.py`, mirroring `test_phase3_migration.py`'s `_columns`/`_indexes` async-introspection helpers, **extended for this file**: the phase-3 `_columns` returns only `{column_name: data_type}` and cannot support the nullability/default assertions below — this file's `_columns` must also select `is_nullable` and `column_default` (return e.g. `{name: (data_type, is_nullable, column_default)}`). Also add a `_check_constraints(url, table) -> set[str]` helper querying `pg_constraint WHERE conrelid = :t::regclass AND contype = 'c'` and an `_indexdef(url, index) -> str` helper reading `pg_indexes.indexdef`:
  - `test_call_schedules_table_shape` — asserts columns/types: `id`=uuid, `elder_id`=uuid, `enabled`=boolean, `window_start_local`/`window_end_local`="time without time zone", `days_of_week`=smallint, `dynamic_vars`=jsonb, `profile_override`=uuid, `next_run_at`="timestamp with time zone", `last_materialized_date`=date, `last_result`=text, `last_result_at`="timestamp with time zone"; index `idx_call_schedules_due` exists and its indexdef contains `WHERE enabled`; check constraints `ck_call_schedules_window`, `ck_call_schedules_days`, `ck_call_schedules_result` present; UNIQUE on `elder_id` (indexname `call_schedules_elder_id_key` present).
  - `test_call_batches_table_shape` — asserts `payload_digest`=text NOT NULL (`is_nullable='NO'`), `status` default `'scheduled'`, `trigger_at`/`started_at`/`completed_at`/`cancelled_at` timestamptz, `max_concurrency` smallint; checks `ck_call_batches_status`, `ck_call_batches_window`, `ck_call_batches_days`, `ck_call_batches_maxconc`; `idx_call_batches_due` is a partial index on the scheduled status — assert `"WHERE" in indexdef and "scheduled" in indexdef` (do **not** assert an exact `WHERE` literal: `status` is TEXT, so Postgres renders `WHERE (status = 'scheduled'::text)`, not the enum-style cast rendering); **`idx_call_batches_open` indexdef contains both `cancelled` and `completed_at IS NULL`** (the bounded-working-set exit condition).
  - `test_call_batch_targets_table_shape` — asserts `id`=bigint, `target_index`=integer, `elder_id` nullable uuid, `skip_reason`/`final_status` text, `call_id` uuid, `materialized_at`/`finalized_at` timestamptz; `uq_call_batch_targets_idx` unique index present; `ck_call_batch_targets_status` present; partial indexes `idx_call_batch_targets_pending` (`WHERE ... 'pending'`), `idx_call_batch_targets_open`, `idx_call_batch_targets_call` (`WHERE (call_id IS NOT NULL)`).
  - `test_fk_delete_rules` — via `referential_constraints` (phase-3 pattern): `call_schedules→elders`=CASCADE; `call_batch_targets→call_batches`=CASCADE, `→elders`=SET NULL, `→calls`=SET NULL, `→agent_profiles`=SET NULL.
  - `test_idx_calls_in_flight_recency_keyed` — `idx_calls_in_flight` exists on table `calls`; indexdef contains `(updated_at)` and `'dialing'`, `'ringing'`, `'in_progress'` (the recency-bounded gate count, spec §3.4; `calls.status` IS a PG enum, so the quoted status strings appear in the indexdef).
  - `test_downgrade_then_upgrade_roundtrip` — `subprocess.run([sys.executable, "-m", "alembic", "downgrade", "0011"], ...)` then `upgrade head` (same env dict as conftest `database_url`); after the roundtrip all three tables exist again and `idx_calls_in_flight` is back. (Runs last; leaves the session DB at head.)

- [ ] Step 2: Run test, verify it FAILS

```bash
cd apps/api && uv run pytest tests/test_batch_migration.py -v
```
RED reason: alembic head is `0011`; `_columns` returns `{}` for all three tables → `KeyError: 'id'`; `idx_calls_in_flight` absent.

- [ ] Step 3: Implement `0012_batch_scheduled_calling.py` — `revision="0012"`, `down_revision="0011"`, raw `op.execute` only, SQL **verbatim from spec §3.1–§3.4** (the four `CREATE TABLE`/`CREATE INDEX` blocks, including all CHECK constraints, the `last_result` enum CHECK list `('created','replayed','rescheduled','skipped_window','skipped_invalid_timezone','skipped_daily_cap','dnc_blocked','key_conflict')`, and `CREATE INDEX idx_calls_in_flight ON calls (updated_at) WHERE status IN ('dialing','ringing','in_progress')`). Carry the spec's load-bearing comments into SQL comments (CASCADE rationale, SET-NULL-not-CASCADE on targets' elder_id, `idx_call_batches_open` exit condition, recency-bound rationale on `idx_calls_in_flight`). `downgrade()`: `DROP INDEX IF EXISTS idx_calls_in_flight`, then `DROP TABLE IF EXISTS call_batch_targets`, `call_batches`, `call_schedules` (reverse FK order).

  Also edit `tests/conftest.py` `_truncate_and_dispose`: prepend the three tables FK-children-first:

```python
"TRUNCATE call_batch_targets, call_batches, call_schedules, "
"agent_profile_versions, agent_profiles, admin_audit_log, "
```

- [ ] Step 4: Run test, verify PASS

```bash
cd apps/api && uv run pytest tests/test_batch_migration.py -v && ruff check migrations tests/conftest.py && ruff format --check migrations && uv run mypy .
```

- [ ] Step 5: Commit

```bash
git add apps/api/migrations/versions/0012_batch_scheduled_calling.py apps/api/tests/test_batch_migration.py apps/api/tests/conftest.py && git commit -m "feat(api): migration 0012 — call_schedules/call_batches/call_batch_targets + idx_calls_in_flight"
```

---

### Task A2: SQLAlchemy models `CallSchedule`, `CallBatch`, `CallBatchTarget`

**Files:**
- Modify: `apps/api/src/usan_api/db/models.py` (append after `SmsMessage`)
- Test: `apps/api/tests/test_batch_models.py`

- [ ] Step 1: Write the failing test — `tests/test_batch_models.py`, mirror of `test_phase3_models.py` (no DB; pure `__table__` introspection):
  - `test_call_schedule_columns_and_fk` — `__tablename__ == "call_schedules"`; column set ⊇ `{id, elder_id, enabled, window_start_local, window_end_local, days_of_week, dynamic_vars, profile_override, next_run_at, last_materialized_date, last_result, last_result_at, created_at, updated_at}`; `elder_id` FK ondelete == `"CASCADE"` and `cols["elder_id"].unique is True`; `profile_override` FK ondelete == `"SET NULL"`; `days_of_week` server_default arg contains `"127"`; `next_run_at` not nullable; `updated_at.onupdate is not None`.
  - `test_call_batch_columns_and_defaults` — `payload_digest` not nullable; `idempotency_key.unique is True`; `status` server_default contains `'scheduled'`; `window_start_local` nullable; `max_concurrency` nullable SmallInteger.
  - `test_call_batch_target_columns_and_fks` — `batch_id` ondelete `"CASCADE"`, `elder_id` ondelete `"SET NULL"` **and nullable**, `call_id` ondelete `"SET NULL"`; `status` server_default contains `'pending'`; `dynamic_vars` JSONB not nullable; `target_index` not nullable.

- [ ] Step 2: Run test, verify it FAILS

```bash
cd apps/api && uv run pytest tests/test_batch_models.py -v
```
RED reason: `ImportError: cannot import name 'CallSchedule' from 'usan_api.db.models'`.

- [ ] Step 3: Implement — append three models to `db/models.py` using existing imports plus `Date`, `Time` from `sqlalchemy` and `from datetime import date, time`. (No alias needed: `models.py` line 3 imports only the `datetime` **class** from the `datetime` module; `date`/`time` collide with nothing — SQLAlchemy's `Date`/`Time` are capitalized.)

```python
class CallSchedule(Base):
    __tablename__ = "call_schedules"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          server_default=text("gen_random_uuid()"))
    # CASCADE: a schedule is meaningless without its elder; calls.elder_id SET NULLs
    # independently, so call history survives (spec §3.1).
    elder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("elders.id", ondelete="CASCADE"),
        nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    window_start_local: Mapped[time] = mapped_column(Time, nullable=False)
    window_end_local: Mapped[time] = mapped_column(Time, nullable=False)
    days_of_week: Mapped[int] = mapped_column(SmallInteger, nullable=False,
                                              server_default=text("127"))
    dynamic_vars: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False,
                                                         server_default=text("'{}'"))
    profile_override: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_profiles.id", ondelete="SET NULL"))
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_materialized_date: Mapped[date | None] = mapped_column(Date)
    last_result: Mapped[str | None] = mapped_column(Text)
    last_result_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at / updated_at  # house pattern (server_default=func.now(), onupdate=func.now())
```

  `CallBatch` (`name` Text NOT NULL, `idempotency_key` Text unique nullable, `payload_digest` Text NOT NULL, `status` Text server_default `text("'scheduled'")`, `trigger_at`, nullable `window_start_local`/`window_end_local` Time, nullable `days_of_week` SmallInteger, nullable `max_concurrency` SmallInteger, `profile_override` FK SET NULL, `started_at`/`completed_at`/`cancelled_at`, created/updated) and `CallBatchTarget` (`id` BigInteger autoincrement, `batch_id` FK CASCADE NOT NULL, `target_index` Integer NOT NULL, `elder_id` FK SET NULL nullable, `dynamic_vars` JSONB, `profile_override` FK SET NULL, `status` server_default `text("'pending'")`, `skip_reason` Text, `call_id` FK `calls.id` SET NULL, `final_status` Text, `materialized_at`/`finalized_at`, created/updated) per spec §3.2/§3.3, with the SET-NULL rationale comments.

- [ ] Step 4: Run test, verify PASS

```bash
cd apps/api && uv run pytest tests/test_batch_models.py tests/test_batch_migration.py -v && ruff check src/usan_api/db/models.py && uv run mypy .
```

- [ ] Step 5: Commit

```bash
git add apps/api/src/usan_api/db/models.py apps/api/tests/test_batch_models.py && git commit -m "feat(api): CallSchedule/CallBatch/CallBatchTarget ORM models (migration 0012 mirror)"
```

---

### Task A3: Part A gate

- [ ] Step 4: `cd apps/api && uv run pytest -v && ruff check . && ruff format --check . && uv run mypy .` — full suite green (proves the conftest TRUNCATE change broke nothing) before Part B starts. Commit any `ruff format` rewrites as `chore(api): format Part A`.

---

## Part B — Schemas (codecs, caps, digest, reserved namespace) + repositories

### Task B1: `schedule_windows.py` — pure window/day-mask/next-occurrence math

**Files:**
- Create: `apps/api/src/usan_api/schedule_windows.py`
- Test: `apps/api/tests/test_schedule_windows.py`

- [ ] Step 1: Write the failing test (pure unit, no DB):
  - `test_days_mask_round_trip` — `days_to_mask(["mon"]) == 1`; `days_to_mask(["mon","sun"]) == 0b1000001`; `mask_to_days(127) == ["mon","tue","wed","thu","fri","sat","sun"]`; round-trip for every single day; order-insensitive input, canonical-order output.
  - `test_days_mask_rejects_unknown_and_empty` — `ValueError` for `["monday"]`, `[]`, `mask_to_days(0)`, `mask_to_days(128)`.
  - `test_effective_window_intersects_quiet_hours` — `effective_window(time(8), time(10)) == (time(9), time(10))`; full-inside passes through; `effective_window(time(21,30), time(22,30)) is None` (empty ⇒ None); boundary `(time(7), time(9))` → None (`[09:00,21:00)` start-inclusive).
  - `test_effective_window_is_timezone_invariant` — pure wall-clock: function takes no tz argument (assert via `inspect.signature`), pinning spec §3.1's tz-invariance claim structurally.
  - `test_next_run_at_inside_window_returns_after` — UTC elder, window 09:00–17:00 all days, `after`=12:00 local → returns `after` unchanged.
  - `test_next_run_at_before_start_returns_todays_start`, `test_next_run_at_after_end_skips_to_next_masked_day` — mask `["mon"]`, after=Mon 18:00 → next Mon 09:00.
  - `test_next_run_at_dst_spring_forward_recomputes_offset` — `America/New_York`, window 09:00–11:00, mask sat+sun, `after = 2026-03-07T23:00:00Z` → returns `2026-03-08T13:00:00Z` (09:00 **EDT**, not 14:00Z EST — the zoneinfo-recompute landmine from `quiet_hours.py`'s correctness note).
  - `test_next_run_at_dst_fall_back_recomputes_offset` — the symmetric landmine: `America/New_York`, window 09:00–17:00, mask sun, `after = 2026-11-01T00:00:00Z` (Sat Oct 31 20:00 EDT) → returns `2026-11-01T14:00:00Z` (09:00 **EST** after the Nov 1 fall-back, not 13:00Z EDT).
  - `test_next_run_at_invalid_tz_raises_value_error` — fail-closed contract.
  - `test_window_bounds_utc_and_day_bounds_utc` — `window_bounds_utc(date(2026,6,10), "America/New_York", window_start=time(9), window_end=time(17))` == (13:00Z, 21:00Z); `day_bounds_utc` returns local-midnight bounds (for the daily cap).
  - `test_day_bounds_utc_spans_dst_transition_days` — the daily-cap boundary on transition days: `day_bounds_utc(date(2026,11,1), "America/New_York")` == (2026-11-01T04:00Z, 2026-11-02T05:00Z) — a **25-hour** local day; `day_bounds_utc(date(2026,3,8), "America/New_York")` == (2026-03-08T05:00Z, 2026-03-09T04:00Z) — a **23-hour** local day. A cached-offset bug here double-counts or misses a root at the cap boundary.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_schedule_windows.py -v` — RED: `ModuleNotFoundError: usan_api.schedule_windows`.

- [ ] Step 3: Implement `src/usan_api/schedule_windows.py` (zoneinfo only, no SQL tz math; module docstring cites spec §5.2/§6.3 and the quiet_hours zoneinfo-recompute note, **and states the one deliberate deviation from spec §9 wording: `effective_window` returns `None` for an empty intersection rather than raising — the error contract is preserved one layer up, where schema validators 422 and `next_run_at` raises `ValueError`**):

```python
DAY_NAMES: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")  # bit 0 = Mon

def days_to_mask(days: Sequence[str]) -> int: ...           # ValueError on empty/unknown
def mask_to_days(mask: int) -> list[str]: ...               # ValueError unless 1 <= mask <= 127
def effective_window(start: time, end: time) -> tuple[time, time] | None:
    """Schedule window ∩ quiet hours [09:00, 21:00) — wall clock, tz-invariant."""
def window_bounds_utc(day: date, tz_name: str, *, window_start: time,
                      window_end: time) -> tuple[datetime, datetime]: ...
def day_bounds_utc(day: date, tz_name: str) -> tuple[datetime, datetime]: ...
def local_date(at: datetime, tz_name: str) -> date: ...
def next_run_at(after: datetime, tz_name: str, *, window_start: time, window_end: time,
                days_mask: int) -> datetime:
    """Earliest aware-UTC instant >= after inside the effective window on a masked day.
    Scans <= 8 local dates; builds local wall-clock targets with .replace(...) on a
    zoneinfo-aware datetime then .astimezone(UTC) (never .replace(tzinfo=...)).
    Raises ValueError on unknown tz or empty effective window."""
```

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_schedule_windows.py -v && ruff check src/usan_api/schedule_windows.py && uv run mypy .`

- [ ] Step 5: `git add ... && git commit -m "feat(api): schedule_windows — day-mask codec + quiet-hours intersection + DST-safe next_run_at"`

---

### Task B2: `schemas/schedule.py`

**Files:**
- Create: `apps/api/src/usan_api/schemas/schedule.py`
- Test: `apps/api/tests/test_schedule_schemas.py`

- [ ] Step 1: Write the failing test:
  - `test_create_defaults` — minimal payload (elder_id + "09:00"/"17:00") → `days_of_week == list(DAY_NAMES)`, `enabled is True`, `dynamic_vars == {}`, `profile_override is None`; times parsed to `datetime.time`.
  - `test_create_rejects_empty_days`, `test_create_rejects_unknown_day` (`"monday"`), `test_create_rejects_duplicate_days`, `test_create_rejects_start_not_before_end`, `test_create_rejects_window_outside_quiet_hours` ("21:30"–"22:30" → ValidationError mentioning quiet hours), `test_create_caps_dynamic_vars_at_8kb` (reuses `MAX_DYNAMIC_VARS_BYTES` — assert the constant is imported from `schemas.call`, not redefined).
  - `test_update_all_fields_optional` + `test_update_rejects_half_window` (only `window_start_local` set → ValidationError: window fields travel together on PATCH).
  - `test_schedule_response_from_model_renders_day_list` — fake model object with `days_of_week=65` → `["mon","sun"]`; includes `next_run_at`, `last_materialized_date`, `last_result`, `last_result_at`.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_schedule_schemas.py -v` — RED: module missing.

- [ ] Step 3: Implement:

```python
MAX_SCHEDULES_LIMIT = 500  # bounded list reads, house style (admin_audit precedent)

class CreateScheduleRequest(BaseModel):
    elder_id: uuid.UUID
    window_start_local: time
    window_end_local: time
    days_of_week: list[str] = Field(default_factory=lambda: list(DAY_NAMES))
    enabled: bool = True
    dynamic_vars: dict[str, Any] = Field(default_factory=dict)   # _cap via MAX_DYNAMIC_VARS_BYTES
    profile_override: uuid.UUID | None = None
    # validators: _known_days (non-empty, known, no dups; canonical order),
    # _window_order (start < end), _intersects_quiet_hours (effective_window(...) is not
    # None else "window never intersects quiet hours [09:00, 21:00)").
    @property
    def days_mask(self) -> int: return days_to_mask(self.days_of_week)

class UpdateScheduleRequest(BaseModel):       # all-optional PATCH body
    enabled / window_start_local / window_end_local / days_of_week / dynamic_vars /
    profile_override: ... | None = None
    # model_validator: window fields both-or-neither; if both, order + quiet-hours check;
    # days validated when present. (Merged-state revalidation happens in the router.)

class ScheduleResponse(BaseModel):
    id, elder_id, enabled, window_start_local, window_end_local,
    days_of_week: list[str], dynamic_vars, profile_override, next_run_at,
    last_materialized_date, last_result, last_result_at, created_at, updated_at
    @classmethod
    def from_model(cls, s: CallSchedule) -> "ScheduleResponse": ...  # mask_to_days render
```

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_schedule_schemas.py -v && ruff check . && uv run mypy .`
- [ ] Step 5: `git commit -m "feat(api): schedule request/response schemas with quiet-hours window validation"`

---

### Task B3: `schemas/batch.py` + canonical payload digest

**Files:**
- Create: `apps/api/src/usan_api/schemas/batch.py`
- Test: `apps/api/tests/test_batch_schemas.py`

- [ ] Step 1: Write the failing test:
  - `test_target_caps_and_defaults` — `BatchTargetIn` vars default `{}`, 8 KB cap enforced per target.
  - `test_create_rejects_zero_and_501_targets` (min 1, `MAX_BATCH_TARGETS=500`), `test_create_rejects_long_name` (`MAX_BATCH_NAME_LENGTH=200`), `test_create_rejects_duplicate_elders_with_index` — two targets sharing `elder_id` → ValidationError whose message contains the duplicate `target_index`.
  - `test_create_naive_trigger_at_assumed_utc` — `"2026-06-12T15:00:00"` → `tzinfo == UTC` (precedent: `ScheduleCallbackRequest.requested_at`); aware values pass through.
  - `test_create_window_must_intersect_quiet_hours` — window `{"start_local":"21:30","end_local":"22:30"}` → 422-shaped error.
  - `test_payload_digest_stable_under_key_order` — two requests whose target `dynamic_vars` dicts are constructed in different key orders → identical digest (sorted-key JSON).
  - `test_payload_digest_sensitive_to_target_order_and_content` — swapped targets → different digest; changed `max_concurrency`/`window`/`name`/one var → different digest; **`idempotency_key` itself excluded** (two requests differing only in key → same digest).

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_batch_schemas.py -v` — RED: module missing.

- [ ] Step 3: Implement:

```python
MAX_BATCH_TARGETS = 500     # volume-abuse cap (spec §8); 422 above
MAX_BATCH_NAME_LENGTH = 200 # operator label; PHI-free by convention (spec §8)

class BatchWindow(BaseModel):
    start_local: time; end_local: time
    days_of_week: list[str] | None = None   # None = any day
    # validators: order + quiet-hours intersection + known days

class BatchTargetIn(BaseModel):
    elder_id: uuid.UUID
    dynamic_vars: dict[str, Any] = Field(default_factory=dict)  # 8 KB cap
    profile_override: uuid.UUID | None = None

class CreateBatchRequest(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_BATCH_NAME_LENGTH)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)
    trigger_at: datetime | None = None        # _assume_utc validator (naive -> UTC)
    window: BatchWindow | None = None
    max_concurrency: int | None = Field(default=None, ge=1)
    profile_override: uuid.UUID | None = None
    targets: list[BatchTargetIn] = Field(min_length=1, max_length=MAX_BATCH_TARGETS)
    # model_validator _unique_elders: duplicates -> error naming the offending index

def payload_digest(req: CreateBatchRequest) -> str:
    """sha256 over the canonical sorted-key JSON of (name, trigger_at, window, days,
    max_concurrency, profile_override, ordered targets incl. vars/overrides).
    Strictly stronger than the _idempotent_replay precedent (spec §4.2)."""

class BatchCounts(BaseModel): pending/materialized/done/skipped/cancelled: int = 0
class BatchTargetResponse(BaseModel): target_index, elder_id, status, skip_reason,
    call_id, final_status, materialized_at, finalized_at  (+ from_model)
class BatchSummaryResponse(BaseModel): id, name, status, trigger_at, window fields,
    days_of_week list|None, max_concurrency, profile_override, started_at, completed_at,
    cancelled_at, created_at, counts: BatchCounts  (+ from_model(batch, counts))
class BatchDetailResponse(BatchSummaryResponse):
    final_status_histogram: dict[str, int]; targets: list[BatchTargetResponse]
```

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_batch_schemas.py -v && ruff check . && uv run mypy .`
- [ ] Step 5: `git commit -m "feat(api): batch schemas + canonical sha256 payload digest"`

---

### Task B4: Reserved `sched:`/`batch:` namespace on `CreateCallRequest`

**Files:**
- Modify: `apps/api/src/usan_api/schemas/call.py` (add validator; constant `RESERVED_KEY_PREFIXES`)
- Test: `apps/api/tests/test_reserved_key_namespace.py`

- [ ] Step 1: Write the failing test:
  - `test_create_call_request_rejects_sched_prefix` / `..._rejects_batch_prefix` — `CreateCallRequest(elder_id=..., idempotency_key="sched:xyz")` raises ValidationError whose message names the reserved namespace; `"batch:1"` likewise; `"scheduled-call-1"` (no colon-prefix match) **passes**.
  - `test_enqueue_call_endpoint_422_on_reserved_prefix` — via `client` + `operator_headers`: `POST /v1/calls` with `idempotency_key="batch:0001:1"` → **422** (FastAPI validation, never reaches the DNC lock).

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_reserved_key_namespace.py -v` — RED: validator absent, request validates and endpoint 404s on the unknown elder instead of 422.

- [ ] Step 3: Implement in `schemas/call.py`:

```python
# The materializer owns these prefixes (spec §2.2 invariant 3): a squatted key could
# otherwise suppress or substitute a wellness call (§5.3 step 5 verifies ownership).
RESERVED_KEY_PREFIXES = ("sched:", "batch:")

@field_validator("idempotency_key")
@classmethod
def _reject_reserved_namespace(cls, v: str) -> str:
    if v.startswith(RESERVED_KEY_PREFIXES):
        raise ValueError("idempotency_key prefixes 'sched:'/'batch:' are reserved")
    return v
```

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_reserved_key_namespace.py tests/test_calls.py -v && ruff check . && uv run mypy .`
- [ ] Step 5: `git commit -m "feat(api): reserve sched:/batch: idempotency-key namespace (422 on CreateCallRequest)"`

---

### Task B5: `repositories/call_schedules.py`

**Files:**
- Create: `apps/api/src/usan_api/repositories/call_schedules.py`
- Test: `apps/api/tests/test_call_schedules_repo.py`

- [ ] Step 1: Write the failing test (own `session_factory` fixture + autouse `TRUNCATE call_batch_targets, call_batches, call_schedules, calls, elders CASCADE`, pattern of `test_retry_orchestrator.py`):
  - `test_create_get_and_unique_elder` — second `create_schedule` for the same elder raises `IntegrityError` (UNIQUE elder_id; the router maps it to 409).
  - `test_claim_due_schedules_orders_and_skips` — three schedules (due-old, due-new, future) → `claim_due_schedules(db, now=NOW, limit=10)` returns the two due ordered by `next_run_at`; disabled schedule never claimed (`WHERE enabled`, `idx_call_schedules_due` predicate).
  - `test_claim_skip_locked_disjoint` — two concurrent sessions (second engine, pattern of `test_claim_skips_locked_rows`) claim disjoint rows.
  - `test_record_result_writes_bookkeeping` — `record_result(..., result="skipped_window", next_run_at=..., last_materialized_date=...)` updates `last_result`, `last_result_at`, advances `next_run_at`; `enabled=False` kwarg disables (the DNC auto-disable write path).
  - `test_list_filters_last_result_and_elder` — `last_result="skipped_window"` filter returns only matching rows, newest-first with `id` tiebreaker, clamped to `MAX_SCHEDULES_LIMIT`.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_call_schedules_repo.py -v` — RED: module missing.

- [ ] Step 3: Implement (async module functions, flush+refresh, **never commit**):

```python
async def create_schedule(db, *, elder_id, window_start_local, window_end_local,
                          days_of_week: int, enabled: bool, dynamic_vars: dict[str, Any],
                          profile_override: uuid.UUID | None,
                          next_run_at: datetime) -> CallSchedule
async def get_schedule(db, schedule_id) -> CallSchedule | None
async def get_by_elder(db, elder_id) -> CallSchedule | None
async def list_schedules(db, *, elder_id=None, last_result=None,
                         limit=100, offset=0) -> list[CallSchedule]   # clamp 1..500
async def delete_schedule(db, schedule: CallSchedule) -> None
async def claim_due_schedules(db, *, now: datetime, limit: int) -> list[CallSchedule]
    # SELECT ... WHERE enabled AND next_run_at <= now ORDER BY next_run_at LIMIT ...
    # .with_for_update(skip_locked=True)  (idx_call_schedules_due exact predicate)
async def record_result(db, schedule: CallSchedule, *, result: str, now: datetime,
                        next_run_at: datetime | None = None,
                        last_materialized_date: date | None = None,
                        enabled: bool | None = None) -> None
```

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_call_schedules_repo.py -v && ruff check . && uv run mypy .`
- [ ] Step 5: `git commit -m "feat(api): call_schedules repository (SKIP LOCKED claim + last_result bookkeeping)"`

---

### Task B6: `repositories/call_batches.py` (batches + targets, status-guarded transitions)

**Files:**
- Create: `apps/api/src/usan_api/repositories/call_batches.py`
- Test: `apps/api/tests/test_call_batches_repo.py`

- [ ] Step 1: Write the failing test (same fixture pattern as B5):
  - `test_create_batch_with_targets_one_flush` — 3 targets inserted with `target_index` 0..2, all `pending`; `uq_call_batch_targets_idx` violated on duplicate index → `IntegrityError`.
  - `test_get_by_idempotency_key`.
  - `test_trigger_due_batches_guarded` — `scheduled` + `trigger_at<=now` → `running` + `started_at`; `trigger_at IS NULL` also triggers; future `trigger_at` and non-`scheduled` rows untouched.
  - `test_claim_next_pending_target_order_and_throttle` — targets claimed in `(batch_id, target_index)` order, SKIP LOCKED; a batch with `max_concurrency=1` and one already-`materialized` (unfinalized) target is **passed over** while another batch's targets are still served (gate-starvation/§9 `max_concurrency` pass-over).
  - `test_claim_next_pending_target_skip_locked_under_open_txn` — open-transaction interleaving (pattern of `test_claim_skips_locked_rows`): session A on a second engine claims target 0 and **holds its transaction open**; session B's `claim_next_pending_target` returns target 1 without blocking; after A rolls back, target 0 is claimable again. (The §9 SKIP LOCKED race at the target level — sequential claims prove nothing.)
  - `test_target_transitions_are_status_guarded` — `mark_target_materialized` on a `cancelled` target is a no-op returning `False`; `mark_target_skipped` only from `pending`; `finalize_target` only from `materialized`.
  - `test_cancel_batch_marks_pending_cancelled_and_is_guarded` — cancel flips batch + pending targets; second cancel returns the batch unchanged (idempotent); `completed` batch refuses (`ValueError` → router 409).
  - `test_open_batches_and_complete_drained` — running batch with all targets in `{done, skipped, cancelled}` → `complete_drained_batches` stamps `completed`+`completed_at`; **cancelled batch with zero open targets gets `completed_at` stamped while status stays `cancelled`** and then disappears from `open_batches` (the `idx_call_batches_open` exit condition — §9 drained-cancelled bookkeeping).
  - `test_list_batches_clamped_and_ordered` — `list_batches` clamps `limit` to ≤500 (spec §8 bounded reads), newest-first with `id` tiebreaker.
  - `test_target_counts_and_histogram` — counts dict has all five statuses; histogram aggregates `final_status`.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_call_batches_repo.py -v` — RED: module missing.

- [ ] Step 3: Implement:

```python
async def create_batch_with_targets(db, *, name, idempotency_key, payload_digest,
        trigger_at, window_start_local, window_end_local, days_of_week: int | None,
        max_concurrency, profile_override,
        targets: Sequence[BatchTargetIn]) -> CallBatch          # one flush, no commit
async def get_batch(db, batch_id) -> CallBatch | None
async def get_by_idempotency_key(db, key: str) -> CallBatch | None
async def list_batches(db, *, status=None, limit=100, offset=0) -> list[CallBatch]
    # clamp 1..500; newest-first, id tiebreaker
async def list_targets(db, batch_id) -> list[CallBatchTarget]   # ORDER BY target_index
async def target_counts(db, batch_id) -> dict[str, int]
async def final_status_histogram(db, batch_id) -> dict[str, int]
async def trigger_due_batches(db, *, now, limit) -> list[CallBatch]    # SKIP LOCKED
async def claim_next_pending_target(db) -> CallBatchTarget | None
    # JOIN call_batches status='running'; pending only; ORDER BY (batch_id, target_index)
    # LIMIT 1 FOR UPDATE OF call_batch_targets SKIP LOCKED; correlated scalar subquery
    # count(status='materialized') < max_concurrency (NULL = unthrottled).
    # NOTE (deliberate, benign deviation from spec §5.2 wording): the spec throttles on
    # "unsettled targets' in-flight chains" (via idx_call_batch_targets_call); counting
    # status='materialized' is equivalent ONLY because phase 1 (finalizer) runs before
    # phase 4 every cycle and settles drained chains to done/skipped. If the phase
    # order ever changes, revisit this throttle.
async def list_materialized_targets(db, batch_id) -> list[CallBatchTarget]
async def mark_target_materialized(db, target, *, call_id, now) -> bool   # pending only
async def mark_target_skipped(db, target, *, reason: str, now) -> bool    # pending only
async def finalize_target(db, target, *, final_status: str, now) -> bool  # materialized only
async def cancel_batch(db, batch, *, now) -> list[uuid.UUID]
    # batch -> cancelled+cancelled_at (guarded: ValueError if completed; no-op if
    # already cancelled); pending targets -> cancelled; returns materialized targets'
    # root call_ids for the caller's guarded chain-tip cancel.
async def open_batches(db, *, limit) -> list[CallBatch]
    # status IN (running, cancelled) AND completed_at IS NULL (idx_call_batches_open)
async def complete_drained_batches(db, *, now) -> list[CallBatch]
```

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_call_batches_repo.py -v && ruff check . && uv run mypy .`
- [ ] Step 5: `git commit -m "feat(api): call_batches repository — guarded target lifecycle + throttled claim + drained-cancel bookkeeping"`

---

## Part C — Operator API (`/v1/schedules`, `/v1/batches`), rate-limit allowlist, audit, provenance

### Task C1: `_is_operator_route` gains `/v1/schedules` + `/v1/batches`

**Files:**
- Modify: `apps/api/src/usan_api/ratelimit.py` (`_is_operator_route`, line 41–48)
- Test: `apps/api/tests/test_app_security.py` (append)

- [ ] Step 1: Write the failing test (precedent: existing ratelimit tests in the same file):

```python
def test_is_operator_route_matches_schedules_and_batches():
    from usan_api.ratelimit import _is_operator_route
    assert _is_operator_route("POST", "/v1/schedules")
    assert _is_operator_route("GET", "/v1/schedules/123")
    assert _is_operator_route("PATCH", "/v1/schedules/123")
    assert _is_operator_route("POST", "/v1/batches")
    assert _is_operator_route("POST", "/v1/batches/123/cancel")
    assert _is_operator_route("GET", "/v1/batches")

def test_schedules_and_batches_routes_throttled_pre_auth(monkeypatch):
    # mirror test_admin_routes_are_rate_limited: 2/minute budget, no auth header;
    # flood GET /v1/schedules then POST /v1/batches -> 429 appears after the budget.
```

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_app_security.py -v` — RED: `_is_operator_route` returns False for both prefixes (middleware never matches → no 429; without this the new endpoints ship completely unthrottled — spec §4/§8 HIGH finding).

- [ ] Step 3: Implement — add to `_is_operator_route` (and extend its docstring):

```python
    if path.startswith("/v1/schedules") or path.startswith("/v1/batches"):
        return True
```

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_app_security.py -v && ruff check src/usan_api/ratelimit.py && uv run mypy .`
- [ ] Step 5: `git commit -m "fix(api): rate-limit allowlist covers /v1/schedules and /v1/batches (pre-auth)"`

---

### Task C2: `is_live_profile` override validation helper

**Files:**
- Modify: `apps/api/src/usan_api/repositories/agent_profiles.py` (append)
- Test: `apps/api/tests/test_agent_profiles_repo.py` (append)

- [ ] Step 1: Write the failing test — `test_is_live_profile_requires_active_and_published`: unpublished ACTIVE profile → `False`; published ACTIVE → `True`; published-then-archived → `False`; unknown UUID → `False`. (Rationale: `resolve_agent_config` silently falls through otherwise — the operator would believe an override is live when it is not; spec §4.)

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_agent_profiles_repo.py -v` — RED: `AttributeError: is_live_profile`.

- [ ] Step 3: Implement:

```python
async def is_live_profile(db: AsyncSession, profile_id: uuid.UUID) -> bool:
    """True iff the profile exists, is ACTIVE, and has a published version —
    the precondition for profile_override to actually take effect (spec §4)."""
    profile = await get_profile(db, profile_id)
    return (profile is not None and profile.status is ProfileStatus.ACTIVE
            and profile.published_version is not None)
```

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_agent_profiles_repo.py -v && uv run mypy .`
- [ ] Step 5: `git commit -m "feat(api): is_live_profile helper for profile_override validation"`

---

### Task C3: `routers/schedules.py` + `main.py` registration

**Files:**
- Create: `apps/api/src/usan_api/routers/schedules.py`
- Modify: `apps/api/src/usan_api/main.py` (import + `include_router` after `dnc`)
- Test: `apps/api/tests/test_schedules_api.py`

- [ ] Step 1: Write the failing test (uses `client` + `operator_headers`; seed elders via `POST /v1/elders`):
  - `test_create_schedule_201_computes_next_run_at` — elder tz `America/New_York`, window 09:00–17:00 → 201; `next_run_at` parses as aware UTC; `days_of_week` echoed as string list.
  - `test_create_schedule_404_unknown_elder`; `test_create_schedule_409_second_schedule_same_elder`.
  - `test_create_schedule_422_invalid_elder_timezone` — elder seeded with tz `"Mars/Olympus"` → 422 (zoneinfo fail-closed at create).
  - `test_create_schedule_422_window_outside_quiet_hours`; `test_create_schedule_422_unpublished_profile_override` (uses C2 helper).
  - `test_list_schedules_filters_last_result` — write `last_result` via repo, `GET /v1/schedules?last_result=skipped_window` returns only the miss ("who missed today's call" view, spec §4.1).
  - `test_get_schedule_200_and_404`.
  - `test_patch_recomputes_next_run_at_and_revalidates` — PATCH window → 200 with changed `next_run_at`; PATCH that makes the merged window quiet-hours-empty → 422; PATCH `enabled=false` pauses.
  - `test_patch_window_422_when_elder_timezone_went_bad` — elder `timezone` is only length-validated at the elder API boundary, so it can go bad after schedule creation: corrupt it via direct `UPDATE elders SET timezone='Mars/Olympus'`, then PATCH the window → **422** (the `ValueError` from the `next_run_at` recompute is mapped), not 500.
  - `test_delete_schedule_204_then_404`.
  - `test_mutations_write_audit_log_lines` — loguru capture (`logger.add(sink)` pattern): create/patch/delete each emit a record bound with `client`, `schedule_id`, `action`; **no record binds elder `name` or `dynamic_vars`**.
  - `test_schedules_require_operator_token` — no bearer → 401 on every method.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_schedules_api.py -v` — RED: 404 (router unregistered).

- [ ] Step 3: Implement `routers/schedules.py`:

```python
router = APIRouter(prefix="/v1/schedules", tags=["schedules"],
                   dependencies=[Depends(require_operator_token)])
# Router docstring: dynamic_vars here are LIVE re-used config exempt from retention;
# the operator PHI-removal path is PATCH (clear vars) or DELETE (spec §8).

@router.post("", status_code=201, response_model=ScheduleResponse)
async def create_schedule(body: CreateScheduleRequest, request: Request,
                          db=Depends(get_db)) -> ScheduleResponse:
    # 404 elder; 409 get_by_elder pre-check (+ IntegrityError fallback -> 409);
    # 422 invalid tz (schedule_windows.next_run_at raises ValueError);
    # 422 profile_override not is_live_profile;
    # next_run_at = schedule_windows.next_run_at(now, elder.timezone,
    #     window_start=..., window_end=..., days_mask=body.days_mask)
    # repo create -> await db.commit() -> audit log:
    # logger.bind(client=client_ip(request), schedule_id=str(s.id),
    #             elder_id=str(body.elder_id), action="schedule_created").info(
    #     "Schedule created")          # ids only — never name/vars (spec §8)

@router.get("", response_model=list[ScheduleResponse])      # ?elder_id=&last_result=&limit=&offset=
@router.get("/{schedule_id}", ...)                          # 200/404
@router.patch("/{schedule_id}", ...)                        # merge -> revalidate merged window
                                                            # (effective_window non-empty) ->
                                                            # recompute next_run_at (ValueError
                                                            # from a bad elder tz -> 422) ->
                                                            # commit -> audit
@router.delete("/{schedule_id}", status_code=204)           # 204/404; commit; audit
```

  Register in `main.py`: add `schedules` to the router import block (alphabetical) and `app.include_router(schedules.router)` after `dnc`.

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_schedules_api.py tests/test_app_security.py -v && ruff check . && uv run mypy .`
- [ ] Step 5: `git commit -m "feat(api): /v1/schedules operator CRUD with next_run_at compute + audit logs"`

---

### Task C4: `routers/batches.py` + chain-tip cancel helpers + `main.py` registration

**Files:**
- Create: `apps/api/src/usan_api/routers/batches.py`
- Modify: `apps/api/src/usan_api/repositories/calls.py` (append — **first of seven sequential edits to this file**)
- Modify: `apps/api/src/usan_api/main.py` (import + include after `schedules`)
- Test: `apps/api/tests/test_batches_api.py`

- [ ] Step 1: Write the failing test:
  - `test_create_batch_201_inserts_targets_one_txn` — 3 targets → 201; `GET /v1/batches/{id}` shows 3 `pending` targets ordered by `target_index`.
  - `test_create_batch_422_unknown_elder_with_target_detail` — one bad elder among 3 → 422 with `detail == [{"target_index": 1, "error": "elder not found"}]` (all-or-nothing: no batch row persisted).
  - `test_create_batch_422_per_target_vars_cap`, `test_create_batch_422_duplicate_elder`, `test_create_batch_422_bad_profile_override_batch_and_target` (detail names `"batch"` or the index).
  - `test_create_batch_replay_200_same_digest` — same `idempotency_key` + identical payload → **200** with the existing batch id; `test_create_batch_409_same_key_different_digest` — one var changed → **409** ("digest-replay divergence", §9).
  - `test_create_batch_replay_after_cancel_returns_cancelled_batch` — same key + identical digest re-POSTed **after** `POST /{id}/cancel` → **200** with the *cancelled* batch (digest replay deliberately ignores status; this pins the behavior so nobody "fixes" it into a silent re-run).
  - `test_list_batches_counts_and_status_filter`; `test_get_batch_detail_histogram`.
  - `test_cancel_batch_200_marks_pending_and_queued_roots` — seed via repos: one `pending` target, one `materialized` target whose root call is `QUEUED` (`scheduled_at` set), one `materialized` whose root is `IN_PROGRESS` → `POST .../cancel` → 200; pending target `cancelled`; QUEUED root flipped `CANCELLED` (first writer of the enum value); **IN_PROGRESS call untouched** (§5.6: in-flight finishes naturally).
  - `test_cancel_batch_cancels_chain_tip_not_root` — a fourth `materialized` target whose root is `NO_ANSWER` (attempt 1) with a **QUEUED retry child** → cancel flips the **child** (the chain tip) to CANCELLED and leaves the root untouched — this exercises `get_chain_tip`'s hop walk, which is otherwise dead code under test (§5.6 "latest attempt").
  - `test_cancel_idempotent_200_and_completed_409`.
  - `test_batch_mutations_write_audit_log_lines` — loguru capture: create and cancel each emit a record bound with `client`, `batch_id`, `action` (positive assertion, §9 "audit records written for each mutation").
  - `test_batch_log_lines_never_bind_name` — loguru capture across create+cancel: no log record carries a `name` key or the batch name string (spec §8 PHI-by-convention pin).
  - `test_batches_require_operator_token`.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_batches_api.py -v` — RED: 404.

- [ ] Step 3: Implement. Append to `repositories/calls.py`:

```python
async def get_chain_tip(db: AsyncSession, root_call_id: uuid.UUID) -> Call | None:
    """Latest attempt of a retry chain: follow child rows via parent_call_id (<=3 hops;
    one-child-max via uq_calls_parent_call_id makes each probe a single indexed lookup)."""
async def cancel_queued_tips(db: AsyncSession, root_call_ids: Sequence[uuid.UUID]) -> int:
    """Guarded UPDATE: each chain's tip queued -> cancelled. Never touches dialing/
    in_progress rows (spec §5.6). Returns rows flipped."""
```

  `routers/batches.py`:

```python
router = APIRouter(prefix="/v1/batches", tags=["batches"],
                   dependencies=[Depends(require_operator_token)])
# Router docstring: `name` is PHI-free BY CONVENTION — never type an elder's name into
# it; it is never bound into log lines (spec §8).

@router.post("", status_code=201, response_model=BatchSummaryResponse)
async def create_batch(body: CreateBatchRequest, request: Request, response: Response,
                       db=Depends(get_db)):
    digest = payload_digest(body)
    if body.idempotency_key:
        existing = await call_batches_repo.get_by_idempotency_key(db, body.idempotency_key)
        if existing is not None:
            if existing.payload_digest != digest:
                raise HTTPException(409, "idempotency_key reused with a different payload")
            response.status_code = 200; return ...existing summary...
    # all-or-nothing validation: one SELECT for all target elder_ids; missing ->
    # 422 detail=[{target_index, error}]; per-target+batch profile_override via
    # is_live_profile -> 422 detail. Then create_batch_with_targets -> commit -> audit:
    # logger.bind(client=client_ip(request), batch_id=str(b.id),
    #             targets=len(body.targets), action="batch_created").info("Batch created")

@router.get("", ...)                 # ?status=&limit=&offset= summary + counts
@router.get("/{batch_id}", ...)      # detail: counts + histogram + ordered targets
@router.post("/{batch_id}/cancel", response_model=BatchSummaryResponse)
async def cancel_batch(batch_id, request, db=Depends(get_db)):
    # 404; status=='completed' -> 409; already cancelled -> 200 unchanged (idempotent).
    # ONE transaction: repo.cancel_batch(...) -> calls_repo.cancel_queued_tips(root_ids)
    # -> commit -> audit log (ids + counts only, action="batch_cancelled").
```

  Register both in `main.py`.

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_batches_api.py -v && ruff check . && uv run mypy .`
- [ ] Step 5: `git commit -m "feat(api): /v1/batches create/list/detail/cancel with digest replay + guarded chain-tip cancel"`

---

### Task C5: Derived `origin` provenance on `CallResponse`

**Files:**
- Modify: `apps/api/src/usan_api/schemas/call.py` (sequential after B4)
- Test: `apps/api/tests/test_call_origin.py`

- [ ] Step 1: Write the failing test:
  - `test_parse_origin_schedule_key` — `parse_origin(f"sched:{u}:2026-06-10")` → `CallOrigin(source="schedule", id=UUID(u), ordinal="2026-06-10")`.
  - `test_parse_origin_batch_key` — `parse_origin(f"batch:{u}:7")` → `source="batch"`, `ordinal=7` (int).
  - `test_parse_origin_none_for_operator_keys_and_garbage` — `None` for `None`, `"daily-2026"`, `"sched:notauuid:x"`, `"batch:{u}"` (missing ordinal) — malformed keys degrade to `None`, never raise.
  - `test_get_call_response_includes_origin` — via `client`: a call created through the repo with a `batch:` key → `GET /v1/calls/{id}` body has `origin == {"source":"batch","id":...,"ordinal":0}`; an enqueue-created call has `origin is None`; retry children (no key) → `None` (chain walk via `parent_call_id` is the documented provenance, §4.3).

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_call_origin.py -v` — RED: `parse_origin`/`CallOrigin` missing; response lacks the field.

- [ ] Step 3: Implement in `schemas/call.py`:

```python
class CallOrigin(BaseModel):
    source: Literal["schedule", "batch"]
    id: uuid.UUID
    ordinal: str | int          # local_date for schedules, target_index for batches

def parse_origin(idempotency_key: str | None) -> CallOrigin | None:
    """Derived, read-only provenance from the materializer's reserved key namespace
    (spec §4.3). Malformed values return None — never raise on stored data."""
```
  Add `origin: CallOrigin | None = None` to `CallResponse` and compute it in `from_model` via `parse_origin(call.idempotency_key)`.

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_call_origin.py tests/test_calls.py tests/test_calls_lifecycle.py -v && ruff check . && uv run mypy .`
- [ ] Step 5: `git commit -m "feat(api): derived origin provenance on CallResponse from reserved key namespace"`

---

### Task C6: Part C gate

- [ ] Step 4: `cd apps/api && uv run pytest -v && ruff check . && ruff format --check . && uv run mypy .` — green before Part D.

---

## Part D — Scheduler poller, materializer, concurrency gate, dial-path hardenings

### Task D1: Settings — 8 keys + cross-field validator *(moved from Part E: hard dependency, see Executor note 1)*

**Files:**
- Modify: `apps/api/src/usan_api/settings.py` (new grouped block after the Telnyx Messaging block)
- Test: `apps/api/tests/test_settings_scheduler.py`

- [ ] Step 1: Write the failing test (pattern: `test_settings_messaging.py`):
  - `test_scheduler_defaults_are_inert` — fresh `Settings(**base_env)`: `scheduler_poller_enabled is False`, `concurrency_gate_enabled is False`, `autonomous_dialing_paused is False`, `scheduler_poll_interval_s == 60`, `scheduler_batch_size == 50`, `max_concurrent_calls == 8`, `reserved_concurrency == 2`, `max_autonomous_calls_per_elder_per_day == 2` (ship-inert contract, spec §5.1/§10.1).
  - `test_scheduler_bounds_enforced` — `SCHEDULER_POLL_INTERVAL_S=10` (lt 15), `=601`, `SCHEDULER_BATCH_SIZE=0`, `MAX_CONCURRENT_CALLS=51`, `MAX_AUTONOMOUS_CALLS_PER_ELDER_PER_DAY=0` each raise.
  - `test_reserved_must_be_below_max` — `RESERVED_CONCURRENCY=8, MAX_CONCURRENT_CALLS=8` → ValidationError naming both fields.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_settings_scheduler.py -v` — RED: `AttributeError`/extra-ignored fields.

- [ ] Step 3: Implement the eight fields exactly per spec §5.1 table (UPPERCASE aliases, bounds, grouped comment block citing the e2-standard-4 sizing + measure-before-raising lesson and the daily-cap rationale), plus:

```python
@model_validator(mode="after")
def _reserved_below_max(self) -> "Settings":
    # The gate computes max - reserved - in_flight; reserved >= max means the
    # autonomous planes can never dial (spec §5.1).
    if self.reserved_concurrency >= self.max_concurrent_calls:
        raise ValueError("RESERVED_CONCURRENCY must be < MAX_CONCURRENT_CALLS")
    return self
```

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_settings_scheduler.py tests/test_settings.py -v && uv run mypy .`
- [ ] Step 5: `git commit -m "feat(api): scheduler/gate settings (8 keys, inert defaults, reserved<max validator)"`

---

### Task D2: `schedule_retry` copies `profile_override` to the child

**Files:**
- Modify: `apps/api/src/usan_api/repositories/calls.py` (sequential after C4)
- Test: `apps/api/tests/test_retry_scheduling.py` (append)

- [ ] Step 1: Write the failing test — `test_schedule_retry_child_inherits_profile_override`: parent `NO_ANSWER` attempt 1 with `profile_override=<published profile uuid>` → `schedule_retry` child has the same `profile_override` (and, regression, the same `dynamic_vars`). Rationale: attempts 2..n currently silently revert to the default profile while `profile_override` is live (runtime agent-config + SMS template resolution) — spec §2.3(3)/§6.1.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_retry_scheduling.py -v` — RED: child `profile_override is None`.

- [ ] Step 3: Implement — one line in the `Call(` child construction in `schedule_retry`: `profile_override=parent.profile_override,`.

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_retry_scheduling.py tests/test_retry_orchestrator.py -v && uv run mypy .`
- [ ] Step 5: `git commit -m "fix(api): schedule_retry propagates profile_override to retry children"`

---

### Task D3: `schedule_retry` batch-cancellation awareness (primary cancel guard)

**Files:**
- Modify: `apps/api/src/usan_api/repositories/calls.py` (sequential after D2)
- Test: `apps/api/tests/test_retry_scheduling.py` (append)

- [ ] Step 1: Write the failing test:
  - `test_schedule_retry_suppressed_for_cancelled_batch_chain` — create a batch via repo, target linked to a root call (`batch:{id}:0` key), cancel the batch (status `cancelled`), mark the root FAILED → `schedule_retry(db, root.id)` returns **None**, no child row exists. This is the §5.6/§9 race the sweep alone would lose (FAILED children are born at +1m; the retry poller claims every 30 s; the sweep may be up to 600 s away) — the guard must live **in the same commit as the parent's terminal transition**, which this placement provides.
  - `test_schedule_retry_suppressed_for_grandchild_of_cancelled_batch` — chain root→child(attempt 2, FAILED) → no attempt-3 child (≤3-hop root walk).
  - `test_schedule_retry_unaffected_for_running_batch_and_sched_roots` — running batch chain still retries; a `sched:`-keyed root still retries (the check does not apply to schedule chains, §5.6).

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_retry_scheduling.py -v` — RED: a FAILED(+1m) child is created for the cancelled batch.

- [ ] Step 3: Implement in `schedule_retry`, after the elder/delay checks and before child construction:

```python
# Batch-cancellation guard (spec §5.6): walk parent_call_id to the chain root
# (<=3 hops); if the root is batch-owned and its batch is cancelled, never create
# a child — in the SAME commit as the parent's terminal transition, so the
# scheduler-cycle sweep is only a backstop for the cancel-vs-transition race.
root = parent
for _ in range(3):
    if root.parent_call_id is None:
        break
    nxt = await db.get(Call, root.parent_call_id)
    if nxt is None:
        break
    root = nxt
if root.idempotency_key and root.idempotency_key.startswith("batch:"):
    result = await db.execute(
        select(CallBatch.status)
        .join(CallBatchTarget, CallBatchTarget.batch_id == CallBatch.id)
        .where(CallBatchTarget.call_id == root.id)      # idx_call_batch_targets_call
    )
    if result.scalar_one_or_none() == "cancelled":
        logger.bind(call_id=str(call_id)).info("Retry suppressed: batch cancelled")
        return None
```
  (Import `CallBatch`, `CallBatchTarget` at module top.)

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_retry_scheduling.py tests/test_batches_api.py -v && uv run mypy .`
- [ ] Step 5: `git commit -m "feat(api): schedule_retry is batch-cancellation-aware (suppresses post-cancel children at the source)"`

---

### Task D4: `dispatch_and_dial` elder-missing FAILED guard (kills the DIALING↔QUEUED ping-pong)

**Files:**
- Modify: `apps/api/src/usan_api/livekit_dispatch.py` (lines 312–315 guard)
- Test: `apps/api/tests/test_dispatch_and_dial.py` (append)

- [ ] Step 1: Write the failing test — `test_dispatch_and_dial_marks_elder_missing_failed_not_silent` (fixtures/fakes per existing file): seed a DIALING retry row, then `UPDATE calls SET elder_id = NULL` (simulating `ON DELETE SET NULL`); run `dispatch_and_dial` → call is **FAILED** with `end_reason == "elder_missing"`, `ended_at` set — NOT still DIALING (today the guard returns with no status write, so `reclaim_stuck_dialing` re-queues it forever, pinning an in-flight slot — spec §2.3(2)). Also `test_dispatch_and_dial_missing_room_marks_failed` (same guard, `livekit_room=None`). Assert no retry child (elder gone ⇒ `schedule_retry` → None ⇒ chain settles, §9 dispatch-guard).

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_dispatch_and_dial.py -v` — RED: status stays DIALING.

- [ ] Step 3: Implement — split the first guard:

```python
call = await calls_repo.get_call(db, call_id)
if call is None:
    logger.bind(call_id=str(call_id)).warning("dispatch_and_dial: call not found")
    return
if call.elder_id is None or not call.livekit_room:
    # ON DELETE SET NULL leaves the row DIALING forever otherwise: reclaim_stuck_dialing
    # re-queues it, the poller re-claims it — an infinite loop pinning one in-flight
    # slot (spec §2.3). Fail it terminally; schedule_retry refuses elder-less parents,
    # so the chain settles here.
    await calls_repo.mark_dial_failure(db, call_id, CallStatus.FAILED,
                                       end_reason="elder_missing")
    await db.commit()
    logger.bind(call_id=str(call_id)).warning("dispatch_and_dial: elder missing; FAILED")
    return
```
  (The pre-existing `elder is None` branch below is now the unreachable-given-FK belt — keep it, it already writes the same outcome.)

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_dispatch_and_dial.py -v && uv run mypy .`
- [ ] Step 5: `git commit -m "fix(api): dispatch_and_dial marks elder-less calls FAILED(elder_missing) — no DIALING ping-pong"`

---

### Task D5: Dial-time quiet-hours re-check (TCPA) in `dispatch_and_dial`

**Files:**
- Modify: `apps/api/src/usan_api/livekit_dispatch.py` (sequential after D4; add `_utcnow()` seam)
- Modify: `apps/api/src/usan_api/repositories/calls.py` (sequential after D3 — add `requeue_for_quiet_hours`)
- Test: `apps/api/tests/test_dispatch_and_dial.py` (append)

- [ ] Step 1: Write the failing test:
  - `test_dispatch_and_dial_requeues_outside_quiet_hours` — elder tz `America/New_York`; monkeypatch `livekit_dispatch._utcnow` to `2026-06-10T03:00:00Z` (23:00 EDT — a clamp that has gone stale); run `dispatch_and_dial` on a DIALING row → row flipped **back to QUEUED** with `scheduled_at == quiet_hours.next_allowed(NOW, tz)` (13:00Z), **`dispatch_agent` never called** (assert on the fake), no SIP participant created. (§2.3(1)/§6.3(3): a clamp is a promise about the past — never dial on a stale clamp; gate-induced waiting makes this load-bearing.)
  - `test_dispatch_and_dial_inside_quiet_hours_proceeds` — `_utcnow` at 16:00Z (12:00 EDT) → dial proceeds (regression guard).
  - `test_dispatch_and_dial_invalid_tz_fails_closed` — elder tz `"Not/AZone"` → call FAILED `end_reason == "invalid_timezone"`, no dial, no child (schedule_retry independently refuses invalid-tz — chain settles).

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_dispatch_and_dial.py -v` — RED: the dial proceeds at 23:00 elder-local.

- [ ] Step 3: Implement. In `repositories/calls.py`:

```python
async def requeue_for_quiet_hours(db: AsyncSession, call_id: uuid.UUID,
                                  *, scheduled_at: datetime) -> Call | None:
    """Flip a claimed DIALING row back to QUEUED with a fresh clamp (dial-time
    quiet-hours re-check, spec §2.3). Guarded on DIALING so it never clobbers an
    outcome written by a racing webhook."""
```

  In `livekit_dispatch.py`: add `def _utcnow() -> datetime: return datetime.now(UTC)` (module-level seam for tests); in `dispatch_and_dial`, **immediately after the `room = call.livekit_room` assignment** (which follows the elder load and the D4 guard) **and before `dnc_repo.lock_phone`** — so the re-queue/fail paths never hold the advisory lock and `room` is in scope for `_delete_room`:

```python
now = _utcnow()
try:
    allowed = quiet_hours.next_allowed(now, elder.timezone)
except ValueError:
    await calls_repo.mark_dial_failure(db, call_id, CallStatus.FAILED,
                                       end_reason="invalid_timezone")
    await db.commit()
    logger.bind(call_id=str(call_id)).error(
        "Dial blocked: elder timezone invalid; call FAILED (fail closed)")
    await _delete_room(room, settings)
    return
if allowed > now:
    await calls_repo.requeue_for_quiet_hours(db, call_id, scheduled_at=allowed)
    await db.commit()
    logger.bind(call_id=str(call_id)).warning(
        "Dial outside quiet hours; re-queued with fresh clamp")
    return
```
  (Metric `usan_dial_requeued_total{reason="quiet_hours"}` is wired in **E1** — do not add it here.) Imports: `quiet_hours`, `UTC`/`datetime`.

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_dispatch_and_dial.py tests/test_livekit_dispatch.py -v && uv run mypy .`
- [ ] Step 5: `git commit -m "feat(api): dial-time quiet-hours re-check — re-queue on stale clamp, fail closed on bad tz"`

---

### Task D6: Global concurrency gate in `retry_orchestrator.poll_once` (count+claim, one txn)

**Files:**
- Modify: `apps/api/src/usan_api/repositories/calls.py` (sequential after D5 — add `count_in_flight`, `count_queued_due`)
- Modify: `apps/api/src/usan_api/retry_orchestrator.py`
- Test: `apps/api/tests/test_concurrency_gate.py` (new; own `session_factory` + truncate fixtures, `_settings()` helper per `test_retry_orchestrator.py`)

- [ ] Step 1: Write the failing test (flag matrix per §9):
  - `test_count_in_flight_recency_bounded` — seed DIALING/RINGING/IN_PROGRESS rows; one IN_PROGRESS with `updated_at` forced older than `outbound_max_call_duration_s + 120` → excluded from the count (**a wedged row stops consuming a slot — without the bound eight wedged rows would silently halt ALL autonomous dialing**, §5.4(1)).
  - `test_gate_shrinks_claim_limit` — `CONCURRENCY_GATE_ENABLED=true, MAX_CONCURRENT_CALLS=5, RESERVED_CONCURRENCY=2`, 2 fresh in-flight rows, 4 due QUEUED rows → `poll_once` claims exactly **1** (5−2−2).
  - `test_gate_zero_slots_claims_nothing` — 3 in-flight → claims `[]`, due rows stay QUEUED (claim skipped entirely at 0).
  - `test_paused_claims_nothing_preserving_state` — `AUTONOMOUS_DIALING_PAUSED=true` (gate on or off) → zero claims, rows untouched, a WARNING log emitted (loguru capture) — the reversible emergency stop, distinct from `RETRY_POLLER_ENABLED=false` (§5.4(3)).
  - `test_gate_disabled_is_bit_identical_to_today` — gate off + in-flight rows present → claims `min(retry_batch_size, all_due)` exactly as the pre-gate code (ship-inert proof, §5.1/§10.1).
  - `test_count_and_claim_share_one_transaction` — monkeypatch-spy: `count_in_flight` and `claim_due_retries` receive the **same session object** (no avoidable intra-process drift, §5.4).

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_concurrency_gate.py -v` — RED: `count_in_flight` missing; claims ignore in-flight.

- [ ] Step 3: Implement. `repositories/calls.py`:

```python
async def count_in_flight(db: AsyncSession, *, now: datetime, max_age_s: int) -> int:
    """Recency-bounded dial-slot consumers (idx_calls_in_flight). Rows older than the
    LiveKit max_call_duration ceiling are wedged (lost webhook) and must not consume
    a slot forever (spec §5.4)."""
    cutoff = now - timedelta(seconds=max_age_s)
    ...select(func.count()).where(Call.status.in_((DIALING, RINGING, IN_PROGRESS)),
                                  Call.updated_at > cutoff)...

async def count_queued_due(db: AsyncSession, *, now: datetime) -> int:
    """COUNT(*) status='queued' AND scheduled_at <= now — idx_calls_due_retries exact
    predicate; feeds the scheduler's batch-materialization slot math (spec §5.2 ph.4)."""
```

  `retry_orchestrator.py` — replace the claim block (count + claim in **one** session/transaction); extend the module docstring with the single-replica count-then-claim overshoot caveat next to the trunk-provisioning note; update the `poll_once` docstring (`scheduled_at IS NOT NULL` now means "poller-owned row", §2.2 invariant 2 — also update the `reclaim_stuck_dialing` docstring; the `idx_calls_due_retries` comment in `0003` is documented in the orchestrator, not edited):

```python
async with factory() as db:
    in_flight = await calls_repo.count_in_flight(
        db, now=moment, max_age_s=settings.outbound_max_call_duration_s + 120)
    free = max(0, settings.max_concurrent_calls - settings.reserved_concurrency - in_flight)
    if settings.autonomous_dialing_paused:
        logger.bind(component="retry_poller").warning(
            "Autonomous dialing paused; claiming nothing this cycle")
        claimed = []
    elif settings.concurrency_gate_enabled:
        limit = min(settings.retry_batch_size, free)
        claimed = (await calls_repo.claim_due_retries(db, now=moment, limit=limit)
                   if limit > 0 else [])
    else:
        claimed = await calls_repo.claim_due_retries(db, now=moment,
                                                     limit=settings.retry_batch_size)
    await db.commit()
```
  (Gauge export lands in **E1**; `free`/`in_flight` are already computed here for it.)

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_concurrency_gate.py tests/test_retry_orchestrator.py -v && uv run mypy .`
- [ ] Step 5: `git commit -m "feat(api): global concurrency gate — recency-bounded count+claim in one txn, pause switch"`

---

### Task D7: Shared materializer (`schedule_orchestrator.materialize_call`, spec §5.3)

**Files:**
- Create: `apps/api/src/usan_api/schedule_orchestrator.py` (materializer half)
- Modify: `apps/api/src/usan_api/repositories/calls.py` (sequential after D6 — `count_autonomous_roots`, `create_materialized_root`)
- Test: `apps/api/tests/test_materializer.py`

- [ ] Step 1: Write the failing test (session_factory + truncate fixtures; `NOW` frozen):
  - `test_materialize_creates_queued_root_with_key_and_room` — outcome `("created", call)`; call: `QUEUED`, `attempt==1`, `parent_call_id is None`, `scheduled_at` set, fresh `usan-outbound-*` room, `dynamic_vars`/`profile_override` copied.
  - `test_daily_cap_blocks_third_root_same_local_date` — pre-create two roots for the elder (`sched:` + `batch:` keys, `scheduled_at` on the same elder-local date), cap 2 → outcome `"skipped_daily_cap"`, **no third call row** (§9 daily-cap: schedule + two batches → third skipped).
  - `test_daily_cap_counts_elder_local_date_not_utc` — elder `Pacific/Auckland`; a root at 11:30 UTC June 10 (= June 10 23:30 local) and a candidate at 13:00 UTC June 10 (= June 11 **local**) → not capped together (`day_bounds_utc`).
  - `test_daily_cap_on_dst_fall_back_day` — elder `America/New_York`, `local_day=date(2026,11,1)` (the **25-hour** local day): roots with `scheduled_at` 2026-11-01T04:30Z and 2026-11-02T04:30Z both fall inside the local day and cap the candidate; a root at 2026-11-02T05:30Z (local Nov 2) does **not** count (the cap boundary is exactly where a cached-offset bug bites).
  - `test_dnc_blocked_creates_terminal_row_consuming_key` — DNC entry → outcome `"dnc_blocked"`, call row status `DNC_BLOCKED` with the deterministic key (identical to `enqueue_call`'s gate; the advisory lock is taken first — assert `lock_phone` spy called).
  - `test_replay_adopts_owned_existing_row` — pre-insert a call with the target key + same elder → outcome `("replayed", existing)`; **exactly one row**; SAVEPOINT path (no transaction poisoning: a subsequent flush in the same session succeeds). This is the §9 deterministic-key double-materialization race test.
  - `test_replay_refuses_foreign_row_key_conflict` — pre-insert with the key but a **different elder** → outcome `"key_conflict"`, ERROR log captured, the foreign call is **not** linked/adopted (a squatted key can never substitute a wellness call, §5.3(5)); either way **a key never dials twice**.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_materializer.py -v` — RED: module missing.

- [ ] Step 3: Implement. `repositories/calls.py`:

```python
from usan_api.schemas.call import RESERVED_KEY_PREFIXES  # single source (B4) — do NOT
                                                          # redefine the tuple here

async def count_autonomous_roots(db, *, elder_id: uuid.UUID,
                                 day_start: datetime, day_end: datetime) -> int:
    """Roots with a reserved-prefix key whose scheduled_at falls inside the elder-local
    day bounds (daily repetition cap, spec §5.3 step 1; served by idx_calls_elder)."""
    ...or_(Call.idempotency_key.like("sched:%"), Call.idempotency_key.like("batch:%"))...
    # (LIKE patterns derived from RESERVED_KEY_PREFIXES)

async def create_materialized_root(db, *, elder_id, status: CallStatus,
        idempotency_key: str, scheduled_at: datetime | None,
        dynamic_vars: dict[str, Any], profile_override: uuid.UUID | None) -> Call:
    # livekit_room only for QUEUED rows; DNC_BLOCKED rows mirror enqueue_call (no room).
```

  `schedule_orchestrator.py` (module docstring: 3rd poller, single-replica caveat, §2.2 invariants):

```python
@dataclass(frozen=True)
class MaterializeOutcome:
    result: str            # created | replayed | dnc_blocked | skipped_daily_cap | key_conflict
    call: Call | None

async def materialize_call(db: AsyncSession, settings: Settings, *, elder: Elder,
        idempotency_key: str, scheduled_at: datetime, local_day: date,
        dynamic_vars: dict[str, Any],
        profile_override: uuid.UUID | None) -> MaterializeOutcome:
    """One Call per transaction — call insert + caller bookkeeping commit atomically
    (spec §5.3). Order: daily cap -> advisory phone lock -> DNC -> create; on
    IntegrityError (unique idempotency_key) SAVEPOINT-rollback (begin_nested), re-fetch,
    VERIFY OWNERSHIP (same elder, parent_call_id IS NULL) -> replayed, else key_conflict
    (ERROR log; never silently link a foreign call)."""
    day_start, day_end = schedule_windows.day_bounds_utc(local_day, elder.timezone)
    if await calls_repo.count_autonomous_roots(...) >= settings.max_autonomous_calls_per_elder_per_day:
        return MaterializeOutcome("skipped_daily_cap", None)
    await dnc_repo.lock_phone(db, elder.phone_e164)        # one lock at a time (§5.2)
    if await dnc_repo.is_blocked(db, elder.phone_e164):
        ...create DNC_BLOCKED row (begin_nested for key races too)...
        return MaterializeOutcome("dnc_blocked", call)
    try:
        async with db.begin_nested():
            call = await calls_repo.create_materialized_root(..., status=CallStatus.QUEUED, ...)
    except IntegrityError:
        existing = await calls_repo.get_by_idempotency_key(db, idempotency_key)
        if (existing is not None and existing.elder_id == elder.id
                and existing.parent_call_id is None):
            return MaterializeOutcome("replayed", existing)
        logger.bind(elder_id=str(elder.id)).error(
            "Materialization key conflict: existing row is not ours; refusing to adopt")
        return MaterializeOutcome("key_conflict", None)
    return MaterializeOutcome("created", call)
```

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_materializer.py -v && ruff check . && uv run mypy .`
- [ ] Step 5: `git commit -m "feat(api): shared materializer — daily cap, DNC gate, verified key replay"`

---

### Task D8: Scheduler poller phases 2+3 (trigger batches, materialize due schedules) + loop

**Files:**
- Modify: `apps/api/src/usan_api/schedule_orchestrator.py` (sequential after D7)
- Test: `apps/api/tests/test_schedule_orchestrator.py`

- [ ] Step 1: Write the failing test (frozen `NOW`; helpers seed elder+schedule via repos):
  - `test_poll_once_materializes_due_schedule_inside_window` — schedule due, `NOW` inside window → one `QUEUED` call keyed `sched:{id}:{local_date}`, `scheduled_at == quiet_hours.next_allowed(NOW, tz)`; schedule rows: `last_result == "created"`, `last_materialized_date == local_date`, **`next_run_at` advanced to the next masked day's effective-window start**.
  - `test_poll_once_twice_is_idempotent_with_full_bookkeeping` — run `poll_once` twice with the **same frozen NOW** after resetting `next_run_at` back to due (simulating the crash-before-bookkeeping window): exactly **one** call; second pass outcome replayed **and** `next_run_at` advanced + `last_result == "replayed"` — the §5.3(5)/§9 regression for the infinite re-claim loop (omitting replay bookkeeping would re-claim the same row every cycle forever).
  - `test_schedule_key_conflict_records_and_advances` — pre-insert a call carrying the `sched:{id}:{today}` key but a **different elder** → poll: no adoption (no second call row; the foreign row untouched), `last_result == "key_conflict"`, **`next_run_at` advanced**, ERROR captured. (A missing advance here is the same infinite-re-claim loop class as the replay-bookkeeping case; `key_conflict` is in migration 0012's CHECK enum for exactly this write.)
  - `test_past_window_end_skips_observably` — `NOW` past window end → **no call**, `last_result == "skipped_window"`, WARN captured, `next_run_at` advanced (poller-outage semantics: never a 23:00 call, §5.2/§5.5).
  - `test_before_window_start_reschedules_without_skipping_day` — stale `next_run_at` (westward tz edit simulation: update elder tz after create) → no call, `last_result == "rescheduled"`, `next_run_at` recomputed under the **current** tz, `last_materialized_date` untouched.
  - `test_invalid_timezone_fails_closed_hourly` — elder tz corrupted to garbage → no call, `last_result == "skipped_invalid_timezone"`, ERROR captured, `next_run_at == NOW + 1h` (observable, never hot-loops).
  - `test_dnc_auto_disables_schedule` — DNC the elder → `enabled is False`, `last_result == "dnc_blocked"`, WARN, a terminal `DNC_BLOCKED` call row exists; next `poll_once` never claims it again (§5.3 step 3: a daily schedule must not mint one DNC_BLOCKED row per day forever; re-enable is the operator's path, observable via `?last_result=dnc_blocked`).
  - `test_trigger_due_batches_phase` — `scheduled` batch with past/NULL `trigger_at` → `running`+`started_at`; future stays.
  - `test_concurrent_poll_once_disjoint_claims` — **open-transaction interleaving** (precedent `test_claim_skips_locked_rows`; sequential runs prove nothing — the first commit advances `next_run_at` and the second poll trivially finds nothing): on a **second engine**, session A runs `claim_due_schedules(db, now=NOW, limit=1)` and **holds the transaction open**; with the lock held, run a full `poll_once` via factory B → B materialized **only the unlocked schedule** and did not block; then A rolls back (releasing the lock, `next_run_at` unchanged) and a second `poll_once` materializes the remaining schedule; assert two calls total with distinct `sched:` keys (key uniqueness held throughout — §9 SKIP LOCKED race).
  - `test_run_poller_loop_discipline` — clone of the three `run_poller` tests from `test_retry_orchestrator.py` (stop-preset exits, survives exception, stop interrupts sleep) against `schedule_orchestrator.run_poller`.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_schedule_orchestrator.py -v` — RED: `poll_once`/`run_poller` missing.

- [ ] Step 3: Implement in `schedule_orchestrator.py`:

```python
async def poll_once(factory: async_sessionmaker[AsyncSession], settings: Settings,
                    *, now: datetime | None = None) -> dict[str, int]:
    """One six-phase cycle (spec §5.2); each phase commits before the next; phases
    3-4 process ONE row per transaction (claim 1, materialize, commit, repeat up to
    the budget) so exactly one per-phone advisory lock is held at a time and crash
    granularity is a single row. Returns per-phase work counts."""

async def _trigger_due_batches(factory, *, now) -> int                       # phase 2
async def _materialize_due_schedules(factory, settings, *, now) -> int      # phase 3
    # loop up to settings.scheduler_batch_size:
    #   one session/txn: claim_due_schedules(limit=1) -> branch in PYTHON (zoneinfo):
    #   ValueError anywhere -> record_result('skipped_invalid_timezone',
    #       next_run_at=now+1h) + ERROR;
    #   eff = effective_window(...); today = local_date(now, tz);
    #   start_utc, end_utc = window_bounds_utc(today, tz,
    #       window_start=eff[0], window_end=eff[1])   # keyword-only per B1 signature
    #   (day unmasked or now < start_utc) -> record_result('rescheduled',
    #       next_run_at=schedule_windows.next_run_at(now, tz, ...)) — no call;
    #   now >= end_utc -> record_result('skipped_window', next_run_at=next occurrence)
    #       + WARN;
    #   inside -> outcome = materialize_call(..., scheduled_at=quiet_hours.next_allowed(
    #       now, tz), local_day=today,
    #       idempotency_key=f"sched:{s.id}:{today.isoformat()}");
    #     created/replayed -> record_result(result, next_run_at=next_run_at(end_utc,...),
    #         last_materialized_date=today)
    #     dnc_blocked -> record_result('dnc_blocked', enabled=False, next_run_at=...) + WARN
    #     skipped_daily_cap / key_conflict -> record_result(..., next_run_at=...) (+ERROR
    #         for key_conflict)
    #   commit; logger.bind(schedule_id=..., call_id=...) — ids only.

async def run_poller(settings: Settings, stop: asyncio.Event) -> None:
    # byte-for-byte the retry orchestrator loop discipline (component="schedule_poller").
```
  Phases 1/4/5/6 are stubbed as no-op functions returning 0 in this task (implemented in D9) so `poll_once`'s shape is final.

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_schedule_orchestrator.py tests/test_materializer.py -v && uv run mypy .`
- [ ] Step 5: `git commit -m "feat(api): schedule poller — batch trigger + exhaustive schedule materialization branches"`

---

### Task D9: Scheduler phases 1, 4, 5, 6 (finalizer, throttled batch materialization, sweep, completion)

**Files:**
- Modify: `apps/api/src/usan_api/schedule_orchestrator.py` (sequential after D8)
- Modify: `apps/api/src/usan_api/repositories/calls.py` (sequential after D7 — add `has_child`)
- Test: `apps/api/tests/test_schedule_orchestrator.py` (append)

- [ ] Step 1: Write the failing test:
  - `test_batch_targets_materialize_with_slot_budget` — **pin `CONCURRENCY_GATE_ENABLED=true` explicitly**; gate math: `MAX_CONCURRENT_CALLS=5, RESERVED_CONCURRENCY=2`, 1 fresh in-flight call + 1 queued-due row → `slots = 5−2−1−1 = 1` → exactly one target materialized per cycle (`batch:{id}:{index}` key, target → `materialized`, `call_id` linked).
  - `test_batch_budget_applies_even_with_gate_disabled` — **same seeding, `CONCURRENCY_GATE_ENABLED=false`** → phase 4 still slot-budgets identically. The phase-4 materialization budget is **intrinsic and flag-independent** (spec §5.2 phase 4): the gate flag governs only the retry-poller *claim* path (D6). This test pins the gate-off × scheduler-on cell of the §9 flag matrix so the semantics can't silently drift either way.
  - `test_schedules_outrank_batches` — due schedule + pending batch target, slots=1 → the schedule materializes (phase 3 unthrottled, runs first); the batch target waits — deliberate priority (§5.2 phase 4 note).
  - `test_batch_target_skip_branches` — deleted elder (`elder_id` NULL via SET NULL) → `skipped/elder_deleted`; invalid tz → `skipped/invalid_timezone`; daily-cap hit → `skipped/daily_cap`; DNC → call row `DNC_BLOCKED` + target **materialized** (finalizer settles it `done/dnc_blocked` next cycle — asserted end-to-end in the finalizer matrix below).
  - `test_batch_target_key_conflict_skipped` — pre-insert a call carrying target 0's `batch:` key but a **different elder** → target `status == "skipped"`, `skip_reason == "key_conflict"`, **`call_id IS NULL`** (never linked), ERROR captured (§9's target-level ownership test).
  - `test_batch_window_pushes_dial_time` — batch window 10:00–12:00 mask `["mon"]`, NOW Tuesday → `scheduled_at` lands at next Monday 10:00 local (clamp pushed into the batch window/day-mask).
  - `test_pushed_target_capped_against_pushed_day` — batch window pushes the dial to next Monday; **two pre-existing autonomous roots already on that Monday (elder-local)** → target `skipped/daily_cap`. The cap must be evaluated against the **pushed** dial day, not today — otherwise a window-pushed target dodges the harassment cap (TCPA hole).
  - `test_crash_mid_batch_resume` — **§9 poller-crash test**: batch of 3; pre-insert a call carrying target 0's key (same elder) with target 0 still `pending` (the crash window between call insert and bookkeeping is impossible by §5.3's one-txn rule, but a *cross-process* duplicate is this exact shape) → next cycle: target 0 **replayed** + linked (`call_id` set, status `materialized`), targets 1–2 materialize fresh; total calls == 3, no duplicates.
  - `test_finalizer_matrix` — (a) completed chain → `done/completed`+`finalized_at`; (b) `no_answer` root **with** QUEUED child → unsettled (stays `materialized`); (c) ladder-exhausted `no_answer` (attempt 3, no child) → settled `done/no_answer`; (d) fail-closed-no-child (FAILED, elder deleted ⇒ no child) → settled; (e) voicemail chain (root VOICEMAIL_LEFT with child → unsettled; childless attempt-2 VOICEMAIL_LEFT → settled); **(f) DNC_BLOCKED root, no child → settled `done/final_status="dnc_blocked"`** (completes the §9 DNC-batch flow end-to-end: blocked number → DNC_BLOCKED row → target materialized → finalizer settles `done/dnc_blocked`). Chain-settled ⇔ tip status ∉ {queued,dialing,ringing,in_progress} AND no child (§6.2).
  - `test_sweep_cancels_queued_chains_backstop` — cancelled batch, materialized target whose tip is QUEUED (simulating the narrow cancel-vs-terminal-commit race) → phase 5 guarded-cancels it; in-flight tip untouched.
  - `test_completion_stamps_running_and_drained_cancelled` — running batch all-terminal → `completed`+`completed_at` **exactly once** (idempotent re-poll); cancelled batch drained → `completed_at` stamped, status stays `cancelled`, **leaves `open_batches` forever** (phases 1/5 never revisit drained history — §9 cancelled-batch drain bookkeeping); finalizer stamps guard-cancelled chains `done/final_status=cancelled` and a naturally-finished-after-cancel chain settles with its truthful outcome (e.g. `done/no_answer`).

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_schedule_orchestrator.py -v` — RED: stub phases return 0.

- [ ] Step 3: Implement. `repositories/calls.py`: `async def has_child(db, call_id) -> bool` (single indexed probe via `uq_calls_parent_call_id`). `schedule_orchestrator.py`:

```python
async def _finalize_settled_targets(factory, *, now) -> int        # phase 1
    # for open_batches -> list_materialized_targets: tip = get_chain_tip(root);
    # settled iff tip.status not active AND not has_child(tip.id);
    # finalize_target(final_status=tip.status.value); commit per batch.
async def _materialize_batch_targets(factory, settings, *, now) -> int   # phase 4
    # one txn: slots = max(0, max - reserved - count_in_flight(...) - count_queued_due(...));
    # NOTE: this budget is intrinsic — it applies regardless of CONCURRENCY_GATE_ENABLED
    # (the flag gates only the retry poller's claim path, spec §5.2 phase 4);
    # budget = min(scheduler_batch_size, slots); then per-target loop, ONE txn each:
    # claim_next_pending_target -> elder gone -> skipped/elder_deleted;
    # tz ValueError -> skipped/invalid_timezone (fail closed);
    # dial_at = quiet_hours.next_allowed(now, tz), pushed into the batch window/day-mask
    #   via schedule_windows.next_run_at(dial_at, tz, ...) when the batch window is set;
    # local_day = schedule_windows.local_date(dial_at, tz)   # the PUSHED dial day —
    #   the daily cap counts the day the call will actually happen, never `now`'s day;
    # materialize_call(idempotency_key=f"batch:{batch_id}:{target_index}",
    #                  scheduled_at=dial_at, local_day=local_day, ...):
    #   created/replayed -> mark_target_materialized(call_id);
    #   dnc_blocked -> mark_target_materialized(call_id)   (finalizer settles);
    #   skipped_daily_cap -> mark_target_skipped("daily_cap");
    #   key_conflict -> mark_target_skipped("key_conflict") + ERROR; commit.
async def _sweep_cancelled_batches(factory, *, now) -> int          # phase 5 (backstop only —
    # primary guard is D3's cancellation-aware schedule_retry)
async def _complete_drained_batches(factory, *, now) -> int         # phase 6
```
  Document in `_materialize_batch_targets`: **`max_concurrency` is a materialization throttle, not a dial cap** — retry children of materialized chains dial whenever due, bounded only by the global gate (§5.2).

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_schedule_orchestrator.py -v && ruff check . && uv run mypy .`
- [ ] Step 5: `git commit -m "feat(api): scheduler phases — finalizer, slot-budgeted batch materialization, sweep backstop, completion"`

---

### Task D10: Lifespan wiring — third poller

**Files:**
- Modify: `apps/api/src/usan_api/main.py` (sequential after C4)
- Test: `apps/api/tests/test_lifespan_poller.py` (append)

- [ ] Step 1: Write the failing test (clone the two existing poller tests): `test_lifespan_starts_scheduler_poller_when_enabled` — `SCHEDULER_POLLER_ENABLED=true`, monkeypatched `schedule_orchestrator.run_poller` → started, shares the same `stop` event, stop set on shutdown; `test_lifespan_skips_scheduler_poller_by_default` — env unset → never started (inert-default proof).

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_lifespan_poller.py -v` — RED: never started.

- [ ] Step 3: Implement in `lifespan` (after the retention poller, ahead of the unchanged `finally` cancel/await + `background.drain()`):

```python
    if settings.scheduler_poller_enabled:
        poller_tasks.append(asyncio.create_task(schedule_orchestrator.run_poller(settings, stop)))
```
  (+ `from usan_api import schedule_orchestrator` in the existing import line.)

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_lifespan_poller.py -v && uv run mypy .`
- [ ] Step 5: `git commit -m "feat(api): wire schedule_orchestrator as the third lifespan poller (env-gated)"`

---

### Task D11: Retention — scrub settled batch-target `dynamic_vars`

**Files:**
- Modify: `apps/api/src/usan_api/retention.py`
- Modify: `apps/api/tests/test_retention.py` (existing call sites — see Step 3)
- Test: `apps/api/tests/test_retention.py` (append)

- [ ] Step 1: Write the failing test: `test_purge_scrubs_settled_batch_target_vars` — settled targets (`done`/`skipped`/`cancelled`, `finalized_at`/`updated_at` past cutoff) get `dynamic_vars == {}` in the **same `purge_expired` transaction**; unsettled (`pending`/`materialized`) and fresh settled targets keep theirs; `test_purge_never_touches_schedule_vars` — `call_schedules.dynamic_vars` untouched (deliberately exempt live config, spec §8); return tuple grows to `(transcripts, calls, batch_targets)`.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_retention.py -v` — RED: vars survive / return-arity mismatch.

- [ ] Step 3: Implement — in `purge_expired` (same single transaction, third statement; update signature/docstring/`_purge_cycle` log line). **The 2-tuple → 3-tuple change breaks five existing unpacks in `tests/test_retention.py` (lines 60, 76, 94, 108, 125: `transcripts, _ =`, `_, scrubbed =`, etc.) — rewrite all five to the 3-tuple shape in this same step or Step 4 fails with `ValueError: too many values to unpack`.**

```python
target_scrub = await session.execute(
    update(CallBatchTarget)
    .where(
        CallBatchTarget.status.in_(("done", "skipped", "cancelled")),
        func.coalesce(CallBatchTarget.finalized_at, CallBatchTarget.updated_at) < cutoff,
        CallBatchTarget.dynamic_vars != {},
    )
    .values(dynamic_vars={})
)
```
  Rationale comment: without this the `calls` copy is scrubbed while the source copy lives forever, defeating the control (spec §8).

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_retention.py -v && uv run mypy .`
- [ ] Step 5: `git commit -m "feat(api): retention scrubs settled call_batch_targets.dynamic_vars (schedules exempt)"`

---

### Task D12: Part D gate

- [ ] Step 4: `cd apps/api && uv run pytest -v && ruff check . && ruff format --check . && uv run mypy .`

---

## Part E — Observability (4 counters + 2 gauges), alert-as-code, infra env plumbing

### Task E1: Metric definitions + gauges in the retry cycle + dial-requeue counter

**Files:**
- Modify: `apps/api/src/usan_api/observability/custom_metrics.py`
- Modify: `apps/api/src/usan_api/retry_orchestrator.py` (sequential after D6)
- Modify: `apps/api/src/usan_api/livekit_dispatch.py` (sequential after D5)
- Modify: `apps/api/tests/conftest.py` (add `gauge_value` helper next to `counter_value`)
- Test: `apps/api/tests/test_batch_observability.py`

- [ ] Step 1: Write the failing test (precedent `test_observability.py`; use `counter_value`/`gauge_value`):
  - `test_metric_objects_and_bounded_labels` — import all six; Counter label names exactly: `MATERIALIZED_CALLS_TOTAL` `("source","result")`, `BATCH_EVENTS_TOTAL` `("event",)`, `BATCH_TARGETS_FINALIZED_TOTAL` `("final_status",)`, `DIAL_REQUEUED_TOTAL` `("reason",)`; gauges `IN_FLIGHT_CALLS`/`DIAL_SLOTS_FREE` unlabeled; exposed names `usan_materialized_calls_total`, `usan_batch_events_total`, `usan_batch_targets_finalized_total`, `usan_dial_requeued_total`, `usan_in_flight_calls`, `usan_dial_slots_free`; the module docstring documents the structurally-impossible combos (`skipped_elder_deleted`×schedule, `skipped_window`/`rescheduled`×batch) — assert both substrings present (§7).
  - `test_gauges_exported_every_retry_cycle_even_when_gate_and_scheduler_disabled` — gate **off**, 2 fresh in-flight rows seeded → `retry_orchestrator.poll_once` sets `usan_in_flight_calls == 2` and `usan_dial_slots_free == max(0, 8−2−2)` (pre-enable observability; gauges live in the retry poller, NOT the scheduler — truthful whenever the gate could act, §5.4(2)/§7).
  - `test_dial_requeue_increments_quiet_hours_counter` — re-run the D5 stale-clamp scenario → `counter_value(DIAL_REQUEUED_TOTAL, reason="quiet_hours")` +1, and **not** incremented on the inside-hours path.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_batch_observability.py -v` — RED: ImportError on the six names.

- [ ] Step 3: Implement — add to `custom_metrics.py` (constructed `usan_X` → `_total`; PHI-free bounded enums; document increment-after-commit discipline + impossible combos per source):

```python
MATERIALIZED_CALLS_TOTAL = Counter("usan_materialized_calls",
    "Schedule/batch materialization decisions.", labelnames=("source", "result"))
BATCH_EVENTS_TOTAL = Counter("usan_batch_events",
    "Batch lifecycle transitions.", labelnames=("event",))   # created|started|completed|cancelled
BATCH_TARGETS_FINALIZED_TOTAL = Counter("usan_batch_targets_finalized",
    "Batch targets reaching a settled chain outcome.", labelnames=("final_status",))
DIAL_REQUEUED_TOTAL = Counter("usan_dial_requeued",
    "Claimed dials re-queued instead of dialed.", labelnames=("reason",))  # quiet_hours
IN_FLIGHT_CALLS = Gauge("usan_in_flight_calls",
    "Recency-bounded dialing/ringing/in_progress calls (gate input).")
DIAL_SLOTS_FREE = Gauge("usan_dial_slots_free",
    "max_concurrent_calls - reserved - in_flight, floor 0. Alert: ==0 for 10m.")
```
  `retry_orchestrator.poll_once`: after computing `in_flight`/`free` (D6) — `IN_FLIGHT_CALLS.set(in_flight); DIAL_SLOTS_FREE.set(free)` (every cycle, all flag states). `livekit_dispatch.py`: `DIAL_REQUEUED_TOTAL.labels(reason="quiet_hours").inc()` **after** the re-queue commit. `conftest.py`: add `gauge_value(gauge)` (collect()-based, like `counter_value`).

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_batch_observability.py tests/test_observability.py tests/test_concurrency_gate.py -v && uv run mypy .`
- [ ] Step 5: `git commit -m "feat(api): batch/scheduler metrics — gauges in retry cycle, quiet-hours requeue counter"`

---

### Task E2: Scheduler + batch-router counter increments (after commit)

**Files:**
- Modify: `apps/api/src/usan_api/schedule_orchestrator.py` (sequential after D9)
- Modify: `apps/api/src/usan_api/routers/batches.py` (sequential after C4)
- Test: `apps/api/tests/test_batch_observability.py` (append)

- [ ] Step 1: Write the failing test:
  - `test_materialization_results_increment_counter` — run the D8 scenarios (created, replayed, skipped_window, dnc_blocked) → `counter_value(MATERIALIZED_CALLS_TOTAL, source="schedule", result=...)` each +1; a batch-target skip increments `source="batch", result="skipped_elder_deleted"`.
  - `test_batch_events_increment_on_transitions` — POST /v1/batches → `event="created"` +1; poller trigger → `"started"`; drain → `"completed"`; cancel endpoint → `"cancelled"`.
  - `test_finalizer_increments_final_status` — completed chain finalized → `BATCH_TARGETS_FINALIZED_TOTAL{final_status="completed"}` +1; **a settled DNC chain (D9 finalizer-matrix case f) → `BATCH_TARGETS_FINALIZED_TOTAL{final_status="dnc_blocked"}` +1** (closes the §9 DNC-batch flow through the metric).
  - `test_increments_happen_after_commit` — monkeypatch the session factory so the materialization commit raises once → counter NOT incremented for the failed row (Phase-3 discipline: a crash can't double-count, §7).

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_batch_observability.py -v` — RED: counters stay 0.

- [ ] Step 3: Implement — in each phase function, collect `(source, result)` / event / final_status outcomes during the txn and `.inc()` **after** its commit; in `routers/batches.py`, `.inc()` after the create/cancel commits.

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_batch_observability.py tests/test_schedule_orchestrator.py tests/test_batches_api.py -v && uv run mypy .`
- [ ] Step 5: `git commit -m "feat(api): increment materialization/batch/finalizer counters after commit"`

---

### Task E3: `usan-dial-slots-exhausted` alert rule as code

**Files:**
- Modify: `infra/grafana/provisioning/alerting/usan_alerts.yml`
- Test: `scripts/tests/test_alerting_provisioning.py` (append)

- [ ] Step 1: Write the failing test:
  - extend `test_alert_rules_present` expected set with `"usan-dial-slots-exhausted"`;
  - `test_dial_slots_alert_sustained_ten_minutes` — that rule has `for == "10m"`, condition threshold `lt 1` (i.e. `usan_dial_slots_free == 0` sustained), datasource `prometheus`, `labels.severity == "page"`; existing `test_alert_rules_handle_nodata_and_exec_errors` loop automatically pins its `noDataState: OK` / `execErrState: Alerting`.

- [ ] Step 2: From the **repo root**: `python3 -m pytest scripts/tests/test_alerting_provisioning.py -v` (`pip install pytest pyyaml` if needed — CI does) — RED: uid absent.

- [ ] Step 3: Implement — append to the `usan-business` group (house shape of the two existing rules): uid `usan-dial-slots-exhausted`, title "Autonomous dial slots exhausted", refId A `expr: usan_dial_slots_free` (instant), refId C threshold `lt 1`, **`for: 10m`**, `noDataState: OK`, `execErrState: Alerting`, `labels: severity: page`, annotation: "usan_dial_slots_free has been 0 for 10m — with CONCURRENCY_GATE_ENABLED=true, autonomous dialing (schedules, batches, retries) has stopped. Only meaningful while the gate is enabled; silence during gate-off operation." (a wellness-check product not dialing is a paging condition, §7).

- [ ] Step 4: From the repo root: `python3 -m pytest scripts/tests -v --tb=short`
- [ ] Step 5: `git commit -m "fix(infra): provision dial-slots-exhausted alert (usan_dial_slots_free==0 for 10m) as code"`

---

### Task E4: Infra env plumbing — 8 keys × three files + dev-on/prod-off pins + contract test

**Files:**
- Modify: `infra/docker-compose.yml` (api `environment`), `infra/docker-compose.prod.yml` (api `environment`), `infra/.env.example`, `infra/.env.prod.example`
- Test: `apps/api/tests/test_infra_scheduler_env.py` (clone of `test_infra_messaging_env.py`)

- [ ] Step 1: Write the failing test — `_KEYS` = the 8 aliases (`SCHEDULER_POLLER_ENABLED`, `SCHEDULER_POLL_INTERVAL_S`, `SCHEDULER_BATCH_SIZE`, `CONCURRENCY_GATE_ENABLED`, `MAX_CONCURRENT_CALLS`, `RESERVED_CONCURRENCY`, `MAX_AUTONOMOUS_CALLS_PER_ELDER_PER_DAY`, `AUTONOMOUS_DIALING_PAUSED`):
  - `test_compose_api_service_has_scheduler_env` — all 8 in `services.api.environment` (dict/list normalization per precedent);
  - `test_dev_compose_enables_both_flags` — the compose values for the two flags **default on for dev**: `"${SCHEDULER_POLLER_ENABLED:-true}"` / `"${CONCURRENCY_GATE_ENABLED:-true}"` (§5.1/§9 dev-compose pin);
  - `test_prod_overlay_pins_flags_off` — `docker-compose.prod.yml` api environment re-pins both flags with `:-false` defaults (this is what makes §10.2's "stale prod .env interpolates defaults ⇒ safe/off" claim TRUE — the prod overlay's environment map overrides the dev-on base);
  - `test_env_example_contains_scheduler_keys` (commented-defaults house style) and `test_env_prod_example_contains_scheduler_keys` (live values; asserts the literal lines `SCHEDULER_POLLER_ENABLED=false` and `CONCURRENCY_GATE_ENABLED=false`);
  - `test_alert_rule_file_provisioned` — `infra/grafana/provisioning/alerting/usan_alerts.yml` contains `usan-dial-slots-exhausted` (§9 infra-contract pin).

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_infra_scheduler_env.py -v` — RED: keys absent from all four files.

- [ ] Step 3: Implement:
  - `docker-compose.yml` api env block (after the retry keys), with a comment citing the rollout sequence: the two flags `:-true` (dev), the other six `:-<settings default>` (`60`, `50`, `8`, `2`, `2`, `false`).
  - `docker-compose.prod.yml` api `environment`: add `SCHEDULER_POLLER_ENABLED: ${SCHEDULER_POLLER_ENABLED:-false}` and `CONCURRENCY_GATE_ENABLED: ${CONCURRENCY_GATE_ENABLED:-false}` with a comment: "ship inert (spec §10.1); flip via /opt/usan/infra/.env — refresh the VM .env BEFORE the v* tag deploy (the deploy never re-fetches it)".
  - `.env.example`: new `# === Batch & scheduled calling ===` block, all 8 as commented defaults with one-line rationale each (retry-orchestrator block style).
  - `.env.prod.example`: live-value block — both flags `false`, six numeric/bool live defaults, comment pointing at the §10.3 staged-enable sequence (gate first for a day, then scheduler, then a ≤5-target batch).

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_infra_scheduler_env.py tests/test_infra_messaging_env.py -v`
- [ ] Step 5: `git commit -m "feat(infra): scheduler/gate env keys across compose + env examples (dev-on, prod-off pins)"`

---

## Part F — Full-suite verification + spec-conformance pass

### Task F1: Gates, untouched-surface checks, §9 conformance audit

**Files:** none (verification only).

- [ ] Step 4: Run, in order, and confirm every command green. **Every line below starts from the repo root** (`/Users/evgenii.vasilenko/gofrolist/usan-voice-engine`) — subagent cwd resets between bash calls; do not chain relative `cd`s across lines:

```bash
cd apps/api && uv run pytest -v --tb=short && ruff check . && ruff format --check . && uv run mypy .
cd services/agent && uv run pytest -v && ruff check . && uv run mypy .   # untouched but must stay green
python3 -m pytest scripts/tests -v --tb=short                            # repo root, NOT services/
git diff --name-only origin/main...HEAD -- services/agent                # MUST print nothing (agent untouched)
cd apps/api && uv run --with pytest-cov pytest tests/ \
  --cov=usan_api.schedule_windows --cov=usan_api.schedule_orchestrator \
  --cov=usan_api.repositories.call_schedules --cov=usan_api.repositories.call_batches \
  --cov=usan_api.schemas.schedule --cov=usan_api.schemas.batch \
  --cov=usan_api.routers.schedules --cov=usan_api.routers.batches \
  --cov-report=term-missing --cov-fail-under=80
```

  The coverage command is a **hard gate**: no `|| true`, `--cov-fail-under=80`, and `--cov` scoped to the new modules only so well-covered legacy modules cannot mask a gap in the new code (spec §9 "coverage ≥80% on new modules").

  Then walk the **§9 conformance checklist** and tick each against a passing test (fix any gap before declaring done):
  - [ ] deterministic-key double-materialization race → `test_replay_adopts_owned_existing_row`, `test_poll_once_twice_is_idempotent_with_full_bookkeeping`, `test_concurrent_poll_once_disjoint_claims` (open-txn interleaving), `test_claim_next_pending_target_skip_locked_under_open_txn`
  - [ ] key-conflict ownership, both planes → `test_replay_refuses_foreign_row_key_conflict` (materializer), `test_schedule_key_conflict_records_and_advances` (schedule bookkeeping), `test_batch_target_key_conflict_skipped` (target skip)
  - [ ] poller crash mid-batch resume → `test_crash_mid_batch_resume`
  - [ ] TCPA dial-time re-check → `test_dispatch_and_dial_requeues_outside_quiet_hours` (+ E1 counter test)
  - [ ] gate starvation / reserved headroom / stale-row exclusion / pause / flag matrix → `test_concurrency_gate.py` (all six) + `test_batch_budget_applies_even_with_gate_disabled` (gate-off × scheduler-on cell)
  - [ ] daily cap (incl. elder-local date, DST fall-back day, window-pushed day) → `test_daily_cap_*`, `test_pushed_target_capped_against_pushed_day`
  - [ ] cancelled-batch drain bookkeeping → `test_completion_stamps_running_and_drained_cancelled`, `test_open_batches_and_complete_drained`
  - [ ] post-cancel retry-child race (sweep would lose) → `test_schedule_retry_suppressed_for_cancelled_batch_chain`
  - [ ] chain-tip cancel walks real chains → `test_cancel_batch_cancels_chain_tip_not_root`
  - [ ] DST boundaries for next_run_at + day bounds → `test_next_run_at_dst_spring_forward_recomputes_offset`, `test_next_run_at_dst_fall_back_recomputes_offset`, `test_day_bounds_utc_spans_dst_transition_days`
  - [ ] DNC auto-disable + DNC batch flow end-to-end → `test_dnc_auto_disables_schedule`, finalizer matrix (f) + E2 `final_status="dnc_blocked"` counter
  - [ ] elder_missing FAILED guard → `test_dispatch_and_dial_marks_elder_missing_failed_not_silent`
  - [ ] profile_override retry inheritance → `test_schedule_retry_child_inherits_profile_override`
  - [ ] digest replay 200/409 (+ replay-after-cancel pin), per-target 422 detail, `?last_result=`, origin, audit lines (positive + negative), `name` never logged → `test_batches_api.py` / `test_schedules_api.py` / `test_call_origin.py`
  - [ ] ratelimit route matching, lifespan 3rd poller, metric increments, retention scrub, env + alert contracts → C1 / D10 / E1–E4 tests

- [ ] Step 5: Commit anything outstanding, then stop. **Do not tag or deploy from this plan** — rollout (§10) is a separate operator sequence: VM `.env` refresh BEFORE the `v*` tag; gate-first staged enable; the drain check (`SELECT count(*) FROM calls WHERE status='queued' AND (idempotency_key LIKE 'sched:%' OR idempotency_key LIKE 'batch:%')` = 0) gates any image rollback.

---

## Review disposition

Second adversarial review pass (integration + test-strategy), applied to this plan. All repo-fact premises were independently re-verified before folding (retention test unpacks at lines 60/76/94/108/125; the combined no-status-write dispatch guard; `_columns` returning only name→type; `from datetime import datetime` class import; `_delete_room` placement; `RESERVED_KEY_PREFIXES` absent today).

**Applied — HIGH (3/3):**
- F1 coverage gate was vacuous (`|| true`, no fail-under) → hard gate: `--cov-fail-under=80`, per-new-module `--cov` scoping, no `|| true`.
- F1 scripts-tests ran from `services/` after the chained `cd` → every F1 line now starts from the repo root, with an explicit warning.
- D8 `test_concurrent_poll_once_disjoint_claims` was vacuous when run sequentially → rewritten as an open-transaction SKIP LOCKED interleaving (precedent `test_claim_skips_locked_rows`); a sibling open-txn disjointness test added for batch-target claims (B6).

**Applied — MEDIUM (11/11):**
- (int. M1) D11 now explicitly rewrites the five 2-tuple unpacks in `tests/test_retention.py`.
- (int. M2) A1 extends the copied `_columns` helper with `is_nullable`/`column_default`; "verbatim" dropped.
- (int. M3) Executor note 2 sequencing corrected: `repositories/calls.py` C4→D2→D3→D5→D6→D7→D9; `routers/batches.py` C4→E2 added.
- (ts) D9 daily-cap day for window-pushed targets pinned to `local_date(dial_at, tz)` + `test_pushed_target_capped_against_pushed_day`.
- (ts) D9 gains target-level `test_batch_target_key_conflict_skipped` (`skipped/key_conflict`, `call_id IS NULL`).
- (ts) D8 gains `test_schedule_key_conflict_records_and_advances` (bookkeeping advance — same infinite-re-claim class as replay).
- (ts) D9 finalizer matrix gains (f) DNC_BLOCKED→`done/dnc_blocked`; E2 asserts the `final_status="dnc_blocked"` counter.
- (ts) C4 gains `test_cancel_batch_cancels_chain_tip_not_root` (NO_ANSWER root + QUEUED child) so `get_chain_tip`'s hop walk is exercised.
- (ts) Every apps/api verify command now carries `cd apps/api && ` (cwd resets between subagent bash calls); Executor note 5 added.
- (ts) Flag matrix completed: D9 budget tests pin `CONCURRENCY_GATE_ENABLED`, and `test_batch_budget_applies_even_with_gate_disabled` codifies the decided semantics — the phase-4 materialization budget is intrinsic/flag-independent; the gate flag governs only the retry-claim path (per spec §5.2 reading).
- (ts) B1 DST coverage extended: fall-back `next_run_at` test (2026-11-01, 14:00Z EST) + `day_bounds_utc` 25h/23h transition-day test; D7 gains `test_daily_cap_on_dst_fall_back_day`.

**Applied — LOW (11/11):**
- (int. L1) A2's wrong import-clash rationale removed; alias dropped (`models.py` imports only the `datetime` class — no conflict).
- (int. L2) Signature mismatches fixed: keyword-only `window_bounds_utc(..., window_start=eff[0], window_end=eff[1])` in D8; `idempotency_key=` (not `key=`) in D8/D9 `materialize_call` calls.
- (int. L3) D7 no longer redefines the prefixes; `repositories/calls.py` imports `RESERVED_KEY_PREFIXES` from `schemas.call` (B4, single source).
- (int. L4) D5 insertion point corrected: after `room = call.livekit_room`, before `dnc_repo.lock_phone` (so `room` is in scope for `_delete_room` and the lock is never held).
- (int. L5) B6 `claim_next_pending_target` documents the materialized-count throttle's equivalence-via-phase-ordering with the spec's in-flight-chain wording.
- (int. L6) A1's misleading enum-style `(status)::text` indexdef literal deleted; hedged assertion kept with the TEXT-rendering note.
- (ts) C4 gains the positive audit-log assertion (`client`/`batch_id`/`action` bound on create+cancel).
- (ts) C4 pins replay-after-cancel: same key+digest → 200 with the cancelled batch, never a re-run.
- (ts) B6 gains `test_list_batches_clamped_and_ordered` (≤500 clamp, newest-first, id tiebreaker).
- (ts) B1 module docstring states the deliberate `effective_window` None-vs-error deviation from spec §9 wording (error contract preserved one layer up).
- (ts) C3 gains `test_patch_window_422_when_elder_timezone_went_bad` (tz corrupted post-create → 422, not 500).

**Rejected:** none — every finding's factual premise checked out against the repo, and each fix is local to the task it amends; no finding conflicted with the spec or with another finding.

**Numbering:** no tasks were added or removed by the review; Task IDs A1–A3, B1–B6, C1–C6, D1–D12, E1–E4, F1 are unchanged and all internal cross-references (executor notes, sequencing chains, F1 checklist) were re-verified against the final text.

---

## Files read (for reference)
- Spec: `/Users/evgenii.vasilenko/gofrolist/usan-voice-engine/docs/superpowers/specs/2026-06-10-batch-scheduled-calling-design.md` (full, incl. Appendix A)
- Format reference: `/Users/evgenii.vasilenko/gofrolist/usan-voice-engine/docs/superpowers/plans/2026-06-09-plan-admin-8-tools.md`
- `apps/api/src/usan_api/`: `retry_orchestrator.py`, `livekit_dispatch.py` (dispatch_and_dial 301–362, guards 312–322, `_delete_room` 209), `dialer.py`, `quiet_hours.py`, `retry_policy.py`, `ratelimit.py` (`_is_operator_route` 31–48), `retention.py` (purge_expired 46–80), `settings.py`, `main.py` (lifespan 57–81), `db/models.py`, `db/base.py`, `schemas/call.py`, `schemas/tools.py` (`_assume_utc` 72–80), `schemas/_validators.py`, `repositories/calls.py` (schedule_retry 178–221, claim_due_retries 224–248, reclaim 283–309), `repositories/dnc.py` (advisory lock), `repositories/follow_up_flags.py`, `repositories/callback_requests.py`, `repositories/admin_audit.py`, `repositories/agent_profiles.py` (resolve/published shape), `routers/calls.py` (`_idempotent_replay` 36–43, PHI-access audit 210–243), `observability/custom_metrics.py`
- `apps/api/migrations/versions/`: `0001` (idx_calls_elder 86, idx_calls_status_scheduled 88–92), `0003` (uq_calls_parent_call_id, idx_calls_due_retries), `0011`
- `apps/api/tests/`: `conftest.py` (TRUNCATE 88–96, counter_value 38–48), `test_phase3_migration.py` (`_columns` returns name→type only), `test_phase3_models.py`, `test_retry_orchestrator.py` (`test_claim_skips_locked_rows` open-txn pattern), `test_dispatch_and_dial.py`, `test_retention.py` (2-tuple unpacks 60/76/94/108/125), `test_lifespan_poller.py`, `test_infra_messaging_env.py`, `test_app_security.py` (ratelimit tests 45–114)
- `infra/`: `docker-compose.yml` (api environment), `docker-compose.prod.yml` (api env merge 17–48), `.env.example`, `.env.prod.example`, `grafana/provisioning/alerting/usan_alerts.yml`
- `scripts/tests/test_alerting_provisioning.py`; `.github/workflows/test.yml` (scripts tests run `python -m pytest scripts/tests` with pip-installed pytest+pyyaml)

> **MEDIUM-10 confirmation (orchestrator, 2026-06-10):** verified against spec §5.2/§5.4 —
> the phase-4 materialization budget is part of the scheduler poller's own behavior and is
> NOT conditioned on `CONCURRENCY_GATE_ENABLED`; only the §5.4 retry-poller *claim gate* is
> flag-controlled ("the throttle is soft pacing…; the claim gate is the invariant").
> `test_batch_budget_applies_even_with_gate_disabled` pins the correct behavior.
