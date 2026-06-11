# Calls UI + Ops Queues (Admin-UI Phase A2) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give nurses a human surface for flagged calls: a Calls console (`/calls`, `/calls/:id` — paged masked-phone list, role-styled transcript viewer, recording playback over a TTL-clamped presigned URL) and an Ops Queues page (`/queues` — follow-up flags + callback requests with an `open → acknowledged → resolved` workflow, urgent-first ordering, PHI-free tab-count summary). Plus the PR #54 schema hardening (migration `0013`: status CHECKs + `status_updated_at`/`status_updated_by`), centralization of the two locked-sink PHI-audit strings into `phi_audit.py` (operator plane bit-identical), `Cache-Control: no-store` on the whole admin plane, and the SMS `to_number` masking fix.

**Architecture:** Everything rides the existing admin plane (session cookie + per-request DB re-check, viewer/admin roles, `admin_audit` rows, Caddy CIDR gate, existing rate limiter — `/v1/admin/` already matched, no matcher change). New in `apps/api`: migration `0013`, `phi_audit.py`, `recording_urls.py`, `masking.py`, `repositories/admin_calls.py`, `schemas/admin_calls.py`, `routers/admin_calls.py`. Touched: `routers/calls.py` (helper extraction, bit-identical), `routers/admin_elders.py` (`_mask` → `masking.mask_phone`), `routers/admin_tools.py` (2 PATCH + summary + list extensions + SMS masking), `repositories/{follow_up_flags,callback_requests}.py`, `schemas/admin_tools.py`, `db/models.py`, `main.py` (router + no-store middleware), `observability/custom_metrics.py` (1 counter). New in `apps/admin-ui`: `features/calls/*`, `features/queues/*`, `lib/format.ts`; touched: `lib/api.ts` (`patch`), `types/api.ts`, `routes.tsx`, `components/NavSidebar.tsx`. **Zero `services/agent` changes, no new env keys, no settings changes, no conftest TRUNCATE change (0013 is column/constraint/index-only).**

**Tech Stack:** Python 3.14 (FastAPI, Pydantic v2, SQLAlchemy 2 async, Alembic raw-SQL, prometheus_client, loguru lazy `{}` ids-only); testcontainers Postgres; React 18 + TanStack Query v5 + react-router 6 + Tailwind; vitest + Testing Library (jsdom, `vi.mock("../lib/api")` route-by-URL pattern).

**Source spec:** `docs/superpowers/specs/2026-06-10-calls-ui-ops-queues-design.md` (Final, review findings applied). Honor every §6 PHI/audit rule and every §9 test row. This plan additionally carries a second adversarial review pass (integration + test-strategy) — see the **Review disposition** section at the end.

**Executor notes (read before starting):**
1. **Stacked-branch mechanics:** branch `feat/calls-ui` is stacked on `feat/batch-calling` (PR #55, open; alembic head `0012`). Before Task A1, run `cd apps/api && uv run alembic heads` and confirm exactly `0012`. After #55 squash-merges and this branch rebases (`git rebase --onto origin/main <prev-plan-tip>`), **re-verify the head is still `0012` and `0013`'s `down_revision` matches** before opening the PR (spec §10.1).
2. **Same-file sequencing (strict — never parallelize tasks sharing a file):** `routers/admin_calls.py` B5 → B6; `main.py` B5 → B7; `repositories/follow_up_flags.py` + `callback_requests.py` C1 → C3; `tests/test_follow_up_flags_repo.py` + `tests/test_callback_requests_repo.py` C1 → C3; `schemas/admin_tools.py` C2 → C3 → C4; `routers/admin_tools.py` C2 → C3 → C4 → C5; `tests/test_admin_tools_api.py` C2 → C3 → C4; `tests/test_admin_callback_requests_api.py` C2 → C3; `tests/test_admin_tools_schemas.py` C3 only; `apps/admin-ui/src/types/api.ts` D1 only (all new types land at once); `features/calls/hooks.ts` D2 → D3; `routes.tsx` D2 → D3 → D4.
3. **The locked-sink strings are load-bearing** (`infra/terraform/observability.tf:47`, locked 6-year `usan-phi-audit` bucket, substring filter on `"Transcript accessed"` / `"Recording URL accessed"`). After B1 they exist ONLY in `phi_audit.py`; never retype them in routers or tests except the one verbatim-pin test. Renaming the messages breaks the immutable audit trail and is forbidden.
4. House rules: repos take the request session, `flush()+refresh()`, **never commit**; routers commit; every audit write uses the `try/except SQLAlchemyError: rollback; raise` guard in the same commit. Loguru lazy `{}` placeholders, **bind ids/actor/counts only — never names, phones, reason/notes text, transcript content, or URLs**. ruff line-length 100. **Type-checking is `uv run mypy` (bare — no `.` argument):** both packages set `[tool.mypy] files = ["src"]`, and passing `.` overrides that to strict-check tests+migrations (~1500 pre-existing errors). CI (`lint.yml`) runs bare `uv run mypy`; every verify command in this plan does the same. Run it before every push (CI runs it even though CLAUDE.md omits it).
5. **Deliberate test-surface breaks — update the tests as written here; never "fix" by loosening the implementation:**
   - **C2:** adding `status_updated_at`/`status_updated_by` to both summaries breaks the exact key-set assert in `test_admin_callback_requests_api.py::test_list_callback_requests` (line 63) → key-set extended in C2. (`test_admin_tools_schemas.py` keeps passing in C2 — the new fields carry `= None` defaults.)
   - **C3:** queue-list `status` junk now 422s (was silent 200-empty) → `test_admin_tools_api.py::test_follow_up_flags_list_and_filter` (uses `status=closed`, line 81) is rewritten; the repo list functions now return `(row, elder_name, phone)` tuples → tuple-unpack rewrites in `test_follow_up_flags_repo.py::test_create_and_list_follow_up_flag` and `test_callback_requests_repo.py::test_list_callback_requests_filters_by_status` / `_filters_by_elder`; the callbacks key-set extends again (`elder_name`, `masked_phone`); `test_admin_tools_schemas.py`'s three `_Row` stubs gain `elder_name`/`masked_phone` attrs (required `masked_phone: str`).
   - **C5:** SMS `to_number` becomes masked → `test_admin_sms_messages_api.py` gains the masking pin.
6. **Every verify command starts from the repo root** (`/Users/evgenii.vasilenko/gofrolist/usan-voice-engine`). Subagent executors get their cwd reset between bash calls; every command below carries its own `cd` prefix — and the Part-E block uses **absolute** paths so it stays correct even if executed top-to-bottom as a single script. Do not strip the prefixes.
7. UI work uses **no new libraries** — existing components only (`Table/Select/Input/Spinner/Button/Badge/Tabs/Dialog/ConfirmDialog`, `pushToast`, `useIsAdmin`). TanStack v5 syntax (`invalidateQueries({ queryKey })`). No PHI in query keys (UUIDs/enums/dates only), no `console.*` of response bodies, presigned URLs live only in props. **UI tests assert semantic markers (`data-role`, `data-severity`, badge text, aria-labels) — never Tailwind class strings** (a restyle must not break tests; a broken style with the same classes must not pass them).

---

## Part A — Migration 0013 + model changes + contract tests

### Task A1: Migration `0013_ops_queue_status_workflow.py` + migration contract test

**Files:**
- Create: `apps/api/migrations/versions/0013_ops_queue_status_workflow.py`
- Test: `apps/api/tests/test_ops_queue_migration.py`
- (No conftest change: 0013 adds no tables; the TRUNCATE list is already correct.)

- [ ] Step 1: Write the failing test — `apps/api/tests/test_ops_queue_migration.py`, copying `test_batch_migration.py`'s helpers verbatim (`_columns` returning `{name: (data_type, is_nullable, column_default)}`, `_indexes`, `_indexdef`, `_check_constraints`, plus its `API_DIR`/env-dict subprocess pattern):
  - `test_workflow_columns_both_tables` — for `follow_up_flags` AND `callback_requests`: `cols["status_updated_at"] == ("timestamp with time zone", "YES", None)` and `cols["status_updated_by"] == ("text", "YES", None)` (NULL = never transitioned past `'open'`; no backfill).
  - `test_status_check_constraints_present_and_enforced` — `_check_constraints` contains `ck_follow_up_flags_status` / `ck_callback_requests_status`; runtime enforcement via a throwaway engine in **three separate `engine.begin()` blocks** (a single connection would hit `InFailedSqlTransaction` on the cleanup DELETE after the failed INSERT): (1) seed an elder + a call (`direction='outbound'`, `status='queued'` cast to the enums); (2) inside `pytest.raises(IntegrityError)`, `INSERT INTO follow_up_flags (call_id, elder_id, severity, category, status) VALUES (..., 'routine','medical','bogus')` — the `engine.begin()` context manager rolls the failed transaction back; same again for `callback_requests` (`requested_time_text='x'`, `status='bogus'`) in its own block; (3) DELETE the seeded elder/call rows (these tests bypass the `client` fixture's teardown TRUNCATE).
  - `test_idx_calls_created_shape` — `idx_calls_created` in `_indexes(url, "calls")`; `_indexdef` contains `(created_at DESC, id DESC)` (serves the global newest-first admin list; the per-elder slice keeps `idx_calls_elder`).
  - `test_downgrade_seed_upgrade_normalizes_and_roundtrips` — **the subprocess roundtrip pattern from `test_batch_migration.py::test_downgrade_then_upgrade_roundtrip`; conftest migrates to head before tests run, so a head-only normalize test is vacuous.** `alembic downgrade 0012` → assert both `status_updated_*` columns absent, both CHECK constraints absent, `idx_calls_created` gone; seed (throwaway engine) an elder + call + one `follow_up_flags` row and one `callback_requests` row with `status='weird'` (legal pre-CHECK); `alembic upgrade head` → both seeded rows read back `status == 'open'` (the defensive normalize), columns/constraints/index all present again; DELETE the seeded rows + call + elder. Runs **last** in the module; leaves the session DB clean at head.

- [ ] Step 2: Run test, verify it FAILS

```bash
cd apps/api && uv run pytest tests/test_ops_queue_migration.py -v
```
RED reason: alembic head is `0012`; `status_updated_at` absent → `KeyError`; `idx_calls_created` absent; `'bogus'` inserts succeed (no CHECK).

- [ ] Step 3: Implement `0013_ops_queue_status_workflow.py` — module attrs typed exactly like `0012` (`revision: str = "0013"`, `down_revision: str | None = "0012"`), raw `op.execute` only, SQL **verbatim from spec §3** in this order: (1) `ALTER TABLE ... ADD COLUMN status_updated_at TIMESTAMPTZ, ADD COLUMN status_updated_by TEXT` for both tables (comment: admin actor email; NULL = open since creation); (2) defensive `UPDATE ... SET status='open' WHERE status NOT IN ('open','acknowledged','resolved')` **before** each `ADD CONSTRAINT ck_*_status CHECK (status IN ('open','acknowledged','resolved'))` (comment: the only writer ever was the server_default `'open'`, but a stray manual edit must not abort the deploy's auto-migration); (3) `CREATE INDEX idx_calls_created ON calls (created_at DESC, id DESC)` (comment: composite because `created_at` ties are guaranteed — `func.now()` is the transaction timestamp and the A1 batch materializer inserts many Call rows per poller transaction; the list orders by the same pair). `downgrade()` in reverse order with `IF EXISTS` on every DROP, exactly the spec §3 block.

- [ ] Step 4: Run test, verify PASS

```bash
cd apps/api && uv run pytest tests/test_ops_queue_migration.py tests/test_batch_migration.py -v && ruff check migrations && ruff format --check migrations && uv run mypy
```

- [ ] Step 5: Commit

```bash
git add apps/api/migrations/versions/0013_ops_queue_status_workflow.py apps/api/tests/test_ops_queue_migration.py && git commit -m "feat(api): migration 0013 — ops-queue status workflow columns + CHECKs + idx_calls_created"
```

---

### Task A2: Model columns on `FollowUpFlag` + `CallbackRequest`

**Files:**
- Modify: `apps/api/src/usan_api/db/models.py` (both classes, after `status`)
- Test: `apps/api/tests/test_ops_queue_models.py`

- [ ] Step 1: Write the failing test — `tests/test_ops_queue_models.py`, pure `__table__` introspection (pattern: `test_phase3_models.py`/`test_batch_models.py`, no DB): `test_workflow_columns_on_both_models` — for `FollowUpFlag` and `CallbackRequest`: `"status_updated_at" in cols` with `cols["status_updated_at"].nullable is True` and `cols["status_updated_at"].type.timezone is True` (house `DateTime(timezone=True)`); `cols["status_updated_by"].nullable is True` and the type is `Text`.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_ops_queue_models.py -v` — RED: `KeyError: 'status_updated_at'`.

- [ ] Step 3: Implement — append to **both** models (mirror of spec §3):

```python
    status_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status_updated_by: Mapped[str | None] = mapped_column(Text)  # admin actor email
```

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_ops_queue_models.py tests/test_ops_queue_migration.py -v && ruff check src/usan_api/db/models.py && uv run mypy`
- [ ] Step 5: `git add apps/api/src/usan_api/db/models.py apps/api/tests/test_ops_queue_models.py && git commit -m "feat(api): status_updated_at/by ORM columns (migration 0013 mirror)"`

---

### Task A3: Part A gate

- [ ] Step 4: `cd apps/api && uv run pytest -v && ruff check . && ruff format --check . && uv run mypy` — full suite green before Part B. Commit any `ruff format` rewrites as `chore(api): format Part A`.

---

## Part B — phi_audit/masking/recording_urls extraction (operator plane bit-identical) + admin calls endpoints + no-store middleware

### Task B1: `phi_audit.py` — the locked-sink constants + emit helpers

**Files:**
- Create: `apps/api/src/usan_api/phi_audit.py`
- Test: `apps/api/tests/test_phi_audit.py`

- [ ] Step 1: Write the failing test (pure unit; loguru capture via `logger.add(lambda m: records.append(m.record), level="INFO")` — the `test_schedules_api.py:234` pattern):
  - `test_locked_sink_strings_verbatim` — `phi_audit.TRANSCRIPT_ACCESSED == "Transcript accessed"` and `phi_audit.RECORDING_URL_ACCESSED == "Recording URL accessed"`, **exact `==`** with a comment citing `infra/terraform/observability.tf:47` (drift = broken immutable 6-year trail).
  - `test_transcript_accessed_binds_ids_and_actor_only_when_given` — `log_transcript_accessed(call_id=cid, client="10.0.0.9", segments=3)` → exactly one record: `record["message"] == TRANSCRIPT_ACCESSED`; `extra` has `call_id`/`client`/`segments`; **`"actor" not in extra`** (operator-plane bit-identical contract). Second call with `actor="nurse@usan.org"` → `extra["actor"] == "nurse@usan.org"`.
  - `test_recording_url_accessed_binds_flag_and_actor` — `log_recording_url_accessed(call_id=cid, client="10.0.0.9", actor="nurse@usan.org")` → message == constant, `extra["has_recording"] is True`, `extra["actor"]` set; without `actor` → `"actor" not in extra`. (The helper never receives the URL by signature, so a URL-absence assertion here would be unfalsifiable — the **non-vacuous** URL negatives live in B3's success test and B6's sink test, where the monkeypatched signer returns a real sentinel URL.)

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_phi_audit.py -v` — RED: `ModuleNotFoundError: usan_api.phi_audit`.

- [ ] Step 3: Implement `src/usan_api/phi_audit.py` (module docstring: single source of the load-bearing locked-sink strings, spec §6.1; both planes call these helpers; extra bound fields are safe — the sink filter is a contains match — but the messages must never change; URLs are bearer secrets and are never passed to or logged by these helpers):

```python
TRANSCRIPT_ACCESSED = "Transcript accessed"
RECORDING_URL_ACCESSED = "Recording URL accessed"

def log_transcript_accessed(*, call_id: uuid.UUID, client: str, segments: int,
                            actor: str | None = None) -> None:
    bound: dict[str, Any] = {"call_id": str(call_id), "client": client, "segments": segments}
    if actor is not None:   # bind only when present: operator-plane records stay bit-identical
        bound["actor"] = actor
    logger.bind(**bound).info(TRANSCRIPT_ACCESSED)

def log_recording_url_accessed(*, call_id: uuid.UUID, client: str,
                               actor: str | None = None) -> None:
    ...same shape, has_recording=True...   # ids/host/actor/flag only — never content or URLs
```

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_phi_audit.py -v && ruff check src/usan_api/phi_audit.py && uv run mypy`
- [ ] Step 5: `git add apps/api/src/usan_api/phi_audit.py apps/api/tests/test_phi_audit.py && git commit -m "feat(api): phi_audit — single source of the locked-sink PHI-access log lines"`

---

### Task B2: `masking.py` — `mask_phone` extraction

**Files:**
- Create: `apps/api/src/usan_api/masking.py`
- Modify: `apps/api/src/usan_api/routers/admin_elders.py` (delete `_mask`, import `mask_phone`)
- Test: `apps/api/tests/test_masking.py`

- [ ] Step 1: Write the failing test — `tests/test_masking.py`: `test_mask_phone_last4` (`mask_phone("+15551234567") == "***4567"`), `test_mask_phone_none_and_empty_unknown` (`mask_phone(None) == "unknown"`, `mask_phone("") == "unknown"`) — bit-identical to today's `admin_elders._mask`.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_masking.py -v` — RED: module missing.

- [ ] Step 3: Implement `src/usan_api/masking.py`:

```python
def mask_phone(phone: str | None) -> str:
    """'***' + last 4 digits; 'unknown' when absent. The ONLY phone rendering
    permitted in admin-plane response bodies (spec §6.3)."""
    return "***" + phone[-4:] if phone else "unknown"
```
  In `routers/admin_elders.py`: delete `_mask`, add `from usan_api.masking import mask_phone`, switch `_summary` to `mask_phone(elder.phone_e164)` (no behavior change).

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_masking.py tests/test_admin_elders_api.py -v && ruff check . && uv run mypy` — `test_admin_elders_api.py` must pass **unmodified**.
- [ ] Step 5: `git add -A apps/api && git commit -m "refactor(api): extract masking.mask_phone from admin_elders (no behavior change)"`

---

### Task B3: `recording_urls.py` — presigned-URL helper extraction + operator `get_call` switch

**Files:**
- Create: `apps/api/src/usan_api/recording_urls.py`
- Modify: `apps/api/src/usan_api/routers/calls.py` (`get_call` switches to the helpers; `_presigned_recording_url` moves out)
- Test: `apps/api/tests/test_recording_urls.py` (new) + append one pin to `apps/api/tests/test_recording_url.py`

- [ ] Step 1: Write the failing test — `tests/test_recording_urls.py` (unit; fake `Call` via `types.SimpleNamespace(id=..., recording_uri=...)`, `Settings` from env kwargs or `get_settings()` with monkeypatched env; monkeypatch `object_storage.generate_signed_url` to capture args and return the sentinel `"https://storage.example/SIGNED-SENTINEL"`):
  - `test_ttl_unclamped_without_max` — settings TTL 3600, `max_ttl_s=None` → signing called with `3600` (operator plane bit-identical).
  - `test_ttl_clamped_with_max` — settings TTL 3600, `max_ttl_s=recording_urls.ADMIN_RECORDING_URL_MAX_TTL_S` → signing called with `600`; settings TTL 300 + `max_ttl_s=600` → `300` (true min, never a raise).
  - `test_expected_bucket_passed` — `expected_bucket=settings.gcs_bucket` forwarded (capture kwargs).
  - `test_signing_called_via_thread` — monkeypatch `asyncio.to_thread` with an async fake (`async def fake_to_thread(func, /, *args, **kwargs): calls.append(func); return func(*args, **kwargs)`) → assert it was invoked with `object_storage.generate_signed_url` (pins the §9 "called via thread" row non-vacuously; a plain signer-mock cannot distinguish offload from a direct call).
  - `test_none_paths` — `recording_uri=None` or `gcs_bucket=None` → `None`, no log line; signing raises → `None` + a `"Failed to sign recording URL"` WARNING captured (existing copy).
  - `test_success_emits_locked_sink_line_actor_optional` — loguru capture: message **is** `phi_audit.RECORDING_URL_ACCESSED`; `actor` bound only when passed; `client` bound from `client_host`; **the sentinel URL string (`"SIGNED-SENTINEL"`) appears in no captured record's message or extra** (URLs are bearer secrets — this is the non-vacuous negative the B1 unit cannot express).
  - Append to `tests/test_recording_url.py` (new test, existing tests untouched): `test_operator_get_call_records_never_bind_actor` — seed a call with a recording (existing `_seed`), mock `generate_signed_url`, log-capture around `GET /v1/calls/{id}` → the `RECORDING_URL_ACCESSED` record exists with `call_id`/`client`/`has_recording` and **`"actor" not in extra`** (behavioral-refactor guard for the extraction).

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_recording_urls.py -v` — RED: `ModuleNotFoundError: usan_api.recording_urls`.

- [ ] Step 3: Implement `src/usan_api/recording_urls.py` — move the body of `routers/calls.py::_presigned_recording_url` here **verbatim** (asyncio.to_thread + expected_bucket + silent-None-with-WARN; keep calling `object_storage.generate_signed_url` via module attribute so the existing monkeypatch pattern survives), with two changes: the TTL becomes `ttl = settings.recording_signed_url_ttl_s if max_ttl_s is None else min(settings.recording_signed_url_ttl_s, max_ttl_s)`, and the success log goes through `phi_audit.log_recording_url_accessed(call_id=call.id, client=client_host, actor=actor)`:

```python
# Admin-plane TTL ceiling (spec §4.2/§8): a signed URL is IP-unbound — it defeats the
# CIDR gate once issued — so the admin plane caps exposure at 10 minutes. The settings
# default (3600) is the MAX of its 60–3600 range, not "short". Constant, not an env key.
ADMIN_RECORDING_URL_MAX_TTL_S = 600

async def presigned_recording_url(call: Call, settings: Settings, *, client_host: str,
                                  actor: str | None = None,
                                  max_ttl_s: int | None = None) -> str | None: ...
```
  In `routers/calls.py`: delete `_presigned_recording_url`; `get_call` calls `recording_urls.presigned_recording_url(call, settings, client_host=client_host)` (no `actor`, no `max_ttl_s` → bit-identical) and replaces the inline transcript log with `phi_audit.log_transcript_accessed(call_id=call_id, client=client_host, segments=len(transcript))` (only when non-empty — unchanged guard). Keep the surrounding comments; drop now-unused imports (`asyncio`, `object_storage`) if nothing else uses them.

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_recording_urls.py tests/test_recording_url.py tests/test_calls.py tests/test_call_transcript.py tests/test_phi_audit.py -v && ruff check . && uv run mypy` — every pre-existing operator-plane test passes **unmodified**.
- [ ] Step 5: `git add -A apps/api && git commit -m "refactor(api): extract recording_urls.presigned_recording_url with admin TTL clamp seam (operator plane bit-identical)"`

---

### Task B4: `repositories/admin_calls.py` — the calls read model

**Files:**
- Create: `apps/api/src/usan_api/repositories/admin_calls.py`
- Test: `apps/api/tests/test_admin_calls_repo.py`

- [ ] Step 1: Write the failing test (own `session_factory` fixture + autouse `TRUNCATE calls, elders CASCADE` — the `test_call_schedules_repo.py` pattern; seed via `elders_repo.create_elder` + `calls_repo.create_call`, which accepts `idempotency_key`):
  - `test_orders_newest_first_id_tiebreaker` — three calls created in **one flush** (tied `created_at` — `func.now()` is the txn timestamp) → returned in `id DESC` order; a fourth created in a later txn sorts first.
  - `test_origin_filter_matrix` — seed: `sched:{u}:2026-06-10`-keyed outbound, `batch:{u}:0`-keyed outbound, `operator-key-1` outbound, NULL-key outbound (retry-child shape), and an inbound NULL-key call (`create_inbound_call`). `origin="schedule"` → only the sched row; `"batch"` → only the batch row; `"adhoc"` → the operator-key AND NULL-key **outbound** rows, **never the inbound** row (the `direction` guard); `origin=None` → all five.
  - `test_status_direction_elder_filters` — each narrows correctly.
  - `test_created_range_to_exclusive` — a row whose `created_at` equals `created_to` is **excluded**; `created_from` inclusive.
  - `test_limit_clamp_and_offset` — `limit=10_000` silently clamps to 500 (`MAX_ADMIN_CALLS_LIMIT`); `offset` skips rows in the same ordering.
  - `test_elder_join_and_deleted_elder` — rows carry `(call, elder.name, elder.phone_e164)`; after `DELETE FROM elders` (SET NULL), the tuple is `(call, None, None)`.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_admin_calls_repo.py -v` — RED: module missing.

- [ ] Step 3: Implement (async module function, flush-free read; keeps the already-large `repositories/calls.py` untouched):

```python
MAX_ADMIN_CALLS_LIMIT = 500

async def list_calls(db: AsyncSession, *, elder_id: uuid.UUID | None = None,
        status: CallStatus | None = None, direction: CallDirection | None = None,
        origin: str | None = None, created_from: datetime | None = None,
        created_to: datetime | None = None, limit: int = 50,
        offset: int = 0) -> list[tuple[Call, str | None, str | None]]:
    """Admin calls list read model: Call + elder name/phone via outerjoin (spec §4.1).
    `origin` translates to idempotency_key prefix predicates over the reserved
    sched:/batch: namespace (A1); 'adhoc' is direction='outbound' AND (key IS NULL OR
    neither prefix) — the direction guard keeps inbound NULL-key calls out of Ad hoc.
    Documented caveats (spec §4.1): retry children carry no key (match adhoc, response
    origin null — the chain root carries provenance); pre-A1 squatted prefixes ~0.
    Ordered (created_at DESC, id DESC) — served exactly by idx_calls_created."""
    limit = max(1, min(limit, MAX_ADMIN_CALLS_LIMIT))
    stmt = select(Call, Elder.name, Elder.phone_e164).outerjoin(Elder, Call.elder_id == Elder.id)
    ...filters; created_to is EXCLUSIVE (<)...
    if origin == "schedule": stmt = stmt.where(Call.idempotency_key.like("sched:%"))
    elif origin == "batch":  stmt = stmt.where(Call.idempotency_key.like("batch:%"))
    elif origin == "adhoc":
        stmt = stmt.where(Call.direction == CallDirection.OUTBOUND,
            or_(Call.idempotency_key.is_(None),
                and_(Call.idempotency_key.not_like("sched:%"),
                     Call.idempotency_key.not_like("batch:%"))))
    stmt = stmt.order_by(Call.created_at.desc(), Call.id.desc()).limit(limit).offset(offset)
```

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_admin_calls_repo.py -v && ruff check . && uv run mypy`
- [ ] Step 5: `git add -A apps/api && git commit -m "feat(api): admin_calls repository — filtered/paged call list with elder outer-join"`

---

### Task B5: `schemas/admin_calls.py` + `GET /v1/admin/calls` + `main.py` registration

**Files:**
- Create: `apps/api/src/usan_api/schemas/admin_calls.py`, `apps/api/src/usan_api/routers/admin_calls.py`
- Modify: `apps/api/src/usan_api/main.py` (import + `include_router(admin_calls.router)` after `admin_tools`)
- Test: `apps/api/tests/test_admin_calls_api.py`

- [ ] Step 1: Write the failing test — patterns from `test_admin_tools_api.py` (cookie-jar `admin_session`, `_create_elder`/`_enqueue` seed helpers, `mock_dispatch`); add a `_as_viewer(client, async_database_url)` helper copied from `test_admin_users_api.py:66-70` (seed `viewer@example.com` role `viewer`, `issue_session(..., AdminRole.VIEWER, ...)`, set the cookie). Seed `sched:`/`batch:`-keyed calls via `calls_repo.create_call` directly (the public enqueue 422s on reserved prefixes):
  - `test_admin_calls_requires_session` — `GET /v1/admin/calls` → 401.
  - `test_admin_calls_viewer_readable` — viewer cookie → 200 (explicit access policy, spec §1.1/§6.4).
  - `test_admin_calls_list_shape_phi_free` — seed two calls: one bare and one carrying PHI surfaces — `set_recording_uri` to `gs://test-bucket/phi.ogg` + one transcript segment with content `"PHI-SENTINEL-LIST"` (via `transcripts_repo.create_transcript_segments`). Assert: each row has `masked_phone == "***" + phone[-4:]`, `elder_name`, `direction`, `status`, `attempt`, `created_at`; `has_recording` is `False` for the bare row and **`True` for the recorded row**; **`r.text` never contains the seeded full phone, `"gs://"`, or `"PHI-SENTINEL-LIST"`** (non-vacuous: the PHI exists in the DB and must not pass through), and no item has any of the keys `transcript`, `presigned_recording_url`, `recording_uri`, `dynamic_vars`, `idempotency_key`.
  - `test_admin_calls_list_filters_paging_ordering` — `status`/`direction`/`elder_id` filters narrow; `origin=schedule|batch|adhoc` matches the B4 matrix through HTTP (incl. inbound exclusion from adhoc — register one via the repo); `limit`/`offset` page in `(created_at DESC, id DESC)` order; `origin` field in the body parses for `sched:`/`batch:` keys and is `null` for operator keys; **`created_to` is exclusive through HTTP** — read a seeded row's `created_at` from the response, re-query with `created_to=<that exact instant>` → the row is excluded; `created_to=<instant + 1s>` → included (§9 places exclusivity in the HTTP matrix: the router boundary is where an inclusive/exclusive or kwarg-swap bug would hide from the repo test).
  - `test_admin_calls_list_422s` — `status=notastatus` → 422; `created_from=2026-06-11T00:00:00&created_to=2026-06-10T00:00:00` → 422; naive datetimes are accepted (assumed UTC — assert a naive `created_from` filters as its UTC instant).
  - `test_admin_calls_list_elder_deleted_unknown` — delete the elder → `masked_phone == "unknown"`, `elder_name is None`.
  - `test_admin_calls_list_audit_row_phi_free` — `GET /v1/admin/audit?action=calls.list` → entry exists with `entity_type == "call"`, `entity_id is None`, `detail` whose key-set is **exactly** `{"elder_id", "status", "direction", "origin", "created_from", "created_to", "offset", "count"}` (the spec §4.1 shape — seven filters + `count`, **no `limit`**); the detail/entity blob never contains the elder name or any phone digits-tail.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_admin_calls_api.py -v` — RED: 404 (router unregistered).

- [ ] Step 3: Implement.
  `schemas/admin_calls.py` — reuses `TranscriptSegment`, `CallOrigin`, `parse_origin` from `schemas/call.py`; field named `masked_phone` (matching `ElderSummary.masked_phone` — one name for one concept in `types/api.ts`):

```python
class AdminCallSummary(BaseModel):
    id: uuid.UUID; elder_id: uuid.UUID | None
    elder_name: str | None              # names allowed in session-gated bodies (precedent)
    masked_phone: str                   # mask_phone(): "***"+last4, "unknown" if elder gone
    direction: str; status: str
    origin: CallOrigin | None           # parse_origin(idempotency_key)
    attempt: int
    started_at: datetime | None; ended_at: datetime | None
    duration_seconds: int | None; end_reason: str | None
    has_recording: bool                 # recording_uri IS NOT NULL
    created_at: datetime
    # Deliberately absent: transcript, raw phone, recording_uri/presigned URL,
    # dynamic_vars, raw idempotency_key (spec §4.1).

class AdminCallDetail(AdminCallSummary):
    livekit_room: str | None; parent_call_id: uuid.UUID | None
    scheduled_at: datetime | None; answered_at: datetime | None
    recording_status: str | None
    presigned_recording_url: str | None
    recording_url_ttl_s: int | None     # clamped effective TTL when URL present
    transcript: list[TranscriptSegment]
```
  `routers/admin_calls.py` — `APIRouter(prefix="/v1/admin", tags=["admin-calls"], dependencies=[Depends(require_admin_session)])`; `GET /calls` with the spec §4.1 param table (`status: CallStatus | None`, `direction: CallDirection | None`, `origin: Literal["schedule","batch","adhoc"] | None`, `created_from/created_to: datetime | None` run through a local `_assume_utc` (house precedent `ScheduleCallbackRequest`), `from > to` → `HTTPException(422, "created_from must be <= created_to")`, `limit: int = Query(default=50, ge=1, le=500)`, `offset: int = Query(default=0, ge=0)`); a module `_summary(call, elder_name, phone) -> AdminCallSummary` helper using `masking.mask_phone` + `parse_origin`; audit (action `"calls.list"`, `entity_type="call"`, `entity_id=None`, detail = the **seven** filter values stringified + `"count"` — no `limit`, spec §4.1) + commit inside the house `try/except SQLAlchemyError: rollback; raise` guard. Register in `main.py` directly after `admin_tools.router`.

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_admin_calls_api.py tests/test_admin_calls_repo.py -v && ruff check . && uv run mypy`
- [ ] Step 5: `git add -A apps/api && git commit -m "feat(api): GET /v1/admin/calls — paged, filtered, masked, audited call list"`

---

### Task B6: `GET /v1/admin/calls/{call_id}` — detail + transcript + clamped recording URL

**Files:**
- Modify: `apps/api/src/usan_api/routers/admin_calls.py` (sequential after B5)
- Test: `apps/api/tests/test_admin_calls_api.py` (append)

- [ ] Step 1: Write the failing tests (transcripts seeded via `transcripts_repo.create_transcript_segments` with dict segments; recordings via `calls_repo.set_recording_uri`; `GCS_BUCKET` env + `get_settings.cache_clear()` per `test_recording_url.py`; the mocked `generate_signed_url` returns the sentinel `"https://storage.example/SIGNED-SENTINEL"`):
  - `test_admin_call_detail_requires_session` — no cookie → 401 (§9 auth matrix: the list-level 401 does not pin the detail route; a per-route dependency refactor must not drop it silently).
  - `test_admin_call_detail_404` — random UUID → 404 `"call not found"`; junk path UUID → 422.
  - `test_admin_call_detail_transcript_and_fields` — seed the call with `idempotency_key=f"sched:{uuid4()}:2026-06-10"`; segments returned in `(started_at, id)` order with `role`/`content`/`tool_name`/`tool_args`; body includes `livekit_room`, `parent_call_id`, `recording_status`, `elder_name`, `masked_phone`, and **`origin["source"] == "schedule"`** (§9 maps origin parsing to the detail endpoint too — same `_summary` helper, but the row as written was unmapped); body **omits** `dynamic_vars`, `error`, `idempotency_key`, `recording_uri` keys entirely.
  - `test_admin_call_detail_presigned_url_clamped` — monkeypatch `object_storage.generate_signed_url` capturing `(gs_uri, ttl, expected_bucket)` → **`ttl == min(settings.recording_signed_url_ttl_s, 600) == 600`** (settings default 3600), `expected_bucket == "test-bucket"`; body `presigned_recording_url` set and `recording_url_ttl_s == 600`.
  - `test_admin_call_detail_signing_failure_200_null` — `generate_signed_url` raises → 200, `presigned_recording_url is None`, `recording_url_ttl_s is None` (page still renders; WARN captured).
  - `test_admin_call_detail_locked_sink_lines` — loguru capture around one detail GET (transcript + recording): two records whose messages are **exactly** `phi_audit.TRANSCRIPT_ACCESSED` / `phi_audit.RECORDING_URL_ACCESSED`; transcript record `extra` has `call_id`, `client`, `segments`, **`actor == "admin@example.com"`**; recording record has `has_recording is True` + `actor`; **no record's message or extra contains the seeded transcript content string (`"PHI-SENTINEL-..."`) or the `"SIGNED-SENTINEL"` URL value** (pin the concrete sentinel, not "a URL").
  - `test_admin_call_detail_empty_transcript_no_sink_line` — call without transcripts → no `TRANSCRIPT_ACCESSED` record (operator-plane parity).
  - `test_admin_call_detail_audit_row` — `action="calls.get"`, `entity_id == str(call_id)`, `detail == {"segments": n, "has_recording": true}`; PHI-free blob check.
  - `test_admin_call_detail_audit_failure_rolls_back` — monkeypatch `usan_api.routers.admin_calls.admin_audit.record` with a **raise-once wrapper** (first invocation raises `SQLAlchemyError`, subsequent invocations delegate to the saved real function — a permanently-raising mock would also fail the follow-up request and make the recovery assertion impossible) → the first request errors (`pytest.raises(SQLAlchemyError)` — the TestClient re-raises server exceptions) and a follow-up detail GET on the same client, with the wrapper still installed, succeeds (session not left dirty).
  - `test_admin_call_detail_viewer_readable` — viewer cookie → 200 with transcript (policy, spec §6.4).

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_admin_calls_api.py -v` — RED: 404/405 on the detail route.

- [ ] Step 3: Implement `GET /calls/{call_id}` mirroring operator `get_call`'s helper order (spec §4.2): (1) `calls_repo.get_call` → 404; (2) `client_host = client_ip(request)`, `actor` via `Depends(get_actor_email)`; (3) `url = await recording_urls.presigned_recording_url(call, settings, client_host=client_host, actor=actor, max_ttl_s=recording_urls.ADMIN_RECORDING_URL_MAX_TTL_S)`; `ttl_s = min(settings.recording_signed_url_ttl_s, recording_urls.ADMIN_RECORDING_URL_MAX_TTL_S) if url else None`; (4) `transcript = await transcripts_repo.list_for_call(db, call_id)`; if non-empty → `phi_audit.log_transcript_accessed(call_id=call_id, client=client_host, actor=actor, segments=len(transcript))`; (5) one `elders_repo.get_elder` lookup for name/phone; (6) guarded `admin_audit.record(action="calls.get", entity_type="call", entity_id=str(call_id), detail={"segments": len(transcript), "has_recording": call.recording_uri is not None})` + commit; (7) return `AdminCallDetail` built from the B5 `_summary` fields + detail extras + `transcript=[TranscriptSegment.from_model(t) for t in transcript]`.

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_admin_calls_api.py tests/test_recording_url.py tests/test_calls.py -v && ruff check . && uv run mypy`
- [ ] Step 5: `git add -A apps/api && git commit -m "feat(api): GET /v1/admin/calls/{id} — transcript + clamped presigned recording URL, per-access audited"`

---

### Task B7: `Cache-Control: no-store` middleware on `/v1/admin/*`

**Files:**
- Modify: `apps/api/src/usan_api/main.py` (sequential after B5)
- Test: `apps/api/tests/test_app_security.py` (append)

- [ ] Step 1: Write the failing tests:
  - `test_admin_responses_are_no_store` — with `admin_session`: `GET /v1/admin/follow-up-flags` → `response.headers["Cache-Control"] == "no-store"`; **and** an unauthenticated `GET /v1/admin/calls` (401) also carries it (the middleware wraps every admin-path response — transcripts and bearer URLs must never land in a shared nurse workstation's HTTP cache, spec §8).
  - `test_public_routes_not_no_store` — `GET /health` and `GET /v1/calls/{uuid}` (any status) have **no** `Cache-Control` header.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_app_security.py -v` — RED: header absent everywhere.

- [ ] Step 3: Implement in `create_app()` (after `_install_rate_limiting`):

```python
    @app.middleware("http")
    async def _admin_no_store(request: Request, call_next):
        # Transcript JSON and live bearer recording URLs must never be written to a
        # shared workstation's HTTP cache (spec §8). Neither the API nor Caddy sets
        # cache headers otherwise; scoped to the admin plane only.
        response = await call_next(request)
        if request.url.path.startswith("/v1/admin/"):
            response.headers["Cache-Control"] = "no-store"
        return response
```

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_app_security.py -v && ruff check src/usan_api/main.py && uv run mypy`
- [ ] Step 5: `git add -A apps/api && git commit -m "feat(api): Cache-Control no-store on every /v1/admin/* response"`

---

### Task B8: Part B gate

- [ ] Step 4: `cd apps/api && uv run pytest -v && ruff check . && ruff format --check . && uv run mypy` — full suite green (proves the B2/B3 extractions changed nothing on the operator plane) before Part C.

---

## Part C — Queue transitions, list enrichment, summary, SMS masking

### Task C1: Repo layer — `get_*`, status-guarded `update_status`, `count_by_status` + test-module isolation (return shapes of list reads unchanged here; the join lands atomically in C3)

**Files:**
- Modify: `apps/api/src/usan_api/repositories/follow_up_flags.py`, `apps/api/src/usan_api/repositories/callback_requests.py`
- Test: `apps/api/tests/test_follow_up_flags_repo.py` (append + isolation fixture), `apps/api/tests/test_callback_requests_repo.py` (append + isolation fixture)

- [ ] Step 1: Write the failing tests (use each module's existing fixtures/seed helpers), mirrored across both files. **First, add the `test_call_schedules_repo.py`-style autouse TRUNCATE fixture (`TRUNCATE follow_up_flags, callback_requests, calls, elders CASCADE`) to both modules** — unlike `test_call_schedules_repo.py` they have none today, and earlier tests in each module accumulate open rows (e.g. `test_create_and_list_follow_up_flag` + `test_list_flags_respects_limit` leave 3 open flags), which would make any exact global GROUP-BY assertion flaky. The pre-existing tests in both modules seed their own rows and must keep passing under the new fixture. Then:
  - `test_get_flag_returns_row_or_none` (resp. `test_get_request_...`).
  - `test_update_status_guarded_state_machine` — `open→acknowledged` returns the updated row with `status_updated_at` set and `status_updated_by == "nurse@usan.org"`; then `acknowledged→resolved` succeeds; a fresh row `open→resolved` succeeds; **`resolved→acknowledged` returns `None` and the row is unchanged** (the WHERE clause IS the state machine); **same-status `acknowledged→acknowledged` returns `None` with `status_updated_*` untouched** (caller disambiguates no-op vs 409 via `get_*`).
  - `test_count_by_status_groups` — seeded mix → `{"open": 2, "acknowledged": 1, "resolved": 1}` shape (absent statuses omitted or 0 — pin whichever you implement; exact counts are safe thanks to the new TRUNCATE fixture); flags only: `test_count_open_urgent` — counts `status='open' AND severity='urgent'`.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_follow_up_flags_repo.py tests/test_callback_requests_repo.py -v` — RED: `AttributeError: update_status` / `get_flag`.

- [ ] Step 3: Implement (`follow_up_flags.py` shown; `callback_requests.py` mirrors with `CallbackRequest`/`request_id`; ORM `update()` form per spec §4.3 — flush-only, router commits):

```python
_ALLOWED_PREDECESSORS = {"acknowledged": ("open",), "resolved": ("open", "acknowledged")}

async def get_flag(db: AsyncSession, flag_id: int) -> FollowUpFlag | None: ...

async def update_status(db: AsyncSession, flag_id: int, *, new_status: str,
                        actor_email: str) -> FollowUpFlag | None:
    # Single status-guarded UPDATE — the WHERE clause IS the state machine; no
    # read-modify-write race. Zero rows -> caller disambiguates 404/no-op/409.
    stmt = (update(FollowUpFlag)
            .where(FollowUpFlag.id == flag_id,
                   FollowUpFlag.status.in_(_ALLOWED_PREDECESSORS[new_status]))
            .values(status=new_status, status_updated_at=func.now(),
                    status_updated_by=actor_email)
            .returning(FollowUpFlag))
    result = await db.execute(stmt)
    return result.scalar_one_or_none()

async def count_by_status(db: AsyncSession) -> dict[str, int]: ...   # GROUP BY status,
async def count_open_urgent(db: AsyncSession) -> int: ...            # served by idx_*_status
```

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_follow_up_flags_repo.py tests/test_callback_requests_repo.py -v && ruff check . && uv run mypy`
- [ ] Step 5: `git add -A apps/api && git commit -m "feat(api): guarded queue status transitions + count_by_status in flag/callback repos"`

---

### Task C2: PATCH transition endpoints + `QueueStatusUpdateRequest` + workflow stamp fields + transition counter

**Files:**
- Modify: `apps/api/src/usan_api/schemas/admin_tools.py` (add `QueueStatusUpdateRequest`; add `status_updated_at`/`status_updated_by` to **both** summaries)
- Modify: `apps/api/src/usan_api/routers/admin_tools.py` (two PATCH endpoints)
- Modify: `apps/api/src/usan_api/observability/custom_metrics.py` (one counter)
- Test: `apps/api/tests/test_admin_tools_api.py` (append — the §9 transition matrix), `apps/api/tests/test_admin_callback_requests_api.py` (key-set extension — deliberate break, Executor note 5)

- [ ] Step 1: Write the failing tests (flags seeded via the existing `_seed_flag` tool-endpoint helper; callbacks via a direct-repo seed helper modeled on `test_admin_callback_requests_api.py::_seed_callback`; viewer via the B5 `_as_viewer` helper; `from tests.conftest import counter_value` — precedent for counter assertions is **`tests/test_batch_observability.py`**, which imports `counter_value` from conftest; `tests/test_observability.py` uses its own `REGISTRY.get_sample_value` helper and is NOT the pattern to copy). The PATCH endpoints return the summaries, so the stamp fields must be on the schemas **in this task** (they were C3's until review caught the sequencing defect):
  - `test_flag_transition_matrix` — `PATCH /v1/admin/follow-up-flags/{id}` `{"status":"acknowledged"}` → 200 with `status == "acknowledged"`, `status_updated_by == "admin@example.com"`, `status_updated_at` set; then `{"status":"resolved"}` → 200; a fresh flag `open→resolved` → 200.
  - `test_flag_transition_idempotent_noop` — **both idempotent legs (§9 requires both):** (a) repeat an identical `acknowledged` PATCH → **200**, `status_updated_at` byte-identical to before, audit row count for `follow_up_flag.update` **unchanged**; (b) drive a flag to `resolved`, then repeat the identical `resolved` PATCH → same assertions (`resolved→resolved` is the likelier real-world double-click — a terminal state).
  - `test_flag_transition_backward_409` — on a resolved flag, `{"status":"acknowledged"}` → **409** with `detail == "illegal transition: resolved -> acknowledged"` (current status in the detail lets the UI refetch).
  - `test_flag_transition_404_422_401_403` — unknown id → 404 `"flag not found"`; body `{"status":"open"}` → 422 (not a settable target); no session → 401; viewer cookie → **403 and the flag is still `open`** (no DB change).
  - `test_flag_transition_audit_from_to_only` — audit detail `== {"from": "open", "to": "acknowledged"}`; the audit blob never contains the seeded `reason` text.
  - **Callback side — five explicit mirrors (do not collapse them; an under-specified "same matrix" reads as three 200s and nothing else):** `test_callback_transition_matrix` (the three legal 200s with stamps), `test_callback_transition_idempotent_noop` (both ack→ack and resolved→resolved legs, no new `callback_request.update` audit row, stamps untouched), `test_callback_transition_backward_409`, `test_callback_transition_404_422_401_403` (404 `"request not found"`; viewer 403 with no DB change), `test_callback_transition_audit_from_to_only` (`notes` never in the audit blob).
  - `test_transition_metric_after_commit_not_noop` — `counter_value(ADMIN_QUEUE_TRANSITIONS_TOTAL, queue="follow_up_flag", to_status="acknowledged")` +1 after a success, **unchanged** after the idempotent replay; and one callback PATCH increments `counter_value(..., queue="callback_request", to_status="resolved")` (**both** `queue` label values pinned — the callback label combination must not go unasserted).
  - **Deliberate break fix-up:** extend the exact key-set in `test_admin_callback_requests_api.py::test_list_callback_requests` (line 63) with `"status_updated_at"`, `"status_updated_by"` — the fields serialize as `null` on never-transitioned rows. (`test_admin_tools_schemas.py` needs no change in this task: the new schema fields default to `None`, and Pydantic v2 `from_attributes` falls back to defaults for attributes missing on the legacy `_Row` stubs.)

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_admin_tools_api.py tests/test_admin_callback_requests_api.py -v` — RED: 405 (no PATCH route); key-set fails.

- [ ] Step 3: Implement.
  `schemas/admin_tools.py`:

```python
class QueueStatusUpdateRequest(BaseModel):
    status: Literal["acknowledged", "resolved"]   # "open" is not a settable target -> 422

# Both FollowupFlagSummary and CallbackRequestSummary gain (after `status`):
    status_updated_at: datetime | None = None   # NULL = never transitioned past 'open'
    status_updated_by: str | None = None        # admin actor email; defaults keep the legacy
                                                # from_attributes stubs in test_admin_tools_schemas.py valid
```
  `custom_metrics.py` (after `SMS_MESSAGES_TOTAL`; bounded PHI-free labels, increment-after-commit discipline documented):

```python
ADMIN_QUEUE_TRANSITIONS_TOTAL = Counter(
    "usan_admin_queue_transitions",
    "Admin ops-queue status transitions.",
    labelnames=("queue", "to_status"),   # follow_up_flag|callback_request x acknowledged|resolved
)
```
  `routers/admin_tools.py` — two endpoints (flags shown; callbacks mirror with `callback_requests_repo`/`"request"` wording). **Order of operations: read first** — the pre-read serves the early 404 and the audit `"from"`; the WHERE guard still owns transition correctness under races; on zero updated rows, **re-read** before disambiguating (a racing request may have moved the status between the pre-read and the UPDATE — disambiguating on the stale pre-read would mislabel a lost ack/ack race as a 409):

```python
@router.patch("/follow-up-flags/{flag_id}", response_model=FollowupFlagSummary)
async def update_follow_up_flag(flag_id: int, body: QueueStatusUpdateRequest, ...,
        actor: str = Depends(get_actor_email),
        _: object = Depends(require_admin_role(AdminRole.ADMIN))) -> FollowupFlagSummary:
    current = await follow_up_flags_repo.get_flag(db, flag_id)   # pre-read: 404 + audit "from"
    if current is None:
        raise HTTPException(404, "flag not found")
    from_status = current.status
    row = await follow_up_flags_repo.update_status(db, flag_id,
                                                   new_status=body.status, actor_email=actor)
    if row is None:
        fresh = await follow_up_flags_repo.get_flag(db, flag_id)  # re-read: races move status
        if fresh is None:
            raise HTTPException(404, "flag not found")
        if fresh.status == body.status:
            return FollowupFlagSummary.model_validate(fresh)      # idempotent 200 no-op;
                                                                  # no audit row, no metric
        logger.bind(flag_id=flag_id, actor=actor, from_status=fresh.status,
                    to_status=body.status).warning("Illegal queue transition")  # two humans racing
        raise HTTPException(409, f"illegal transition: {fresh.status} -> {body.status}")
    try:
        await admin_audit.record(db, actor_email=actor, action="follow_up_flag.update",
            entity_type="follow_up_flag", entity_id=str(flag_id),
            detail={"from": from_status, "to": body.status})   # status strings ONLY — never reason/notes
        await db.commit()
    except SQLAlchemyError:
        await db.rollback(); raise
    ADMIN_QUEUE_TRANSITIONS_TOTAL.labels(queue="follow_up_flag", to_status=body.status).inc()
    logger.bind(flag_id=flag_id, actor=actor, to_status=body.status).info("Queue item transitioned")
    return FollowupFlagSummary.model_validate(row)
```
  (Under a lost race the audited `"from"` may lag one hop behind the true predecessor — acceptable: the guarded UPDATE, not the audit detail, owns correctness. In C3 the `model_validate(row)` returns switch to the elder-joined `_flag_summary` helpers.)

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_admin_tools_api.py tests/test_admin_callback_requests_api.py tests/test_admin_tools_schemas.py tests/test_batch_observability.py -v && ruff check . && uv run mypy`
- [ ] Step 5: `git add -A apps/api && git commit -m "feat(api): PATCH queue transitions — guarded UPDATE state machine, idempotent no-op, 409 backward, audited + metered"`

---

### Task C3: Queue list enrichment — elder outer-join, severity filter, urgent-first, typed status, offset (atomic repo+schema+router change)

**Files:**
- Modify: `apps/api/src/usan_api/repositories/follow_up_flags.py`, `callback_requests.py` (list reads → join tuples; sequential after C1)
- Modify: `apps/api/src/usan_api/schemas/admin_tools.py` (sequential after C2)
- Modify: `apps/api/src/usan_api/routers/admin_tools.py` (sequential after C2)
- Test: `apps/api/tests/test_admin_tools_api.py` (append + **rewrite one existing test**), `apps/api/tests/test_admin_callback_requests_api.py` (append + key-set extension), `apps/api/tests/test_admin_tools_schemas.py` (**rewrite the three `_Row` stubs**), repo tests (append + **tuple-unpack rewrites**)

- [ ] Step 1: Write the failing tests. The repo return-shape change and the two new required schema fields break four existing surfaces **in addition to** the deliberate 422 change — all six rewrites below are in-scope for this task (Executor note 5; never loosen the implementation to dodge them):
  - Repo (`test_follow_up_flags_repo.py`): `test_list_flags_returns_elder_join_tuples` — `(flag, elder_name, phone_e164)`; `test_list_flags_severity_filter_and_urgent_first` — an urgent flag **older** than a page of routine flags sorts first (`(severity='urgent') DESC, created_at DESC, id DESC`); `test_list_flags_offset`. (`test_callback_requests_repo.py`: join tuple + offset; **ordering unchanged** — assert newest-first still.)
  - **Rewrite (tuple unpacking) — existing repo tests:** `test_follow_up_flags_repo.py::test_create_and_list_follow_up_flag` — the three id-comprehensions over `list_flags()` results (lines 54–63, incl. the `status="closed"` read: the repo keeps accepting arbitrary status strings as a filter — typing lands at the router) become `for (f, _, _) in ...`; `test_callback_requests_repo.py::test_list_callback_requests_filters_by_status` and `::test_list_callback_requests_filters_by_elder` — `[r.id for (r, _, _) in ...]` (the `== []` asserts and the len-only `_respects_limit` tests keep passing as-is).
  - API (`test_admin_tools_api.py`): `test_flags_list_elder_identity_and_workflow_fields` — items carry `elder_name`, `masked_phone == "***"+last4`, `status_updated_at`/`status_updated_by` (null before any transition, populated after a C2 PATCH); **`r.text` never contains the raw phone**; `test_flags_list_severity_filter_and_urgent_first_http`; `test_flags_list_status_junk_422` — `?status=bogus` → **422** (was silent 200-empty; no client sends junk — spec §4.4 deliberate change) and `?severity=high` → 422; `test_flags_list_offset_paging`; `test_flags_audit_detail_gains_offset_and_severity` — `follow_up_flags.list` audit detail now carries `"offset"` and `"severity"` keys (still PHI-free).
  - **Rewrite** `test_follow_up_flags_list_and_filter`: replace the `status=closed` lookup with `status=resolved` (valid literal, returns 200-empty for the open seed) **and** add `assert client.get("/v1/admin/follow-up-flags?status=closed").status_code == 422` — the pinned behavior change.
  - `test_admin_callback_requests_api.py` append: `test_callbacks_list_elder_identity_and_offset` — same shape as the flags test, no severity, **and the explicit raw-phone negative: `phone not in r.text`** (§9's "never the raw phone" applies to both queues — do not leave it implied); `test_callbacks_list_status_junk_422`. **Extend the exact key-set in `test_list_callback_requests` again** with `"elder_name"`, `"masked_phone"` (now 12 keys).
  - **Rewrite `test_admin_tools_schemas.py`:** the three `_Row` stubs gain `elder_name = None` and `masked_phone = "***4567"` class attrs (`masked_phone: str` is required — it is computed by the router helpers via `mask_phone`, never read off an ORM row, so the stubs must carry it for `from_attributes` validation); assert the new fields round-trip.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_admin_tools_api.py tests/test_admin_callback_requests_api.py tests/test_admin_tools_schemas.py tests/test_follow_up_flags_repo.py tests/test_callback_requests_repo.py -v` — RED: tuples/fields/422s all absent.

- [ ] Step 3: Implement.
  Repos — `list_flags(db, *, status=None, elder_id=None, severity=None, limit=100, offset=0) -> list[tuple[FollowUpFlag, str | None, str | None]]` via `select(FollowUpFlag, Elder.name, Elder.phone_e164).outerjoin(Elder, FollowUpFlag.elder_id == Elder.id)` (same shape as `admin_calls`); flags ordering `(FollowUpFlag.severity == "urgent").desc(), created_at.desc(), id.desc()` with a comment: not served by `idx_followup_flags_status` — acceptable at current volumes, revisit with a partial index at tens of thousands (spec §4.4). `list_callback_requests` gains the join + `offset`, ordering unchanged.
  `schemas/admin_tools.py` — both summaries gain `elder_name: str | None`, `masked_phone: str` (a nurse seeing "urgent / medical / chest pain" must not need an audited transcript read just to learn *who* — spec §4.4; the `status_updated_*` fields landed in C2).
  `routers/admin_tools.py` — list params become `status: Literal["open","acknowledged","resolved"] | None = Query(default=None)` (both) and `severity: Literal["routine","urgent"] | None` + `offset: int = Query(default=0, ge=0)`; build summaries via module helpers `_flag_summary(row, elder_name, phone)` / `_callback_summary(...)` constructing the models field-by-field with `masking.mask_phone(phone)` (a `model_validate(row).model_copy(...)` two-step cannot work: `masked_phone` is required and absent on the row); **the C2 PATCH endpoints switch to the same helpers** (one `elders_repo.get_elder` lookup for the response row); audit details gain `"offset"` (+ `"severity"` for flags); `*.list` audit actions/behavior otherwise untouched.

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_admin_tools_api.py tests/test_admin_callback_requests_api.py tests/test_admin_tools_schemas.py tests/test_follow_up_flags_repo.py tests/test_callback_requests_repo.py -v && ruff check . && uv run mypy`
- [ ] Step 5: `git add -A apps/api && git commit -m "feat(api): queue lists — elder identity, severity filter, urgent-first, typed status (422), offset"`

---

### Task C4: `GET /v1/admin/queues/summary` — PHI-free counts, no audit row

**Files:**
- Modify: `apps/api/src/usan_api/schemas/admin_tools.py` (sequential after C3), `apps/api/src/usan_api/routers/admin_tools.py` (sequential after C3)
- Test: `apps/api/tests/test_admin_tools_api.py` (append)

- [ ] Step 1: Write the failing tests:
  - `test_queues_summary_counts_match_seeds` — seed 2 open flags (1 urgent), 1 acknowledged flag, 1 resolved flag, 1 open callback, 1 acknowledged callback → body `== {"flags_open": 2, "flags_open_urgent": 1, "flags_acknowledged": 1, "callbacks_open": 1, "callbacks_acknowledged": 1}`.
  - `test_queues_summary_viewer_readable_no_audit_no_phi` — viewer cookie → 200; the response contains **no** name/phone/reason strings (counts only); the `/v1/admin/audit` row count is identical before and after the call (**deliberately un-audited**: PHI-free aggregate backing tab badges, may be refetched often — spec §4.5).
  - `test_queues_summary_requires_session` — 401.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_admin_tools_api.py -v` — RED: 404.

- [ ] Step 3: Implement — `QueuesSummary` schema (the five int fields, spec §4.5); `GET /queues/summary` in `routers/admin_tools.py` composing `follow_up_flags_repo.count_by_status` + `count_open_urgent` + `callback_requests_repo.count_by_status` (`.get("open", 0)` shape); no `admin_audit.record`, no commit, no sink line.

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_admin_tools_api.py -v && ruff check . && uv run mypy`
- [ ] Step 5: `git add -A apps/api && git commit -m "feat(api): PHI-free /v1/admin/queues/summary counts (un-audited by design)"`

---

### Task C5: SMS list masking fix (`to_number`)

**Files:**
- Modify: `apps/api/src/usan_api/routers/admin_tools.py` (sequential after C4)
- Test: `apps/api/tests/test_admin_sms_messages_api.py` (append)

- [ ] Step 1: Write the failing test — `test_sms_list_masks_to_number`: seed via the existing `_seed` (raw E.164 phone), `GET /v1/admin/sms-messages` → the row's `to_number == "***" + phone[-4:]` and **`phone not in r.text`** (the one violation of "masked phones only on this plane", spec §4.6). Existing tests (`omits_body`, `status_filter`, key-set superset) must keep passing unmodified.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_admin_sms_messages_api.py -v` — RED: raw E.164 in the body.

- [ ] Step 3: Implement — in `list_sms_messages`, replace the return with:

```python
    return [
        SmsMessageSummary.model_validate(r).model_copy(
            update={"to_number": mask_phone(r.to_number)}   # spec §4.6: field keeps its
        )                                                    # name; content becomes masked
        for r in rows
    ]
```
  Audit behavior unchanged.

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_admin_sms_messages_api.py -v && ruff check . && uv run mypy`
- [ ] Step 5: `git add -A apps/api && git commit -m "fix(api): mask elder phone in GET /v1/admin/sms-messages (PHI gap from PR #54 review)"`

---

### Task C6: Part C gate

- [ ] Step 4: `cd apps/api && uv run pytest -v && ruff check . && ruff format --check . && uv run mypy` — green before Part D.

---

## Part D — Admin-UI: api.patch + types + Calls pages + Queues page + nav

### Task D1: `api.patch`, mirrored types, `lib/format.ts`

**Files:**
- Modify: `apps/admin-ui/src/lib/api.ts`, `apps/admin-ui/src/types/api.ts`
- Create: `apps/admin-ui/src/lib/format.ts`
- Test: `apps/admin-ui/src/test/api.test.ts` (append), `apps/admin-ui/src/test/format.test.ts` (new)

- [ ] Step 1: Write the failing tests:
  - `api.test.ts` append — `it("sends PATCH with a JSON body")`: stub fetch (existing `mockFetch` helper), `await api.patch("/v1/admin/follow-up-flags/1", { status: "acknowledged" })`, assert fetch called with `method: "PATCH"`, `Content-Type: application/json`, body `JSON.stringify({ status: "acknowledged" })`.
  - `format.test.ts` — `fmtDate` parses an ISO string via `toLocaleString` and returns the input verbatim on junk (the `AuditPage` behavior, now shared); `fmtDuration(null) === "—"`, `fmtDuration(0) === "0:00"`, `fmtDuration(61) === "1:01"`, `fmtDuration(3725) === "62:05"` (minutes unbounded, zero-padded seconds).

- [ ] Step 2: `cd apps/admin-ui && npm run test -- src/test/api.test.ts src/test/format.test.ts` — RED: `api.patch` is not a function (TS error in test) / format module missing.

- [ ] Step 3: Implement.
  `lib/api.ts` — one line in the `api` object: `patch: <T>(u: string, b?: unknown) => request<T>("PATCH", u, b),`.
  `lib/format.ts` — `fmtDate(iso: string): string` (lifted from `AuditPage.tsx`; existing pages NOT migrated — three new pages justify ending the duplication going forward, spec §2.1) and `fmtDuration(seconds: number | null): string`.
  `types/api.ts` — extend the header comment's mirrored-files list with `schemas/admin_calls.py` and `schemas/admin_tools.py`, then add (nullability/value sets exactly matching the server; `masked_phone` everywhere):

```ts
export type QueueStatus = "open" | "acknowledged" | "resolved";
export interface CallOrigin { source: "schedule" | "batch"; id: string; ordinal: string | number; }
export interface TranscriptSegment { role: string; content: string; tool_name: string | null;
  tool_args: Record<string, unknown> | null; started_at: string; ended_at: string | null; }
export interface AdminCallSummary { id: string; elder_id: string | null; elder_name: string | null;
  masked_phone: string; direction: string; status: string; origin: CallOrigin | null; attempt: number;
  started_at: string | null; ended_at: string | null; duration_seconds: number | null;
  end_reason: string | null; has_recording: boolean; created_at: string; }
export interface AdminCallDetail extends AdminCallSummary { livekit_room: string | null;
  parent_call_id: string | null; scheduled_at: string | null; answered_at: string | null;
  recording_status: string | null; presigned_recording_url: string | null;
  recording_url_ttl_s: number | null; transcript: TranscriptSegment[]; }
export interface FollowupFlagSummary { id: number; call_id: string; elder_id: string;
  elder_name: string | null; masked_phone: string; severity: string; category: string;
  reason: string | null; status: QueueStatus; status_updated_at: string | null;
  status_updated_by: string | null; created_at: string; }
export interface CallbackRequestSummary { id: number; call_id: string; elder_id: string;
  elder_name: string | null; masked_phone: string; requested_time_text: string;
  requested_at: string | null; notes: string | null; status: QueueStatus;
  status_updated_at: string | null; status_updated_by: string | null; created_at: string; }
export interface QueuesSummary { flags_open: number; flags_open_urgent: number;
  flags_acknowledged: number; callbacks_open: number; callbacks_acknowledged: number; }
```

- [ ] Step 4: `cd apps/admin-ui && npm run test -- src/test/api.test.ts src/test/format.test.ts && npm run typecheck && npm run lint`
- [ ] Step 5: `git add apps/admin-ui && git commit -m "feat(admin-ui): api.patch + A2 type mirrors + shared fmtDate/fmtDuration"`

---

### Task D2: `CallsPage` + `useCalls` + `/calls` route

**Files:**
- Create: `apps/admin-ui/src/features/calls/hooks.ts`, `apps/admin-ui/src/features/calls/CallsPage.tsx`
- Modify: `apps/admin-ui/src/routes.tsx` (add `{ path: "calls", element: <CallsPage /> }` in the `PageLayout` group)
- Test: `apps/admin-ui/src/test/CallsPage.test.tsx`

- [ ] Step 1: Write the failing test — `vi.mock("../lib/api")` route-by-URL (`ToolsSection.test.tsx` style: a `getMock(url)` switching on the path), wrapped in `QueryClientProvider` (fresh client, `retry: false`) + `MemoryRouter` with a stub `/calls/:id` route rendering `DETAIL`:
  - `renders masked phone, elder name and origin badges` — a sched-origin row shows `Schedule`, a batch row `Batch`, an **inbound row with `origin: null` shows `Inbound`**, an outbound null-origin row shows `Ad hoc` (assert the badge **text**); `***4567` visible; the raw phone never rendered (it's never in the payload — assert `masked_phone` text only).
  - `filter change resets offset to 0` — return 50 rows (PAGE_SIZE) → click Next (request URL contains `offset=50`) → change the Status select → next request URL contains `offset=0`.
  - `To date is sent exclusive (+1 day) and labeled inclusive` — set To = `2026-06-10` → request URL contains `created_to=2026-06-11`; the label reads "To (inclusive)" (otherwise To=2026-06-10 silently drops June 10's calls).
  - `honors elder_id from the URL` — `initialEntries: ["/calls?elder_id=<uuid>"]` → request URL contains `elder_id=<uuid>` (deep-link target; no picker this phase).
  - `hasNext heuristic` — 50 rows → Next enabled; 3 rows → disabled; Prev disabled at offset 0; range text `1–50`.
  - `row click navigates to detail` — click a row → `DETAIL` rendered.
  - `loading / error / empty states` — pending → Spinner copy; rejected (`ApiError`) → red message; `[]` → "No calls match these filters".

- [ ] Step 2: `cd apps/admin-ui && npm run test -- src/test/CallsPage.test.tsx` — RED: module not found.

- [ ] Step 3: Implement.
  `features/calls/hooks.ts`:

```ts
export const PAGE_SIZE = 50;
export interface CallsFilters { elderId?: string; status?: string; direction?: string;
  origin?: string; createdFrom?: string; createdTo?: string; }
// Query keys carry UUIDs/enums/dates ONLY — never names or phones (spec §6.5).
export function useCalls(filters: CallsFilters, limit: number, offset: number) {
  ...URLSearchParams; queryKey: ["admin-calls", filters, limit, offset];
  api.get<AdminCallSummary[]>(`/v1/admin/calls?${params}`)...   // global focus-refetch off
}
```
  `CallsPage.tsx` — EldersPage/AuditPage hybrid (spec §5.2): `useSearchParams()` for `elder_id`; `useState(offset)`; filter bar of `Select`s — Status (the 11 `CallStatus` values + All), Direction (All/Outbound/Inbound), Origin (All/Schedule/Batch/Ad hoc) — and two `<Input type="date">` (From / "To (inclusive)" — the To value gets `+1 day` before being sent, since `created_to` is exclusive server-side); every filter change resets offset to 0; table columns Created (`fmtDate`), Elder (name + `masked_phone`), Direction, Origin `Badge` (null → "Inbound" when `direction === "inbound"` else "Ad hoc"), Status `Badge`, Attempt, Duration (`fmtDuration`), recording indicator (`has_recording` → "●"/aria-label "has recording"); row click `useNavigate()(/calls/${id})`; Prev/Next + `rangeStart–rangeEnd` with the `length === PAGE_SIZE` hasNext heuristic; `Spinner`/red `<p>`/empty states per house pattern.

- [ ] Step 4: `cd apps/admin-ui && npm run test -- src/test/CallsPage.test.tsx && npm run typecheck && npm run lint`
- [ ] Step 5: `git add apps/admin-ui && git commit -m "feat(admin-ui): /calls console — filtered, paged, masked call list"`

---

### Task D3: `TranscriptViewer` + `RecordingPlayer` + `CallDetailPage` + `/calls/:id` route

**Files:**
- Create: `apps/admin-ui/src/features/calls/TranscriptViewer.tsx`, `RecordingPlayer.tsx`, `CallDetailPage.tsx`
- Modify: `apps/admin-ui/src/features/calls/hooks.ts` (append `useCall`; sequential after D2), `apps/admin-ui/src/routes.tsx` (add `{ path: "calls/:id", element: <CallDetailPage /> }`)
- Test: `apps/admin-ui/src/test/TranscriptViewer.test.tsx`, `RecordingPlayer.test.tsx`, `CallDetailPage.test.tsx`

- [ ] Step 1: Write the failing tests (presentational components first — pure props, no api mock needed). **Assert semantic markers, not Tailwind classes** (Executor note 7): segment cards render a `data-role` attribute; styling classes are implementation and stay un-asserted (a pure restyle must not break these tests, and a broken style with intact class names must not pass them):
  - `TranscriptViewer.test.tsx` — assistant segment card has `data-role="assistant"`, user segment card `data-role="user"` (the role-styled alignment/accent classes hang off these attributes in the implementation); a tool segment has `data-role="tool"` and renders a monospace chip with `tool_name` plus a **collapsed `<details>`** containing the `tool_args` JSON; per-segment `started_at` timestamp rendered via `fmtDate`; empty + `callStatus="in_progress"` → "Call still in progress — transcript appears after the call ends."; empty + `"completed"` → "No transcript was captured for this call."; no virtualization (segments ≤1000 by server cap — render 3, assert all 3).
  - `RecordingPlayer.test.tsx` — `url` present → an `<audio controls preload="none">` whose `src` is the prop URL, plus the TTL note "Recording link expires in ~10 min — reload the page for a fresh link." (`Math.round(600/60)`); `hasRecording` true + `url` null → "Recording exists but no playback link is available right now." (deliberately generic — null covers signing failure AND unconfigured bucket); `callStatus="in_progress"` → "Call still in progress — recording appears after the call ends."; otherwise → "No recording for this call.". The URL appears **only** as the audio `src` (assert it is not rendered as text).
  - `CallDetailPage.test.tsx` (api mocked, `MemoryRouter` at `/calls/<id>` with the route) — loading Spinner; **`ApiError(404)` → distinct "Call not found" copy** (stale queue links happen); `ApiError(500, "boom")` → red block containing "boom"; success → header shows elder name + masked phone with the name linking to `/calls?elder_id=<elder_id>` (assert `href`), direction/status/origin/attempt, timestamps, duration, end reason; `parent_call_id` set → "attempt N — view parent" link to `/calls/<parent_id>`.

- [ ] Step 2: `cd apps/admin-ui && npm run test -- src/test/TranscriptViewer.test.tsx src/test/RecordingPlayer.test.tsx src/test/CallDetailPage.test.tsx` — RED: modules missing.

- [ ] Step 3: Implement per spec §5.3:
  - `hooks.ts` append: `useCall(id: string)` — key `["admin-call", id]`, `api.get<AdminCallDetail>(...)`; **do not opt into focus-refetch** (each detail fetch re-signs a bearer URL and writes audit rows; the global default `refetchOnWindowFocus: false` stands — add the comment).
  - `TranscriptViewer({ segments, callStatus })` — each segment card carries `data-role={segment.tool_name ? "tool" : segment.role}` (the tests' semantic hook); role-styled alignment/accent via classes keyed off the same value. `RecordingPlayer({ url, ttlS, hasRecording, callStatus })` — the URL lives only in props, never in query keys/localStorage/`console.*`.
  - `CallDetailPage` — `useParams`, `useCall`; 404 detection via `(error as ApiError).status === 404`; renders header card + `RecordingPlayer` + `TranscriptViewer` inside `PageLayout` scrolling (no full-height escape hatch).

- [ ] Step 4: `cd apps/admin-ui && npm run test -- src/test/TranscriptViewer.test.tsx src/test/RecordingPlayer.test.tsx src/test/CallDetailPage.test.tsx src/test/CallsPage.test.tsx && npm run typecheck && npm run lint`
- [ ] Step 5: `git add apps/admin-ui && git commit -m "feat(admin-ui): /calls/:id detail — transcript viewer + status-aware recording player"`

---

### Task D4: `QueuesPage` + `QueueTable` + queue hooks + `/queues` route

**Files:**
- Create: `apps/admin-ui/src/features/queues/hooks.ts`, `QueueTable.tsx`, `QueuesPage.tsx`
- Modify: `apps/admin-ui/src/routes.tsx` (add `{ path: "queues", element: <QueuesPage /> }`)
- Test: `apps/admin-ui/src/test/QueuesPage.test.tsx`

- [ ] Step 1: Write the failing test (api mocked route-by-URL **including** `/v1/auth/me` so viewer/admin gating is driven by the real `useIsAdmin` query — return `{ email, role: "admin" }` or `"viewer"` per test):
  - `tab, status and offset sync to the URL` — `initialEntries: ["/queues?tab=callbacks&status=resolved&offset=50"]` → the callbacks request URL contains `status=resolved&...offset=50`; clicking the Flags tab issues a flags request and the search params update (assert via a location-probe child or the next request URL).
  - `tab labels carry summary counts` — summary `{flags_open: 2, flags_open_urgent: 1, callbacks_open: 3, ...}` → labels "Follow-up flags (2 open, 1 urgent)" / "Callbacks (3 open)".
  - `default status filter is Open` — first flags request URL contains `status=open`.
  - `urgent rows are semantically marked` — urgent row carries `data-severity="urgent"` and its severity `Badge` renders the text "urgent"; routine row carries `data-severity="routine"` with badge text "routine". Assert the data attribute + badge text — **never the Tailwind border/fill classes** (the red left-border + filled-badge styling hangs off `data-severity` in the implementation; a restyle must not break this test).
  - `viewer sees no actions; admin sees them` — role `viewer` → no "Acknowledge"/"Resolve" buttons (**hidden, not disabled**); role `admin` → both visible on an `open` row, only "Resolve" on an `acknowledged` row.
  - `acknowledge calls api.patch and disables while pending` — click Acknowledge → `patchMock` called with (`/v1/admin/follow-up-flags/<id>`, `{status:"acknowledged"}`); while the promise is unresolved the button is disabled (double-click guard; the server is also idempotent).
  - `resolve goes through ConfirmDialog` — click Resolve → dialog appears (resolution is one-way); confirm → patch `{status:"resolved"}`.
  - `409 refetches and toasts` — patch rejects with `ApiError(409, ...)` → "Status changed elsewhere — list refreshed" toast appears (assert via rendered `ErrorToast` or by spying `pushToast`) and the flags GET fires again (invalidate).
  - `queue hooks refetch on window focus` — harness rendering `useFollowUpFlags("open", undefined, 50, 0)` with a `staleTime: 0` client; `focusManager.setFocused(false)` then `(true)` → a second fetch occurs (pins the per-query `refetchOnWindowFocus: true` opt-in against the global `false` default).
  - `Refresh button refetches` — click Refresh → another flags GET.
  - `empty copy` — flags `[]` with status Open → "No open follow-up flags — all clear."; with a non-default filter → "No flags match these filters".

- [ ] Step 2: `cd apps/admin-ui && npm run test -- src/test/QueuesPage.test.tsx` — RED: modules missing.

- [ ] Step 3: Implement per spec §5.4:
  - `hooks.ts` — `useFollowUpFlags(status, severity, limit, offset)` key `["admin-flags", status, severity, limit, offset]`; `useCallbackRequests(status, limit, offset)` key `["admin-callbacks", ...]`; `useQueuesSummary()` key `["admin-queues-summary"]` — **all three set `refetchOnWindowFocus: true`** (this page is what a Grafana alert sends a nurse into; the global default stays `false` elsewhere; comment it). Mutations `useUpdateFlagStatus()` / `useUpdateCallbackStatus()`: `api.patch`, `onSuccess` → `invalidateQueries({ queryKey: ["admin-flags"] })` / `["admin-callbacks"]` / `["admin-queues-summary"]`; `onError(err)` → `err.status === 409 ? (pushToast("Status changed elsewhere — list refreshed", "info"), invalidate same keys) : pushToast(err.detail)`.
  - `QueuesPage.tsx` — `useSearchParams()` owns `tab`/`status`/`severity`/`offset` (back-navigation from a call detail restores the nurse's exact position); `Tabs` component with labels from `useQueuesSummary()`; status `Select` (Open default / Acknowledged / Resolved / All), severity `Select` on the flags tab (All/Urgent/Routine); `PAGE_SIZE = 50` Prev/Next; manual Refresh button calling each query's `refetch()`; no `refetchInterval` (non-goal).
  - `QueueTable.tsx` — generic table used by both tabs; each row carries `data-severity` (flags tab) — the urgent styling (`border-l-4 border-red-500` row + filled red badge; routine = outline badge) keys off it. Flags columns: Created, Elder (name + `masked_phone`, name → `/calls?elder_id=…`), Severity, Category, Reason, Status (+ `status_updated_by` / `fmtDate(status_updated_at)` when set), "View call" → `/calls/<call_id>`, Actions. Callbacks columns: Created, Elder, Requested time (`requested_time_text` verbatim + `fmtDate(requested_at)` when present), Notes, Status, View call, Actions (resolving = the nurse dials out-of-band; the console never shows a dialable number — spec §1.1/Q9). Actions gated `useIsAdmin()` (hidden for viewers): Acknowledge when `open`; Resolve when `open`/`acknowledged` behind `ConfirmDialog`; buttons `disabled={mutation.isPending}`.

- [ ] Step 4: `cd apps/admin-ui && npm run test -- src/test/QueuesPage.test.tsx && npm run typecheck && npm run lint`
- [ ] Step 5: `git add apps/admin-ui && git commit -m "feat(admin-ui): /queues — flags/callbacks triage with URL-synced state, badges, admin-gated transitions"`

---

### Task D5: NavSidebar `Operate` group

**Files:**
- Modify: `apps/admin-ui/src/components/NavSidebar.tsx`
- Test: `apps/admin-ui/src/test/NavSidebar.test.tsx` (new)

- [ ] Step 1: Write the failing test (api mocked for `/v1/auth/me`): as **viewer** — the `Operate` heading renders with links "Calls" (`href="/calls"`) and "Queues" (`href="/queues"`) — neither is `adminOnly` (viewers triage read-only; mutation affordances are gated in-page); regression: "Elders" stays hidden for the viewer, visible for admin; group order is Build, Config, Operate, System.

- [ ] Step 2: `cd apps/admin-ui && npm run test -- src/test/NavSidebar.test.tsx` — RED: no Operate group.

- [ ] Step 3: Implement — insert into `GROUPS` between `Config` and `System`:

```ts
  {
    heading: "Operate",
    items: [
      { to: "/calls", label: "Calls" },
      { to: "/queues", label: "Queues" },
    ],
  },
```

- [ ] Step 4: `cd apps/admin-ui && npm run test -- src/test/NavSidebar.test.tsx && npm run typecheck && npm run lint`
- [ ] Step 5: `git add apps/admin-ui && git commit -m "feat(admin-ui): Operate nav group — Calls + Queues (viewer-visible)"`

---

### Task D6: Part D gate

- [ ] Step 4: `cd apps/admin-ui && npm run build && npm run lint && npm run test` — tsc + eslint (`--max-warnings 0`) + full vitest green before Part E.

---

## Part E — Full-suite verification + spec-conformance pass

### Task E1: Gates, untouched-surface checks, §6/§9 conformance audit

**Files:** none (verification only).

- [x] Step 4: Run, in order, every command green. **Every line carries an absolute `cd`** so the block stays correct whether each line runs in its own bash call (subagent cwd resets) or the whole block runs top-to-bottom as one script:

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api && uv run pytest -v --tb=short && ruff check . && ruff format --check . && uv run mypy
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/admin-ui && npm run build && npm run lint && npm run test
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/services/agent && uv run pytest -v && ruff check . && uv run mypy   # untouched but must stay green
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine && python3 -m pytest scripts/tests -v --tb=short   # needs pytest+pyyaml on python3 (CI does `pip install pytest pyyaml`, test.yml:50); if missing: python3 -m pip install pytest pyyaml
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine && git diff --name-only feat/batch-calling...HEAD -- services/agent   # MUST print nothing. Pre-rebase base is feat/batch-calling; AFTER the post-#55 rebase, re-run as `git diff --name-only origin/main...HEAD -- services/agent` (the local stacked branch goes stale and would false-pass/false-fail)
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api && uv run --with pytest-cov pytest tests/ \
  --cov=usan_api.phi_audit --cov=usan_api.recording_urls --cov=usan_api.masking \
  --cov=usan_api.repositories.admin_calls --cov=usan_api.routers.admin_calls \
  --cov=usan_api.schemas.admin_calls \
  --cov-report=term-missing --cov-fail-under=80
```

  The coverage command is a **hard gate**: no `|| true`, `--cov-fail-under=80`, `--cov` scoped to the new api modules only so well-covered legacy modules cannot mask a gap (spec §9 "coverage ≥80% on new modules").

  Then walk the **§6/§9 conformance checklist** and tick each row against a passing test (fix any gap before declaring done):
  - [x] Locked-sink strings verbatim + single source → `test_phi_audit.py::test_locked_sink_strings_verbatim`; admin emission with actor/client/call_id and no content → `test_admin_call_detail_locked_sink_lines`; empty transcript → no line → `test_admin_call_detail_empty_transcript_no_sink_line`
  - [x] URLs never logged — **non-vacuous** sentinel negatives (`"SIGNED-SENTINEL"` absent from every captured record where the mocked signer actually returned it) → `test_recording_urls.py::test_success_emits_locked_sink_line_actor_optional` + `test_admin_call_detail_locked_sink_lines`; signing offloaded via `asyncio.to_thread` → `test_recording_urls.py::test_signing_called_via_thread`
  - [x] Operator plane bit-identical after extraction → `test_recording_url.py` + `test_calls.py` + `test_call_transcript.py` passing **unmodified**, plus `test_operator_get_call_records_never_bind_actor` and `test_recording_urls.py::test_ttl_unclamped_without_max`
  - [x] Admin TTL clamp ≤600 s + `recording_url_ttl_s` → `test_admin_call_detail_presigned_url_clamped`; signing failure degrades to null URL → `..._signing_failure_200_null`
  - [x] DB audit rows (actor email, PHI-free detail, same commit) for `calls.list` / `calls.get` / `*.update` → B5/B6/C2 audit tests; rollback-guard **pinned at `calls.get`** (`test_admin_call_detail_audit_failure_rolls_back`) — `calls.list` and the two PATCHes use the identical house `try/except SQLAlchemyError` guard but are not separately rollback-pinned (verify by code review of the guard blocks); `queues/summary` un-audited → `test_queues_summary_viewer_readable_no_audit_no_phi`
  - [x] Masked phones only, raw E.164 never serialized → `test_admin_calls_list_shape_phi_free` (with seeded recording URI + transcript sentinel — non-vacuous), `test_flags_list_elder_identity_and_workflow_fields`, `test_callbacks_list_elder_identity_and_offset` (explicit raw-phone negative), `test_sms_list_masks_to_number`; elder-gone → `"unknown"` → `test_admin_calls_list_elder_deleted_unknown`
  - [x] Auth/role matrices (401 on list **and detail** / viewer-200 reads / viewer-403 mutations with no DB change) → `test_admin_calls_requires_session`, `test_admin_call_detail_requires_session`, B5/B6 viewer tests, `test_flag_transition_404_422_401_403`, `test_callback_transition_404_422_401_403`
  - [x] Transition matrix ×2 queues: 3 legal 200s, idempotent same-status 200 no-ops for **both** `acknowledged→acknowledged` AND `resolved→resolved` (no audit row, `status_updated_*` untouched, no metric), backward 409 with current status in detail, 404, 422 on `"open"` → the five flag tests + five callback mirrors in C2; guarded-UPDATE semantics under sequential conflict → repo `test_update_status_guarded_state_machine`
  - [x] Metric after commit, never on no-ops, **both `queue` label values** → `test_transition_metric_after_commit_not_noop`
  - [x] Origin filter ×3 incl. inbound exclusion + retry-child caveat → `test_origin_filter_matrix` (repo) + `test_admin_calls_list_filters_paging_ordering` (HTTP); origin parsed on the **detail** body too → `test_admin_call_detail_transcript_and_fields`
  - [x] `created_to` exclusive at the repo (`test_created_range_to_exclusive`) **and through HTTP** (`test_admin_calls_list_filters_paging_ordering` — the router boundary is where kwarg-swap bugs hide); `from > to` 422 / naive→UTC → B5 tests
  - [x] Audit detail shapes pinned: `calls.list` detail key-set is exactly the seven spec §4.1 filters + `count` (no `limit`) → `test_admin_calls_list_audit_row_phi_free`
  - [x] Queue list: offset, severity, urgent-first, junk-status 422 (deliberate change pinned in the rewritten existing test) → C3 tests
  - [x] Migration contract incl. subprocess normalize roundtrip + `idx_calls_created` → `test_ops_queue_migration.py`
  - [x] `Cache-Control: no-store` on admin, absent on public → `test_app_security.py` B7 tests
  - [x] UI: masked phone + origin badge text, To+1day, offset reset, hasNext, 404 detail state, `data-role`-marked transcript segments + tool chips, status-aware recording states + TTL note, URL-synced queue state, summary badges, `data-severity`-marked urgent rows, viewer-hidden actions, pending-disable, Resolve confirm, 409 toast+invalidate, focus-refetch opt-in, `api.patch` → D1–D5 vitest files (semantic markers asserted, never Tailwind classes)

- [x] Step 5: Commit anything outstanding, then stop. **Do not tag, deploy, or open the PR from this plan** until the stacked-branch mechanics are settled (Executor note 1): after PR #55 squash-merges, rebase, re-verify `alembic heads` is exactly `0012` and `0013.down_revision` matches, re-run the services/agent diff check against `origin/main...HEAD`, then open the A2 PR. Rollout (spec §10) is additive — no feature flag, no env refresh, no Terraform; the entrypoint auto-runs `alembic upgrade head`; post-deploy validation follows §10.4 (viewer list → detail+sink check in Cloud Logging → ack/resolve flow → viewer 403 → tab counts + SMS masking), which also discharges the carried-forward Phase 2/3 live-call validation debt.

---

## Files read (for reference)
- Spec: `/Users/evgenii.vasilenko/gofrolist/usan-voice-engine/docs/superpowers/specs/2026-06-10-calls-ui-ops-queues-design.md` (full)
- Format reference: `docs/superpowers/plans/2026-06-10-plan-batch-calling.md` (full, incl. Review disposition)
- `apps/api/src/usan_api/`: `routers/calls.py` (`_presigned_recording_url` 189–215, `get_call` 218–244 — the locked-sink emission sites), `routers/admin_tools.py` (all three list endpoints + audit guards), `routers/admin_elders.py` (`_mask` 24–25, role-gate pattern 49–56), `repositories/{follow_up_flags,callback_requests,sms_messages,transcripts,admin_audit,elders,calls}.py`, `schemas/{admin_tools,call}.py` (`TranscriptSegment`, `CallOrigin`, `parse_origin`), `db/models.py` (Call 73–132, AdminAuditLog 334–347, FollowUpFlag 350–368, CallbackRequest 371–387), `db/base.py` (CallStatus/CallDirection/AdminRole), `main.py` (router registration + middleware seam), `settings.py` (`recording_signed_url_ttl_s` 72–74), `object_storage.py` (`generate_signed_url` 59–71), `auth.py` (`require_admin_session` 144, `require_admin_role` 181–189), `admin_actor.py`, `ratelimit.py` (`_is_operator_route` 31–45 — `/v1/admin/` already matched), `observability/custom_metrics.py` (counter house style)
- `apps/api/pyproject.toml` (`[tool.mypy] files = ["src"]` — why every gate runs bare `uv run mypy`); same in `services/agent/pyproject.toml`
- `apps/api/migrations/versions/0012_batch_scheduled_calling.py` (raw-SQL house style, typed module attrs); `0011_followup_callback_sms.py` (`idx_followup_flags_status` / `idx_callback_requests_status`)
- `apps/api/tests/`: `conftest.py` (TRUNCATE 100–110, `admin_session` 216–230, `_seed_admin_user_async` 200–213, `counter_value` 38), `test_batch_migration.py` (3-tuple `_columns`, `_check_constraints`, `_indexdef`, subprocess roundtrip 220–251), `test_batch_observability.py` (the `counter_value` assertion precedent), `test_admin_tools_api.py` (seed helpers + `status=closed` at line 81 — rewritten in C3), `test_admin_tools_schemas.py` (the three `_Row` from_attributes stubs — extended in C3), `test_admin_users_api.py` (viewer-session pattern 66–70), `test_admin_sms_messages_api.py`, `test_admin_callback_requests_api.py` (exact key-set at line 63 — extended in C2 + C3), `test_follow_up_flags_repo.py` + `test_callback_requests_repo.py` (no autouse TRUNCATE today — added in C1; id-comprehensions rewritten in C3), `test_recording_url.py` (operator presign tests + `_seed`), `test_schedules_api.py` (loguru-capture pattern 234), `test_call_schedules_repo.py` (session_factory + autouse truncate fixture)
- `apps/admin-ui/`: `src/lib/api.ts` (get/post/put/del only), `src/lib/queryClient.ts` (`refetchOnWindowFocus: false` global), `src/routes.tsx`, `src/components/NavSidebar.tsx` (`GROUPS`), `src/components/ConfirmDialog.tsx`, `src/components/ui/{badge,tabs,toast}.tsx|ts`, `src/auth/useSession.ts` (`useIsAdmin`), `src/features/elders/{EldersPage.tsx,hooks.ts}` (list/paging/mutation pattern), `src/features/audit/{AuditPage.tsx,hooks.ts}` (filter-bar + `fmtDate` duplication), `src/types/api.ts`, `src/test/{ToolsSection.test.tsx,api.test.ts}` (vi.mock api / stubGlobal fetch patterns), `vitest.setup.ts`, `package.json` (build = `tsc --noEmit && vite build`, lint `--max-warnings 0`, `test = vitest run`)
- `infra/terraform/observability.tf:47` (locked-sink substring filter — the load-bearing strings)
- `scripts/tests/` (root-run pytest suite, included in the E1 gate)

---

## Review disposition

Second adversarial review pass (integration + test-strategy) applied to the draft. Repo premises were re-verified before folding (mypy `files = ["src"]` in both packages; `_Row` stubs in `test_admin_tools_schemas.py` lacking every new field; the exact key-set assert at `test_admin_callback_requests_api.py:63`; the id-comprehensions at `test_follow_up_flags_repo.py:54-63` and `test_callback_requests_repo.py:94/117`; no autouse TRUNCATE in either repo test module). 24 findings across the two reviews, 23 unique (both reviews independently caught the C2 sequencing defect). **Applied: 23/23 unique. Rejected: none.**

**Applied — HIGH (3 integration + 1 test-strategy, 3 unique):**
- (int. H1) Every `uv run mypy .` gate would fail today (the `.` argument overrides `files=["src"]` → ~1513 errors in tests/migrations) → all gates are now bare `uv run mypy` (matches CI `lint.yml`), both packages; Executor note 4 documents why.
- (int. H2 ≡ ts HIGH) C2's PATCH-response assertions on `status_updated_*` referenced fields that only landed in C3 → the two stamp fields moved into C2 (with `= None` defaults so the legacy `from_attributes` stubs stay valid); C3 keeps only `elder_name`/`masked_phone`.
- (int. H3) C3 broke four existing test surfaces beyond the draft's "two deliberate breaks" → all enumerated and rewritten in-task: tuple unpacking in `test_create_and_list_follow_up_flag` + the two callback repo filter tests; the callbacks exact key-set (extended in C2 for the stamps and again in C3 for elder identity); the three `_Row` stubs in `test_admin_tools_schemas.py`. Executor note 5 now lists every break. (The alternative — parallel `list_*_with_elder` functions — was not taken: it leaves dead list paths kept alive only by their old tests.)

**Applied — MEDIUM (7/7):**
- (int. M1) `count_by_status` exact-count tests were flaky (no module isolation; earlier tests accumulate open rows) → C1 adds the `test_call_schedules_repo.py`-style autouse TRUNCATE fixture to both repo test modules.
- (ts) §9 requires both idempotent no-ops → `resolved→resolved` legs added alongside `acknowledged→acknowledged`, both queues.
- (ts) Callback transition coverage was one under-specified "same matrix" test → five explicit callback test functions enumerated; the `queue="callback_request"` metric label combination is now asserted.
- (ts) Detail-route 401 was unmapped (router-level dependency = probably-correct = needs a pin) → `test_admin_call_detail_requires_session` added in B6.
- (ts) §9 places `created_to` exclusivity in the HTTP matrix; draft pinned it only at the repo → HTTP-level exclusivity assertion added to B5 (seeded instant excluded, +1 s included).
- (ts) D3/D4 tests asserted Tailwind class strings (restyle-fragile, style-bug-blind) → semantic markers: `data-role` on transcript segments, `data-severity` + badge text on queue rows; Executor note 7 forbids class-string assertions.
- (ts) E1's verify block broke if executed as one script (relative `cd`s compound) → all Part-E lines use absolute paths.

**Applied — LOW (13/13):**
- (int. L1) B6 rollback test's always-raising mock would also fail the recovery GET → raise-once wrapper (first call raises, then delegates to the real `admin_audit.record`).
- (int. L2) C2 snippet read `current` only after a zero-row UPDATE yet audited `"from"` → snippet rewritten: pre-read (404 + audit "from"), guarded UPDATE, re-read on zero rows to disambiguate no-op vs 409 (race-correct; documented one-hop audit-lag caveat).
- (int. L3) Metric-test precedent mis-cited → C2 now points at `tests/test_batch_observability.py` (imports `counter_value` from conftest), explicitly not `test_observability.py`.
- (int. L4) "Eight filter values" vs spec §4.1's seven + `count` → pinned to the spec shape (no `limit`); B5 test asserts the exact eight-key detail set.
- (int. L5) `python3 -m pytest scripts/tests` may lack pytest/pyyaml locally (CI pip-installs them) → dependency noted inline in the E1 command.
- (ts) Stale diff base after the post-#55 rebase → E1 documents `feat/batch-calling...HEAD` pre-rebase, `origin/main...HEAD` after; Step 5 re-runs it at PR time.
- (ts) B1's URL-absence assertion was unfalsifiable (helper never receives the URL) → claim dropped from B1; non-vacuous `"SIGNED-SENTINEL"` negatives added where the mocked signer returns a real URL (B3 success test, B6 sink test).
- (ts) "Thread-offloaded" was vacuously tested via a signer mock → dedicated `test_signing_called_via_thread` monkeypatching `asyncio.to_thread` and asserting it receives `object_storage.generate_signed_url`.
- (ts) E1 checklist overstated rollback coverage → claim narrowed to `calls.get` (the one pinned surface); `calls.list`/PATCH guards noted as same-pattern code-review items (the finding's option B — a per-endpoint rollback test adds no new code path, all four share one guard shape).
- (ts) Callbacks raw-phone negative was implied → `phone not in r.text` stated explicitly in `test_callbacks_list_elder_identity_and_offset`.
- (ts) A1's IntegrityError cleanup would hit `InFailedSqlTransaction` on a shared connection → three separate `engine.begin()` blocks spelled out (seed / failing insert inside `pytest.raises` / cleanup).
- (ts) B5's list PHI-negative was key-shape-only (nothing PHI-bearing seeded) → one listed call now carries a `gs://` recording URI + `"PHI-SENTINEL-LIST"` transcript segment; `r.text` negatives are non-vacuous and `has_recording is True` is exercised at list level.
- (ts) §9 maps origin parsing to the detail endpoint; draft tested it only on the list → `origin["source"] == "schedule"` asserted on the B6 detail body (sched-keyed seed).
