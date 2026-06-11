# Admin-UI Phase A2 — Calls console + Ops queues (design)

**Date:** 2026-06-10
**Status:** Final (review findings applied)
**Predecessors:** Admin-UI Phase 3 (tool catalog + flag/callback/SMS tools, PR #54, **merged** `ef2fa81`); Batch & scheduled calling (A1, PR #55 open, branch `feat/batch-calling` — this spec's branch `feat/calls-ui` is stacked on it, alembic head `0012`)
**Related specs:** `docs/superpowers/specs/2026-06-07-admin-ui-design.md`, `docs/superpowers/specs/2026-06-09-admin-ui-phase3-tools-design.md`, `docs/superpowers/specs/2026-06-10-batch-scheduled-calling-design.md`

---

## 1. Goals & non-goals

### 1.1 Goals

Close the operational gap left open since the Phase-1 admin-UI design deferred the "operations console": **today a nurse cannot see a flagged call without Grafana.** The agent writes `follow_up_flags` and `callback_requests` rows and a Grafana alert pages on urgent flags, but there is no human surface to read the flag, hear the call, or record that someone acted on it. Two deliverables:

1. **Calls console** (`/calls`, `/calls/:id` in `apps/admin-ui`) — a paged, filterable call list (masked phones, no transcript) and a call detail page with a role-styled transcript viewer and recording playback over a short-TTL presigned GCS URL (admin plane clamps TTL to ≤10 min, §4.2).
2. **Ops queues** (`/queues`) — Follow-up flags and Callback requests list views (with elder name + masked phone on every row, and open/urgent counts in the tab headers) with an **open → acknowledged → resolved** status workflow, so triage state lives in the DB with actor attribution instead of in someone's head.

Plus the schema hardening the PR #54 review flagged: `follow_up_flags.status` / `callback_requests.status` are unconstrained `TEXT DEFAULT 'open'` today — migration `0013` adds the CHECK constraint and the `status_updated_at` / `status_updated_by` workflow columns. And one PHI fix in passing: `GET /v1/admin/sms-messages` currently returns the elder's raw E.164 in `to_number`; this phase masks it (§4.6), making "masked phones only on this plane" true rather than aspirational.

**Roles, stated explicitly:** triage *reads* (lists, transcripts, recordings) are session-gated and available to the `viewer` role — this is a deliberate access-policy decision, not an accident of precedent: the nurses doing triage are the intended audience, and every PHI access is per-access audited (§6). Status *mutations* require `AdminRole.ADMIN`, so **nurses who acknowledge/resolve must be provisioned as admins this phase**; a dedicated triage role is open Q10. Resolving a callback implies dialing the elder **out-of-band** (the nurse's existing phone workflow) — the console deliberately never shows a dialable number (open Q9).

Everything rides the existing admin plane: session-cookie auth with per-request DB re-check, viewer/admin roles, `admin_audit` rows, Caddy CIDR gating, the pre-auth rate limiter (which already matches `/v1/admin/*`), and — non-negotiably — the **locked 6-year PHI-audit Cloud Logging sink**, whose filter matches the verbatim substrings `"Transcript accessed"` and `"Recording URL accessed"` (§6).

### 1.2 Non-goals (this phase)

- Live call monitoring / barge-in / mid-call anything.
- Batch & schedule **management UI** — A1 shipped the operator API; its UI is a later increment on the same `Operate` sidebar group.
- Elder CRUD (unchanged from Phase 1: assignment only).
- Transcript search or export; recording download/retention tooling.
- Websockets or polling `refetchInterval`s. Freshness = manual Refresh button + per-query `refetchOnWindowFocus: true` on the queue hooks only (the app-wide default is `refetchOnWindowFocus: false` in `lib/queryClient.ts` and stays that way; §5.4).
- Free-text resolution notes on flags/callbacks (status-only workflow this phase; open Q2).
- In-app dialing / phone reveal for callbacks (open Q9).
- Migrating the operator-plane (`OPERATOR_API_KEY`) endpoints onto the admin session plane (A1 open Q3, still open).

## 2. Architecture

No new deployables, no new env keys, no agent changes. The admin SPA gains three pages; `apps/api` gains one router, two PATCH endpoints, one summary endpoint, one migration, and two small extracted helper modules.

```
nurse/operator (browser, ADMIN_ALLOWED_CIDR)
   │  session cookie (require_admin_session → DB re-check per request)
   ▼
Caddy admin host ── /v1/* ──▶ apps/api  (Cache-Control: no-store on /v1/admin/*)
   │                            ├─ routers/admin_calls.py   (NEW)
   └─ /* ──▶ admin-ui SPA       │    GET /v1/admin/calls
        ├─ /calls               │    GET /v1/admin/calls/{id} ──▶ object_storage.generate_signed_url
        ├─ /calls/:id           │         │                        (keyless V4 IAM signBlob, GCS)
        └─ /queues              │         └─ phi_audit log lines → locked usan-phi-audit sink
                                ├─ routers/admin_tools.py   (EXTENDED)
                                │    PATCH /v1/admin/follow-up-flags/{id}
                                │    PATCH /v1/admin/callback-requests/{id}
                                │    GET   /v1/admin/queues/summary
                                └─ admin_audit rows (same commit) ──▶ Postgres (migration 0013)
```

### 2.1 New / touched files

| File | Role |
|---|---|
| `apps/api/src/usan_api/routers/admin_calls.py` | NEW — admin calls list + detail |
| `apps/api/src/usan_api/repositories/admin_calls.py` | NEW — `list_calls(...)` read model (keeps the already-large `repositories/calls.py` untouched) |
| `apps/api/src/usan_api/schemas/admin_calls.py` | NEW — `AdminCallSummary`, `AdminCallDetail` (reuses `TranscriptSegment`, `CallOrigin`, `parse_origin` from `schemas/call.py`) |
| `apps/api/src/usan_api/phi_audit.py` | NEW — the two locked-sink message constants + `log_transcript_accessed` / `log_recording_url_accessed` (single source of the load-bearing strings; `actor` bound only when not None) |
| `apps/api/src/usan_api/recording_urls.py` | NEW — `presigned_recording_url(call, settings, *, client_host, actor=None, max_ttl_s=None)` extracted from `routers/calls.py::_presigned_recording_url`, calling `phi_audit.log_recording_url_accessed`; operator call sites pass neither `actor` nor `max_ttl_s` → behavior and log lines bit-identical |
| `apps/api/src/usan_api/masking.py` | NEW — `mask_phone(phone) -> str` (`"***" + last4`, `"unknown"` on None) lifted from `routers/admin_elders.py:_mask` |
| `apps/api/src/usan_api/routers/calls.py` | TOUCHED — operator `get_call` switches to the extracted helpers; behavior and log lines bit-identical (existing tests must pass unmodified) |
| `apps/api/src/usan_api/routers/admin_elders.py` | TOUCHED — `_mask` replaced by `masking.mask_phone` (no behavior change) |
| `apps/api/src/usan_api/routers/admin_tools.py` | TOUCHED — two PATCH endpoints; queues summary; list GETs gain `offset`/`severity`/typed `status`; SMS `to_number` masking |
| `apps/api/src/usan_api/repositories/follow_up_flags.py`, `callback_requests.py` | TOUCHED — `get_*`, `update_status`, `count_by_status`, `offset`/`severity` support, Elder outer-join in list reads |
| `apps/api/src/usan_api/schemas/admin_tools.py` | TOUCHED — summaries gain `elder_name`/`masked_phone`/`status_updated_at`/`status_updated_by`; `to_number` masked; new `QueueStatusUpdateRequest`, `QueuesSummary` |
| `apps/api/src/usan_api/db/models.py` | TOUCHED — two columns on `FollowUpFlag` + `CallbackRequest` |
| `apps/api/migrations/versions/0013_ops_queue_status_workflow.py` | NEW — §3 |
| `apps/api/src/usan_api/main.py` | TOUCHED — register `admin_calls` router with the other admin routers (before public ones); `Cache-Control: no-store` middleware for `/v1/admin/*` responses (§8) |
| `apps/admin-ui/src/lib/api.ts` | TOUCHED — add `patch: <T>(u: string, b?: unknown) => request<T>("PATCH", u, b)` (today only get/post/put/del exist) |
| `apps/admin-ui/src/features/calls/` | NEW — `CallsPage.tsx`, `CallDetailPage.tsx`, `TranscriptViewer.tsx`, `RecordingPlayer.tsx`, `hooks.ts` |
| `apps/admin-ui/src/features/queues/` | NEW — `QueuesPage.tsx`, `QueueTable.tsx`, `hooks.ts` |
| `apps/admin-ui/src/lib/format.ts` | NEW — `fmtDate`, `fmtDuration` (three new pages justify ending the per-page `fmtDate` duplication; existing pages not migrated) |
| `apps/admin-ui/src/routes.tsx`, `src/components/NavSidebar.tsx`, `src/types/api.ts` | TOUCHED — §5 |

### 2.2 Deliberate reuse decisions

- **One signing path.** The admin detail endpoint reuses the operator plane's keyless V4 `generate_signed_url` (wrapped in `asyncio.to_thread`, `expected_bucket` fail-closed, settings TTL bounded 60–3600 s) via the extracted `recording_urls.py`, with an **admin-plane TTL clamp** `min(settings.recording_signed_url_ttl_s, 600)` (the settings default is 3600 — the max of the range, not "short"; a signed URL is IP-unbound and defeats the CIDR gate once issued, so the admin plane caps exposure at 10 min; constant in `recording_urls.py`, no new env key). No second GCS code path, and the `"Recording URL accessed"` emission lives in exactly one place.
- **No rate-limit changes.** `_is_operator_route` already matches `path.startswith("/v1/admin/")`; the new endpoints are throttled from day one. The matcher is not widened. Note the shared-IP budget caveat in §8.
- **No new tables.** `0013` is column/constraint/index-only; the conftest TRUNCATE list is unchanged.
- **`origin` stays derived.** The list filter translates `origin` to `idempotency_key` prefix predicates (`sched:` / `batch:` reserved namespace from A1); no provenance column is added.

## 3. Data model

Migration `0013_ops_queue_status_workflow.py` — `revision = "0013"`, `down_revision = "0012"` (stacked on `feat/batch-calling`; **re-verify the head is still `0012` after PR #55 squash-merges and this branch rebases**, §10). Raw-SQL `op.execute` house style, typed module attrs, explicit `downgrade()` with `IF EXISTS`.

```sql
-- 1. Ops-queue status workflow columns (NULL = never transitioned past 'open').
ALTER TABLE follow_up_flags
    ADD COLUMN status_updated_at TIMESTAMPTZ,
    ADD COLUMN status_updated_by TEXT;          -- admin actor email

ALTER TABLE callback_requests
    ADD COLUMN status_updated_at TIMESTAMPTZ,
    ADD COLUMN status_updated_by TEXT;

-- 2. Constrain the status enum (PR #54 review gap: unconstrained TEXT today).
-- Defensive normalize first: the only writer ever was the server_default 'open',
-- but a stray manual edit must not abort the deploy's auto-migration.
UPDATE follow_up_flags SET status = 'open'
    WHERE status NOT IN ('open', 'acknowledged', 'resolved');
ALTER TABLE follow_up_flags
    ADD CONSTRAINT ck_follow_up_flags_status
        CHECK (status IN ('open', 'acknowledged', 'resolved'));

UPDATE callback_requests SET status = 'open'
    WHERE status NOT IN ('open', 'acknowledged', 'resolved');
ALTER TABLE callback_requests
    ADD CONSTRAINT ck_callback_requests_status
        CHECK (status IN ('open', 'acknowledged', 'resolved'));

-- 3. The global "all calls, newest first" admin list has no serving index today
-- (idx_calls_elder covers only the per-elder slice). Composite (created_at, id)
-- because created_at ties are guaranteed (func.now() is the transaction timestamp
-- and the A1 batch materializer inserts many Call rows per poller transaction),
-- and the list orders by the same pair.
CREATE INDEX idx_calls_created ON calls (created_at DESC, id DESC);
```

`downgrade()` (reverse order):

```sql
DROP INDEX IF EXISTS idx_calls_created;
ALTER TABLE callback_requests DROP CONSTRAINT IF EXISTS ck_callback_requests_status;
ALTER TABLE callback_requests DROP COLUMN IF EXISTS status_updated_by;
ALTER TABLE callback_requests DROP COLUMN IF EXISTS status_updated_at;
ALTER TABLE follow_up_flags DROP CONSTRAINT IF EXISTS ck_follow_up_flags_status;
ALTER TABLE follow_up_flags DROP COLUMN IF EXISTS status_updated_by;
ALTER TABLE follow_up_flags DROP COLUMN IF EXISTS status_updated_at;
```

Model changes (`db/models.py`, both `FollowUpFlag` and `CallbackRequest`, house style `DateTime(timezone=True)`):

```python
status_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
status_updated_by: Mapped[str | None] = mapped_column(Text)  # admin actor email
```

**Status state machine** (shared by both tables): allowed transitions are exactly
`open → acknowledged`, `open → resolved`, `acknowledged → resolved`. One-way; no reopen (open Q3). **A same-status request is an idempotent 200 no-op** (`status_updated_*` unchanged, no `*.update` audit row, transition metric not incremented — but it does write a PHI-free `*.noop_read` audit row, §4.3/§6.2) — this makes the PATCH retry-safe after a lost response or a double-click; **409 is reserved for genuine backward transitions** (e.g. `resolved → acknowledged`), and the 409 detail carries the current status so the UI can refetch. Filtered list reads keep using the existing `idx_followup_flags_status` / `idx_callback_requests_status` `(status, created_at DESC)` indexes.

Existing rows: `status_updated_at`/`status_updated_by` stay NULL until the first transition — "open since creation" needs no backfill.

## 4. API surface

All endpoints below sit on the admin plane: router-level `dependencies=[Depends(require_admin_session)]` (401 + `WWW-Authenticate: Cookie` on missing/expired session or de-listed email), Caddy-blocked on the public host, served only on the CIDR-gated admin origin, covered by the existing pre-auth rate limiter. **Reads are session-gated (viewer OK — explicit access policy, §1.1/§6); mutations additionally require `require_admin_role(AdminRole.ADMIN)` → 403 for viewers.** Repos stay flush-only; routers commit; every audit write uses the existing `try/except SQLAlchemyError: rollback; raise` guard in the same commit.

### 4.1 `GET /v1/admin/calls` — paged call list

Router `routers/admin_calls.py`, `APIRouter(prefix="/v1/admin", tags=["admin-calls"], dependencies=[Depends(require_admin_session)])`.

Query params:

| Param | Type | Default / bounds |
|---|---|---|
| `elder_id` | `uuid.UUID \| None` | — |
| `status` | `CallStatus \| None` | enum-validated → 422 on junk |
| `direction` | `CallDirection \| None` | — |
| `origin` | `Literal["schedule","batch","adhoc"] \| None` | — |
| `created_from`, `created_to` | `datetime \| None` | naive → UTC (house precedent); `from > to` → 422; `to` exclusive |
| `limit` | int | `Query(default=50, ge=1, le=500)`, repo-side clamp mirrors |
| `offset` | int | `Query(default=0, ge=0)` |

`origin` filter SQL: `schedule` → `idempotency_key LIKE 'sched:%'`; `batch` → `LIKE 'batch:%'`; `adhoc` → `direction = 'outbound' AND (idempotency_key IS NULL OR (NOT LIKE 'sched:%' AND NOT LIKE 'batch:%'))` — the `direction` guard keeps inbound calls (always NULL-key) out of "Ad hoc"; the existing `direction` filter covers inbound browsing. Two documented caveats: (a) retry children carry no key, so they match `adhoc` in the filter while their response `origin` is `null` — the chain root carries provenance; (b) the `sched:`/`batch:` operator-key rejection only exists since A1, so a hypothetical pre-A1 operator key with those prefixes would match the filter while `parse_origin` returns `None` (probability ~0; same class as the retry-child caveat).

Repo: `repositories/admin_calls.py::list_calls(db, *, elder_id, status, direction, origin, created_from, created_to, limit, offset) -> list[tuple[Call, str | None, str | None]]` — `select(Call, Elder.name, Elder.phone_e164).outerjoin(Elder)`, ordered `Call.created_at.desc(), Call.id.desc()` (served exactly by the new composite `idx_calls_created`; the per-elder slice uses existing `idx_calls_elder`).

Response: bare `list[AdminCallSummary]` (house pattern — no envelope, UI uses the `len == PAGE_SIZE` hasNext heuristic; see Q4):

```python
class AdminCallSummary(BaseModel):
    id: uuid.UUID
    elder_id: uuid.UUID | None
    elder_name: str | None          # names allowed in session-gated responses
    masked_phone: str               # mask_phone(): "***" + last 4, "unknown" if elder gone
    direction: str
    status: str
    origin: CallOrigin | None       # parse_origin(idempotency_key), reused from schemas/call.py
    attempt: int
    started_at: datetime | None
    ended_at: datetime | None
    duration_seconds: int | None
    end_reason: str | None
    has_recording: bool             # recording_uri IS NOT NULL
    created_at: datetime
```

(`masked_phone` — not `phone_masked` — matching the existing `ElderSummary.masked_phone` so the `types/api.ts` mirror keeps one name for one concept.)

**Deliberately absent from the list:** transcript, raw phone, `recording_uri`/presigned URL, `dynamic_vars`, raw `idempotency_key`.

Codes: **200**; 401; 422. Audit (same commit, SQLAlchemyError-guarded): action **`calls.list`**, `entity_type="call"`, `entity_id=None` (a list has no single entity; the elder filter goes in detail), detail = filter shape + count only, e.g. `{"elder_id": "…", "status": "completed", "direction": null, "origin": "batch", "created_from": "…", "created_to": null, "offset": 0, "count": 50}` — never names/phones.

### 4.2 `GET /v1/admin/calls/{call_id}` — detail + transcript + recording URL

Flow (mirrors operator `get_call`, same helper order):

1. `calls_repo.get_call` → 404 `"call not found"`.
2. `client_host = client_ip(request)`; `actor` = session principal email.
3. `presigned_recording_url(call, settings, client_host=client_host, actor=actor, max_ttl_s=600)` — extracted helper; effective TTL = `min(settings.recording_signed_url_ttl_s, 600)` on this plane (operator plane passes no clamp and stays bit-identical); `None` when no `recording_uri`/bucket; signing exception → warn `"Failed to sign recording URL"` + `None` (page still renders, identical to operator plane). On success it emits the locked-sink line (§6) with `actor` bound (the helper binds `actor` only when not None, so operator-plane log records remain bit-identical).
4. `transcripts_repo.list_for_call(db, call_id)` (existing `(started_at, id)` ordering, 1000-segment cap); when non-empty, `phi_audit.log_transcript_accessed(call_id=..., client=..., actor=..., segments=...)`.
5. `admin_audit.record(actor_email=actor, action="calls.get", entity_type="call", entity_id=str(call_id), detail={"segments": n, "has_recording": bool})` + commit (guarded).
6. Elder name/masked phone via one `elders` lookup.

```python
class AdminCallDetail(AdminCallSummary):
    livekit_room: str | None
    parent_call_id: uuid.UUID | None
    scheduled_at: datetime | None
    answered_at: datetime | None
    recording_status: str | None
    presigned_recording_url: str | None
    recording_url_ttl_s: int | None      # the clamped effective TTL when URL present
    transcript: list[TranscriptSegment]  # reused schema: role, content, tool_name?, tool_args?, started_at/ended_at
```

`dynamic_vars`, `error`, raw `idempotency_key`, and `recording_uri` are deliberately omitted — the console needs none of them and each is a gratuitous exposure.

Codes: **200**; 401; 404; 422 (bad UUID). Every detail GET = one audit row + the log lines — per-access granularity is the point (§6). The URL is minted eagerly with the detail (pre-seeded design decision); a lazy "sign on play" sub-resource is open Q7's alternative if real usage shows most detail views never press play.

### 4.3 Queue status transitions (in `routers/admin_tools.py`)

| Method & path | Codes | Auth |
|---|---|---|
| `PATCH /v1/admin/follow-up-flags/{flag_id}` | **200** (incl. idempotent no-op); 401; 403 viewer; 404; 409 backward transition; 422 | `require_admin_role(AdminRole.ADMIN)` + `get_actor_email` |
| `PATCH /v1/admin/callback-requests/{request_id}` | same | same |

Request body (shared):

```python
class QueueStatusUpdateRequest(BaseModel):
    status: Literal["acknowledged", "resolved"]   # "open" is not a settable target → 422
```

Repo (`follow_up_flags.py` shown; `callback_requests.py` mirrors) — ORM form, matching repo style (a raw-SQL `IN :param` would need an expanding bindparam; don't):

```python
_ALLOWED_PREDECESSORS = {"acknowledged": ("open",), "resolved": ("open", "acknowledged")}

async def update_status(db, flag_id, *, new_status, actor_email) -> FollowUpFlag | None:
    # Single status-guarded UPDATE — the WHERE clause IS the state machine; no read-
    # modify-write race. Zero rows → caller disambiguates 404 / no-op / 409 via get_flag().
    stmt = (
        update(FollowUpFlag)
        .where(FollowUpFlag.id == flag_id,
               FollowUpFlag.status.in_(_ALLOWED_PREDECESSORS[new_status]))
        .values(status=new_status, status_updated_at=func.now(),
                status_updated_by=actor_email)
        .returning(FollowUpFlag)
    )
    # flush-only; router commits
```

Router on zero rows updated: `get_flag(db, flag_id)` → missing → 404 `"flag not found"`; `row.status == body.status` → **200 idempotent no-op** (return current summary; `status_updated_*` untouched, no `*.update` audit row, transition metric not incremented — retry/double-click safe. The returned summary is still a PHI read — free-text `reason`/`notes` + `elder_name` — so the no-op leg **does write an audit row in the same request**: action **`follow_up_flag.noop_read`** / **`callback_request.noop_read`**, detail `{"status": <current>, "noop": true}` (status string only, PHI-free), reconciling this leg with §6.2's "every admin PHI exposure is DB-audited"); otherwise → **409** `detail=f"illegal transition: {row.status} -> {body.status}"` (e.g. `"illegal transition: resolved -> acknowledged"`; current status in the detail lets the UI refetch). Success → audit **`follow_up_flag.update`** / **`callback_request.update`** with `entity_type`/`entity_id` and detail `{"from": "open", "to": "acknowledged"}` (status strings only — never `reason`/`notes`), same commit, then return the updated summary. Metric increment after commit (§7).

### 4.4 Queue list extensions (existing GETs)

`GET /v1/admin/follow-up-flags` and `GET /v1/admin/callback-requests` change as follows (additive plus two deliberate, documented behavior changes):

- **`offset: int = Query(default=0, ge=0)`** added to both (repos pass it through; clamps unchanged).
- **`status` filter tightened** from free-text `str (max_length=32)` to `Literal["open","acknowledged","resolved"] | None` → 422 on junk instead of a silent empty 200, symmetric with the new calls endpoints, and no more attacker-typed strings copied into `admin_audit.detail` (behavior change: previously-200 junk now 422s; no client sends junk).
- **Flags only — `severity: Literal["routine","urgent"] | None` filter** added, and the flags list ordering changes to **urgent-first**: `(severity = 'urgent') DESC, created_at DESC, id DESC` — without this an urgent flag older than one page of routine open flags is invisible. Not served by `idx_followup_flags_status` (acceptable at current volumes; revisit with a partial index if flags ever number in the tens of thousands). Callback ordering unchanged.
- **Elder identity on every row:** both repos' list reads gain `outerjoin(Elder)` (same shape as the `admin_calls` read model) and `FollowupFlagSummary` / `CallbackRequestSummary` gain `elder_name: str | None` and `masked_phone: str` — a nurse seeing "urgent / medical / chest pain" must not need an audited transcript read just to learn *who*. Names in session-gated bodies are existing precedent; phones are masked.
- Summaries also gain `status_updated_at: datetime | None` and `status_updated_by: str | None`.
- Existing `elder_id`/`limit` params and the `*.list` audit behavior are untouched; the audit detail gains `"offset"` (and `"severity"` for flags).

### 4.5 `GET /v1/admin/queues/summary` — PHI-free counts

The day-2 question "how many open urgent flags are there?" must be answerable by the people who don't have Grafana — that's the gap this phase closes. Cheap aggregate endpoint (in `routers/admin_tools.py`; repos gain `count_by_status` GROUP-BY helpers served by the existing `(status, created_at DESC)` indexes):

```python
class QueuesSummary(BaseModel):
    flags_open: int
    flags_open_urgent: int
    flags_acknowledged: int
    callbacks_open: int
    callbacks_acknowledged: int
```

Codes: **200**; 401. Viewer-readable. **No `admin_audit` row and no sink line** — deliberately: the response is a PHI-free aggregate (counts only), the endpoint backs tab badges and may be refetched often, and HTTP metrics already account for usage. The sidebar badge built on top of it stays deferred (open Q5). **Consistency caveat (accepted):** the five counts come from three separate statements (two per-table `count_by_status` GROUP-BYs + the open-urgent count) with no shared snapshot, so a transition that commits between them can make the counts momentarily mutually inconsistent — the badges are a triage hint, not an accounting surface, and the next refetch self-corrects.

### 4.6 SMS list masking fix (`GET /v1/admin/sms-messages`)

`SmsMessageSummary.to_number` currently returns the elder's **raw E.164** to any session holder — the one violation of "masked phones only on this plane," called out by review. This phase fixes it: the router maps `to_number` through `masking.mask_phone` before serialization (one-line change + test; nothing in the UI consumes the raw value). The field keeps its name; its content becomes `"***" + last4`. The list's audit behavior is unchanged.

## 5. UI design

### 5.1 Routes & navigation

`routes.tsx` — three children in the existing `PageLayout` group under `RequireAuth > AppLayout` (the call detail page scrolls within `PageLayout` like every list page; the full-height escape hatch used by the profile editor is not needed at current transcript volumes):

```
/calls       → CallsPage
/calls/:id   → CallDetailPage
/queues      → QueuesPage        (?tab=flags|callbacks&status=…&severity=…&offset=…)
```

`NavSidebar.tsx` `GROUPS` gains, between `Config` and `System`:

```ts
{ heading: "Operate", items: [
  { to: "/calls",  label: "Calls" },
  { to: "/queues", label: "Queues" },
]}
```

Neither item is `adminOnly` — viewers triage read-only; mutation affordances are gated in-page via `useIsAdmin()`.

`lib/api.ts` gains the `patch` method (§2.1) — the mutations below cannot be called without it.

### 5.2 `CallsPage` (`features/calls/CallsPage.tsx`)

EldersPage/AuditPage hybrid: `PAGE_SIZE = 50`, `useState(offset)`, filter bar of `Select`s (status — the 11 `CallStatus` values + All; direction; origin: All/Schedule/Batch/Ad hoc) and two date inputs (From / To); filters reset offset to 0. **Date semantics:** `created_to` is exclusive server-side, so the UI sends `selected To date + 1 day` and labels the field "To (inclusive)" — otherwise To=2026-06-10 silently drops June 10's calls. `elder_id` filter is honored from the URL search string (deep-linked from queue rows and call detail, §5.3/§5.4) but has no picker this phase. Hook `useCalls(filters, limit, offset)`, query key `["admin-calls", filters, limit, offset]` (UUIDs/enums/dates only — never names or phones in keys; default `refetchOnWindowFocus: false` applies). `hasNext = list.length === PAGE_SIZE`, Prev/Next + `rangeStart–rangeEnd`.

Table columns: Created (`fmtDate`), Elder (name + `masked_phone`), Direction, Origin badge (`origin.source`; null → "Inbound" when `direction === "inbound"` else "Ad hoc"), Status badge, Attempt, Duration (`fmtDuration(duration_seconds)`), recording indicator (`has_recording`). Row click → `/calls/:id`. States: `Spinner` loading, red `<p>` error, "No calls match these filters" empty state.

### 5.3 `CallDetailPage` + `TranscriptViewer` + `RecordingPlayer`

- `useCall(id)`, key `["admin-call", id]`, **`refetchOnWindowFocus` stays false** (each detail fetch re-signs a URL and writes audit rows; focus-refetching this page would be pure audit noise). Page states: `Spinner` while loading; **404 → distinct "Call not found" state** (stale queue links happen); other errors → red error block with the `ApiError` detail.
- Header card: elder name + masked phone (name links to `/calls?elder_id=…` — "view this elder's calls"), direction, status, origin, attempt, `parent_call_id` link (renders as "attempt N — view parent"), created/started/answered/ended timestamps, duration, end reason.
- **`RecordingPlayer`** (status-aware): when `presigned_recording_url` present — native `<audio controls preload="none" src={presigned_recording_url}>` plus a visible TTL note: *"Recording link expires in ~{Math.round(recording_url_ttl_s/60)} min — reload the page for a fresh link."* When `has_recording` but URL null — muted "Recording exists but no playback link is available right now." (deliberately generic: the server returns null for signing failure **and** for an unconfigured bucket; "try reloading" would be a lie in the second case). When the call status is non-terminal (`in_progress` etc.) — "Call still in progress — recording appears after the call ends." Otherwise "No recording for this call." The URL lives only in component props — never in query keys, localStorage, or `console.*`.
- **`TranscriptViewer`**: simple role-styled message list, no virtualization (segments ≤1000 by server cap). `role === "assistant"` left-aligned neutral card; `role === "user"` right-aligned accent card; tool segments render as a compact monospace chip (`tool_name`) with `tool_args` behind a collapsed `<details>`. Per-segment `started_at` timestamp. Empty state is status-aware: non-terminal call → "Call still in progress — transcript appears after the call ends." (transcripts are bulk-inserted post-call); terminal → "No transcript was captured for this call."

### 5.4 `QueuesPage` (`features/queues/QueuesPage.tsx`)

Tab strip (Follow-up flags / Callbacks) with **tab, status filter, severity filter, and offset all synced to the URL search params** — back-navigation from a call detail restores the nurse's exact position (a `useState`-only filter would lose it). Tab labels carry live counts from `useQueuesSummary()`: "Follow-up flags (N open{, M urgent})" / "Callbacks (N open)".

Hooks `useFollowUpFlags(status, severity, limit, offset)` / `useCallbackRequests(status, limit, offset)` / `useQueuesSummary()`, keys `["admin-flags", …]` / `["admin-callbacks", …]` / `["admin-queues-summary"]` — **all three set per-query `refetchOnWindowFocus: true`** (the global default is `false` in `queryClient.ts`; this page is what a Grafana page sends a nurse into, so it must not show stale data after an alt-tab), plus a manual **Refresh** button (`refetch()`). No `refetchInterval` (non-goal). Status filter `Select` (Open / Acknowledged / Resolved / All), default **Open**; flags tab adds a Severity `Select` (All / Urgent / Routine); `PAGE_SIZE = 50` offset paging.

Per-tab states: `Spinner` loading, red error block, empty copy "No open follow-up flags — all clear." / "No callback requests match." (the filtered variant: "No flags match these filters").

- Flags columns: Created, Elder (name + `masked_phone`, name links to `/calls?elder_id=…`), Severity, Category, Reason, Status (+ `status_updated_by`/`fmtDate(status_updated_at)` when set), "View call" link → `/calls/:id`, actions. **Urgent flags are visually distinct**: red left border + filled `severity` badge; routine = outline badge. The server already orders urgent-first (§4.4).
- Callbacks columns: Created, Elder (same treatment), Requested time (verbatim `requested_time_text`, plus `fmtDate(requested_at)` when resolved), Notes, Status, View call, actions. Resolving a callback = the nurse dials out-of-band (§1.1, Q9).
- **Actions (admin-gated, hidden — not disabled — for viewers via `useIsAdmin()`):** `Acknowledge` (shown when status `open`) and `Resolve` (shown when `open`/`acknowledged`, behind a `ConfirmDialog` — resolution is one-way). Buttons are **disabled while the mutation is pending** (double-click guard; the server is also idempotent on same-status, §4.3). Mutations via the house pattern (TanStack v5 syntax): `useMutation` → `api.patch` → `invalidateQueries({ queryKey: ["admin-flags"] })` / `({ queryKey: ["admin-callbacks"] })` / `({ queryKey: ["admin-queues-summary"] })`; on `ApiError` 409 → `pushToast("Status changed elsewhere — list refreshed")` + invalidate; other errors → `pushToast(err.detail)`.

### 5.5 Types

`types/api.ts` gains `CallOrigin`, `TranscriptSegment`, `AdminCallSummary`, `AdminCallDetail`, `QueueStatus = "open" | "acknowledged" | "resolved"`, `QueuesSummary`, and the extended `FollowupFlagSummary` / `CallbackRequestSummary` (`elder_name`, `masked_phone`, `status_updated_at`, `status_updated_by`); the header comment adds `schemas/admin_calls.py` and `schemas/admin_tools.py` to the mirrored-files list (nullability and value sets must match the server exactly, per the file's contract). Field naming follows the server: `masked_phone` everywhere.

## 6. PHI & audit discipline

Non-negotiables, enforced by tests (§9):

1. **Locked-sink strings are load-bearing.** The 6-year locked Cloud Logging sink (`infra/terraform/observability.tf`, bucket `usan-phi-audit`, `locked = true`) substring-matches `"Transcript accessed"` and `"Recording URL accessed"`. Both strings move into `phi_audit.py` as constants used by **both** planes; the admin detail endpoint emits them with the same shape as the operator plane plus an `actor` bind:
   - `logger.bind(call_id=…, client=…, actor=…, segments=…).info("Transcript accessed")` — only when the transcript is non-empty (operator-plane parity);
   - `logger.bind(call_id=…, client=…, actor=…, has_recording=True).info("Recording URL accessed")` — only when a URL was actually signed.
   The helpers bind `actor` **only when not None**, so operator-plane log records remain bit-identical after the extraction. Extra bound fields are safe (the filter is a `:` contains match); renaming the message breaks the immutable audit trail and is forbidden. Content is never logged — ids, client host, actor email, counts only.
2. **DB audit rows with actor email** accompany every admin PHI exposure and every mutation, in the same commit, rollback-guarded: `calls.list`, `calls.get`, the existing `follow_up_flags.list` / `callback_requests.list` / `sms_messages.list`, `follow_up_flag.update`, `callback_request.update` — plus `follow_up_flag.noop_read` / `callback_request.noop_read` on the idempotent no-op PATCH legs, whose 200 body returns the full summary and is therefore a PHI read like any other (§4.3). Audit `detail` carries filter shape, counts, and status strings only — never `reason`, `notes`, names, or phone text. One row per detail view is deliberate per-access granularity; the volume (a triage console, not a firehose) is acceptable and `list_recent` filters by action. The PHI-free `queues/summary` endpoint deliberately writes no audit row (§4.5). **Scope note for compliance readers:** this both-sink-line-and-DB-row guarantee is admin-plane-only; operator-plane (`OPERATOR_API_KEY`) reads have no actor identity and remain sink-line-only, unchanged by this phase.
3. **Masked phones only** in every list and detail response (`masking.mask_phone`) — including the SMS list as of this phase (§4.6); the raw `phone_e164` never leaves the server on this plane. Elder names are permitted in session-gated response bodies (existing precedent) but never in audit details, log binds, URLs, or query keys.
4. **Viewer access is policy, not accident:** the `viewer` role can read transcripts and play recordings — the nurses doing triage are the audience, "minimum necessary" is enforced by who gets an `admin_users` row at all (CIDR-gated, individually provisioned), and detection is per-access auditing (sink + DB). Mutations stay admin-only (pre-seeded decision); a finer-grained triage role is open Q10.
5. **Client side:** no PHI in TanStack query keys (UUIDs/enums/dates only), no `console.*` of response bodies, presigned URLs never persisted, transcripts only ever fetched on the detail page, and the call-detail query never focus-refetches (no surprise re-signing). The 401 hard-redirect in `lib/api.ts` already prevents PHI render without a session. Server adds `Cache-Control: no-store` on all `/v1/admin/*` responses (§8) so transcripts and bearer URLs are never written to a shared workstation's HTTP cache.
6. The transcript content cap (1000 segments) and the recording TTL ceiling (admin plane: ≤600 s) are inherited/derived server-side bounds, not UI promises.

## 7. Observability

- **New counter** in `observability/custom_metrics.py`: `usan_admin_queue_transitions_total{queue ∈ {follow_up_flag, callback_request}, to_status ∈ {acknowledged, resolved}}` — bounded, PHI-free labels; incremented **after** commit (house discipline: a crash can't double-count); idempotent no-ops do not increment. Day-2 question "are urgent flags actually being acknowledged?" becomes a Grafana query against this counter vs `usan_followup_flags_total{severity="urgent"}` — and the in-app `queues/summary` counts answer it for non-Grafana users.
- **No new metrics for reads** — admin read traffic is visible via the existing HTTP metrics, and PHI-access accounting is the locked sink's job, not Prometheus'.
- The existing **urgent-flag alert remains the paging path**; this UI is the triage surface it pages people into. No alert changes.
- Log lines bind ids/actor only (`logger.bind(flag_id=…, actor=…, from_status=…, to_status=…)`); transitions at INFO, backward-transition 409s at WARNING (they indicate two humans racing), signing failures keep the existing WARN.

## 8. Security

- **Defense in depth unchanged, surface reused:** Caddy CIDR gate (admin host only; `/v1/admin/*` 403-blocked on the public host) → pre-auth rate limiter (`/v1/admin/` prefix already matched — verified, no matcher change) → session cookie JWT (`SameSite=Strict`, HttpOnly) → per-request `admin_users` DB re-check (instant revocation) → role gate on mutations (`AdminRole.ADMIN`; viewers 403) → audit.
- **Shared-IP rate budget:** the limiter keys on the XFF first hop, so all nurses behind the office CIDR share one `rate_limit_default` (60/min) window; three new chatty pages make legitimate 429s plausible during busy triage — watch for them in rollout step 4 before touching limits.
- **`Cache-Control: no-store` middleware** on every `/v1/admin/*` response (neither the API nor Caddy sets cache headers today): transcript JSON and live bearer recording URLs must never land in a shared nurse workstation's HTTP cache. **Accepted exception:** unhandled-exception 500s are emitted by Starlette's outermost `ServerErrorMiddleware`, above this middleware, so they lack the header — their body is the PHI-free constant `"Internal Server Error"`, so nothing cacheable leaks.
- **Recording URLs:** keyless V4 signing with mandatory `expected_bucket` confinement and `_parse_gs_uri` traversal rejection (inherited); a signed URL is IP-unbound (it defeats the CIDR gate once issued), so the admin plane clamps TTL to `min(settings.recording_signed_url_ttl_s, 600)`; URLs are bearer secrets — returned once per detail GET, never logged (only `has_recording=True`), never stored.
- **Transition endpoints are not mass-assignment surfaces:** the request body is a single `Literal` field; `status_updated_by` is always the session actor, never client-supplied; the state machine lives in the SQL `WHERE` clause, so a race produces a 409 (or an idempotent no-op), not a lost update.
- **Input validation at the boundary:** enum/`Literal`-typed query params (422 on junk — including the newly-tightened queue `status` and `severity` filters), UUID path params, bounded `limit`/`offset` with repo-side clamps mirroring `Query` bounds, date-range sanity check.
- **No new secrets, no new env keys, no settings changes.** No CSRF change: the API is same-origin behind Caddy with the `SameSite=Strict` session cookie as shipped in Phase 1; mutations remain JSON `PATCH` (no form posts).
- Error messages leak nothing beyond status words: `"call not found"`, `"flag not found"`, `"illegal transition: resolved -> acknowledged"`.

## 9. Testing strategy (TDD — tests first per task)

**Migration contract** — `tests/test_ops_queue_migration.py` (named per house pattern: `test_batch_migration.py`, `test_phase3_migration.py`): after `alembic upgrade head`, assert both tables have `status_updated_at TIMESTAMPTZ NULL` + `status_updated_by TEXT NULL`, both CHECK constraints exist and reject `status='bogus'` (IntegrityError), and `idx_calls_created` exists on `(created_at DESC, id DESC)`; downgrade drops all of it cleanly and re-upgrade succeeds. **The normalize-on-upgrade assertion must use the subprocess roundtrip pattern** (`test_batch_migration.py`'s `downgrade 0012 → seed out-of-enum row → upgrade head → assert 'open'`) — conftest migrates to head before tests run, so a head-only test of the normalize is vacuous.

**API — `tests/test_admin_calls_api.py`** (patterns: `test_admin_tools_api.py` — cookie-jar `admin_session` fixture, `_create_elder`/`_enqueue` seed helpers, `mock_dispatch`):
- Auth matrix: list + detail 401 without session; both readable as viewer.
- List: filter matrix (status / direction / origin×3 — incl. `adhoc` excluding inbound NULL-key calls and the retry-child caveat / elder_id / date range, `created_to` exclusive), ordering `created_at DESC, id DESC`, limit/offset paging, 422s (bad enum, `from > to`).
- **PHI-free list assertions:** response JSON contains `masked_phone == "***" + last4`, and the serialized body never contains the seeded full phone or any `transcript`/`presigned`/`recording_uri` key; elder-deleted row → `masked_phone == "unknown"`.
- Detail: 404; transcript segments in `(started_at, id)` order; `origin` parsed for `sched:`/`batch:` keys and `None` otherwise; presigned URL path with `object_storage.generate_signed_url` mocked (called via thread, `expected_bucket` passed, **TTL argument == min(settings TTL, 600)**) + `recording_url_ttl_s` populated with the clamped value; signing exception → 200 with `presigned_recording_url=None`.
- **Locked-sink log capture:** loguru capture asserts a detail GET with transcript+recording emits messages exactly equal to `phi_audit` constants `"Transcript accessed"` and `"Recording URL accessed"` with `actor`/`client`/`call_id` bound and no transcript content in the record; empty transcript → no transcript line. Plus a constants test pinning both strings verbatim (drift = broken immutable trail). Operator-plane `get_call` tests keep passing **unmodified** after the helper extraction (behavioral refactor guard), including the no-`actor`-key-in-extra property of operator-plane records.
- Audit: `calls.list` (entity_id None, elder filter in detail) / `calls.get` rows written with actor email, PHI-free `detail` (filter shape/counts only); audit commit failure path rolls back.
- `Cache-Control: no-store` present on `/v1/admin/*` responses, absent on public routes.

**API — transition matrix** (extends `test_admin_tools_api.py`): for both queues — `open→acknowledged` 200, `acknowledged→resolved` 200, `open→resolved` 200; **idempotent no-ops:** `acknowledged→acknowledged` and `resolved→resolved` → 200 with unchanged `status_updated_*`, **no new `*.update` audit row**, and a **PHI-free `*.noop_read` audit row** (detail `{"status", "noop"}` only — never `reason`/`notes`; §6.2); backward `resolved→acknowledged` → 409 with current status in detail; missing id → 404; viewer → 403 (and no DB change); no session → 401; body `status:"open"` → 422; success stamps `status_updated_at`/`status_updated_by=<actor>` and writes the `follow_up_flag.update`/`callback_request.update` audit row with `{"from","to"}` only; two sequential conflicting updates → second is no-op-or-409 per guarded-UPDATE semantics; metric increments after commit and not on no-ops (precedent: `test_observability.py`).

**API — queue list/summary extensions:** list endpoints honor `offset`; flags honor `severity` and order urgent-first; `status=bogus` → 422 (was 200-empty); summaries expose `elder_name`/`masked_phone`/`status_updated_*` and never the raw phone; `queues/summary` counts match seeded rows, is viewer-readable, writes **no** audit row; **SMS masking:** `sms-messages` list returns `to_number == "***" + last4` and never the seeded raw E.164.

**UI (vitest, jsdom, `src/test/`, `ToolsSection.test.tsx` style with `vi.stubGlobal("fetch", …)`):** CallsPage — rows render masked phone + origin badge ("Inbound" for inbound NULL-origin), filter change resets offset, To-date sends +1 day, Prev/Next heuristic, loading/error/empty states; CallDetailPage — loading/404/error states, elder-calls link; TranscriptViewer — role styling split, tool chip with collapsed args, status-aware empty states; RecordingPlayer — `<audio>` src + TTL note, no-link and in-progress and no-recording states; QueuesPage — tab/status/offset URL sync, summary counts in tab labels, urgent visual distinction, viewer hides Acknowledge/Resolve while admin sees them, pending-disabled buttons, Resolve confirm flow, 409 → toast + invalidate (v5 `invalidateQueries({ queryKey })`); `api.patch` exists and is used; types compile (`tsc --noEmit` in `build`).

**Gates:** coverage ≥80% on new modules; `ruff check`, `ruff format`, **mypy** (CI runs it on apps/api even though CLAUDE.md omits it), `npm run build` (tsc), `eslint --max-warnings 0`, `npm run test` — all green before push.

## 10. Rollout

1. **Branch mechanics (stacked PR):** `feat/calls-ui` is stacked on `feat/batch-calling` (PR #55, open). After #55 squash-merges, rebase via the established flow (`git rebase --onto origin/main <prev-plan-tip>`), then **re-verify `alembic heads` is exactly `0012` and `0013`'s `down_revision` still matches** before opening the A2 PR — a renumber in #55's final squash must be caught here, not on the VM.
2. **Ship live, no feature flag.** Everything is additive: a migration old code ignores (old code only ever writes the default `'open'`, which the CHECK permits), new admin endpoints, new SPA pages. There is no autonomous behavior to gate — the A1 inert-deploy machinery is not needed.
3. **Deploy mechanics:** standard `v*` tag (app + compose). **No new env keys → no `/opt/usan/infra/.env` refresh and no Terraform apply needed.** The entrypoint auto-runs `alembic upgrade head` (0013 is additive and fast — column adds + one composite index; `CREATE INDEX` on `calls` is acceptable non-concurrent at current volumes).
4. **Post-deploy validation sequence:** (a) load `/calls` as a viewer — list renders, masked phones only, no 429s during normal clicking (shared-CIDR budget, §8); (b) open one call detail with a recording — audio plays, TTL note shows ≤10 min, then confirm in Cloud Logging that the locked `usan-phi-audit` sink captured both `"Transcript accessed"` and `"Recording URL accessed"` entries with the actor bound, and `admin_audit` has the `calls.get` row; (c) as admin, acknowledge → resolve a test flag, confirm re-resolve is a 200 no-op, a backward PATCH 409s, the audit rows exist, and `usan_admin_queue_transitions_total` moved; (d) confirm viewer sees no action buttons and PATCH as viewer returns 403; (e) confirm `/queues` tab counts match the DB and the SMS list shows masked numbers.
5. **Rollback:** roll the `v*` tag back — old code runs unchanged against the 0013 schema (columns unread, CHECK satisfied by the only value old code writes). `alembic downgrade 0012` only if the schema itself must go; never auto-triggered.
6. **Manual-validation debt carried forward:** Phase 2/3 live-call validation (including SMS) is still pending and is unblocked by this UI — the calls console is itself the easiest way to perform it; fold both into step 4's live test call.

## 11. Open questions

1. **Push notification path** — the queues are pull-based; the Grafana urgent-flag alert remains the only push. A "notify nurse by SMS/email on urgent flag" channel is a product decision (and a Telnyx Messaging consumer once PR #54's flag flips).
2. **Resolution notes** — a free-text "what was done" field on resolve (PHI-bearing, audit-relevant). Deferred to keep this phase status-only; would ride the same PATCH.
3. **Reopen transition** — the machine is deliberately one-way; if mis-resolves become real, an admin-only `resolved → open` with its own audit action is the fix, not direct DB edits.
4. **Server-side totals / pagination envelope / keyset** — the `len == PAGE_SIZE` heuristic is repo precedent but lies on exact-multiple boundaries, and the calls list is the one append-heavy newest-first list in the console: inserts between page clicks shift rows across offset pages (duplicates on Next). An `X-Total-Count` header or envelope, and keyset paging on `(created_at, id)` (the new index already serves it), are cross-cutting changes for all list pages at once, not piecemeal here.
5. **Sidebar queue badge** — `queues/summary` (§4.5) now provides the data; the badge is wiring plus a polling-cadence decision. Nice-to-have once usage patterns are real.
6. **Batch/schedule management UI** (A1 day-2 affordances: PATCH batch, per-target cancel, re-run failed, schedule run-now) — next increment in the `Operate` group, reusing this phase's table/detail patterns.
7. **Recording URL refresh / lazy minting** — TTL expiry currently requires a page reload (new audit event per re-sign, which is correct). Two future options: a "refresh link" button, or moving the URL to a lazy `GET /v1/admin/calls/{id}/recording-url` sub-resource signed only on play (own audit row + sink line; also stops minting bearer URLs for detail views that never press play). Decide after observing real playback behavior.
8. **Audit volume from focus refetch** — the queue hooks opt into `refetchOnWindowFocus: true` (§5.4), so every alt-tab back to `/queues` writes `*.list` audit rows. Acceptable now (the summary endpoint is audit-free, and the global default stays off elsewhere); if it gets noisy, the fix is differentiating read-audit granularity, which is a compliance conversation, not a code default.
9. **Callback execution path** — resolving a callback assumes the nurse dials the elder out-of-band (existing phone workflow); the console never shows a dialable number and outbound enqueue is operator-plane. If that assumption fails operationally, the options are an admin-gated, per-access-audited "reveal phone" affordance or a "trigger callback call" button onto the operator enqueue path — both product decisions with PHI/consent implications.
10. **Triage role** — mutations require `AdminRole.ADMIN` (pre-seeded decision), so triaging nurses are provisioned as admins this phase, which also grants elder assignment and admin-user management. If that's too broad, a third role (`triage`: read + queue transitions only) is the fix; revisit once real nurses are onboarded.
11. **Related queue items on call detail** — the detail page doesn't yet show the call's own flags/callbacks (both tables carry `call_id`); a `call_id` filter on the existing queue GETs plus a card with inline Acknowledge/Resolve would close the loop queue→call→resolve without back-navigation (URL-synced queue state covers the return trip this phase).
