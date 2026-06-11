# Signed Outbound Event Webhooks (Phase A3) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Push call-lifecycle events (`call.started`, `call.completed`, `flag.created`, `callback.created`, `batch.completed`) to operator-registered HTTPS endpoints via a transactional outbox + a fourth lifespan poller, with HMAC-SHA256 signed POSTs, a 4-attempt retry ladder, a per-endpoint circuit breaker, two-layer SSRF defense, and PHI-free-by-construction payloads — plus the prerequisite hardening of every enqueue-bearing call mutator into an atomic guarded transition (including the `mark_answered` zombie fix).

**Architecture:** Everything in `apps/api` (zero `services/agent` changes). New: migration `0014` (two tables + partial claim index), `ssrf_guard.py`, `webhook_signing.py`, `webhook_events.py` (the ONLY payload constructors), `repositories/webhook_outbox.py`, `repositories/webhook_endpoints.py`, `schemas/webhook_endpoints.py`, `routers/webhook_endpoints.py`, `webhook_delivery.py` (4th poller, ALWAYS started; `WEBHOOK_DELIVERY_ENABLED` gates delivery only). Touched: `repositories/calls.py` (guarded transitions + enqueue), `repositories/follow_up_flags.py`, `repositories/callback_requests.py`, `schedule_orchestrator.py` (phase-6 `batch.completed`), `main.py`, `settings.py`, `ratelimit.py`, `db/models.py`, `observability/custom_metrics.py`, `infra/*`, `usan_alerts.yml`, `scripts/tests/test_alerting_provisioning.py`, `tests/conftest.py`, `tests/test_lifespan_poller.py`. Ships inert: `WEBHOOK_DELIVERY_ENABLED=false` default; zero registered endpoints ⇒ zero outbox rows.

**Tech Stack:** Python 3.14 (FastAPI, Pydantic v2, SQLAlchemy 2 async, Alembic raw-SQL, httpx + MockTransport, prometheus_client, loguru lazy `{}` ids-only); testcontainers Postgres; Grafana alerts-as-code.

**Source spec:** `docs/superpowers/specs/2026-06-10-outbound-webhooks-design.md` (Final, three-lens review applied).
**Review status:** adversarial integration + test-strategy reviews applied — see "Review disposition" at the end.

**Executor notes (read before starting):**
1. **Two dependency-driven placements, deliberate (batch-plan precedent):** (a) the **4 Settings fields land in Task B1** — Part D's `/test` 409 gate and Part E's poller read them; Part F owns only infra mirroring + env contract tests. (b) **All metric objects and `.inc()`/`.set()` wiring land in Part F (Task F1)** — Parts C–E deliver behavior with DB-state + log assertions only. Do not define any new metric before F1.
2. **Same-file sequencing (strict, never parallelize tasks sharing a file):** `repositories/calls.py` C3 → C4; `repositories/webhook_outbox.py` C2 → D3 → E1; `db/models.py` A2 → D4 (AdminAuditLog docstring); `main.py` D4 → E3; `webhook_delivery.py` E2 → F1; `tests/test_lifespan_poller.py` E3 only; `tests/test_app_security.py` D1 only; `tests/conftest.py` A1 only.
3. House rules: repos take the request session, `flush()+refresh()`, **never commit** (the poller modules own their sessions/commits like `sms_outbox`/`schedule_orchestrator`); routers commit. Raw `op.execute` migrations. Loguru lazy `{}`; bind ids only — **never** URLs, secrets, elder ids in delivery logs, or `str(exc)` (`type(exc).__name__` only). ruff line-length 100 (no `N` rules selected — see note 5); `uv run mypy .` before every push (strict mode, `files=["src"]`).
4. **Deliberate deviations from spec prose, decided here (record in commit messages):**
   - `mark_dial_failure` guards on **DIALING only**, narrower than §2.1's "(queued/dialing)" wording: §10.7's test row ("stale `mark_dial_failure` after `reclaim_stuck_dialing` re-queue is a no-op") is unsatisfiable if QUEUED is allowed, and every caller (all in `livekit_dispatch.dispatch_and_dial` + the post-`set_status(DIALING)` dial path) operates on DIALING rows. The test row is the contract.
   - `cancel_queued_tips` keeps its `-> int` signature (both callers consume a count); internally it switches to `.returning(Call)` and enqueues one event per returned row.
   - The **201 create response is a superset** of spec §4's pinned shape: `WebhookEndpointCreatedResponse(WebhookEndpointResponse)` additionally carries `consecutive_failures`, `disabled_reason`, `pending_deliveries`, `updated_at`. All additive fields are PHI-free operator state; a single response-model hierarchy beats a second trimmed model, and additive response fields are backward-compatible.
   - `deliver_one` catches **`OSError` (⊃ `socket.gaierror`)** alongside `httpx.HTTPError`/`ValueError`/`SsrfBlocked` so DNS resolution failures (NXDOMAIN — the most common dead-receiver mode) record `last_error='gaierror'` via the type-name rule instead of escaping the except tuple and aborting the endpoint group.
5. The delivery-time SSRF exception class is named **`SsrfBlocked`** (no `Error` suffix) so `type(exc).__name__` equals the spec-pinned `last_error='SsrfBlocked'`. ruff selects `E,F,I,B,UP,ASYNC,S,PT,RET,SIM` — no pep8-naming, so this is clean.
6. Routers read settings via `Depends(get_settings)`; API tests that need `WEBHOOK_DELIVERY_ENABLED=true` do `monkeypatch.setenv("WEBHOOK_DELIVERY_ENABLED", "true"); get_settings.cache_clear()` **in the test body before the request** (the lru_cache is re-read per request). Restoration mechanism: the `client` fixture's own `finally` block runs `get_settings.cache_clear()` on teardown (conftest.py:152) — there is **no** autouse cache-clear fixture in conftest; the only autouse settings fixture (`_clear_settings`) lives in `tests/test_lifespan_poller.py`. Do not go hunting for one.
7. **Every verify command starts from the repo root** (`/Users/evgenii.vasilenko/gofrolist/usan-voice-engine`). Subagent cwd resets between bash calls — do not strip the `cd apps/api && ` prefixes.
8. **Stacked-branch ritual (spec §11.7):** this branch is third in the stack (`feat/batch-calling` PR #55 → `feat/calls-ui` PR #56 → here); alembic head is `0013`. After **each** predecessor squash-merges, run `git rebase --onto origin/main <prev-plan-tip>`, re-run the A1 migration roundtrip (a renumber on main breaks `down_revision="0013"`), and re-verify the C3/C4 mutator shapes against the rebased tree.
9. **The webhook poller is ALWAYS started by lifespan (E3), so every `client`-fixture test runs a live background poller.** This is safe — and load-bearing — for two reasons: (a) flag-off ⇒ the delivery half never claims rows; (b) the poller holds the **lifespan-time `Settings` snapshot**, so D4's in-test `setenv("WEBHOOK_DELIVERY_ENABLED","true")` flips per-request settings but never the running poller — which is exactly what keeps `test_test_ping_enqueues_real_pipeline_row` deterministic (the enqueued ping row stays `pending`; nothing races to deliver it). The poller's first cycle runs real housekeeping SQL against the shared test DB; harmless because sweep/expire/prune only touch rows older than their thresholds (tests create fresh rows). State this comment in E3.

---

## Part A — Migration 0014 + models + conftest TRUNCATE + contract tests

### Task A1: Migration `0014_outbound_webhooks.py` + conftest TRUNCATE + migration contract test

**Files:**
- Create: `apps/api/migrations/versions/0014_outbound_webhooks.py`
- Modify: `apps/api/tests/conftest.py` (TRUNCATE list, lines 104–110)
- Test: `apps/api/tests/test_webhook_migration.py`

- [ ] Step 1: Write the failing test — clone `test_ops_queue_migration.py`'s helpers verbatim (`_columns` already returns `{name: (data_type, is_nullable, column_default)}` there, plus `_indexes`, `_check_constraints`, `_indexdef`, `_execute`, `_fetch_one`):
  - `test_webhook_endpoints_table_shape` — columns: `id`=uuid, `url`=text NOT NULL, `description`=text nullable, `enabled`=boolean default contains `true`, `secret`=text NOT NULL, `events`=`ARRAY`, `consecutive_failures`=integer default contains `0`, `disabled_reason`=text nullable, `created_at`/`updated_at`=timestamptz NOT NULL; check constraints `ck_webhook_endpoints_events`, `ck_webhook_endpoints_disabled_reason`, `ck_webhook_endpoints_failures` present.
  - `test_webhook_deliveries_table_shape` — `endpoint_id`=uuid NOT NULL, `event`=text NOT NULL, `payload`=jsonb NOT NULL, `status`=text default contains `'pending'`, `attempts`=integer default contains `0`, `next_attempt_at`=timestamptz NOT NULL default contains `now()`, `response_code`=integer nullable, `last_error`=text nullable, `delivered_at` nullable timestamptz; checks `ck_webhook_deliveries_status`, `ck_webhook_deliveries_event`, `ck_webhook_deliveries_attempts`.
  - `test_due_index_is_partial_on_pending` — `idx_webhook_deliveries_due` indexdef contains `(next_attempt_at)`, `WHERE`, and `pending` (TEXT column — do not assert an exact `WHERE` literal, the 0012 rendering lesson).
  - `test_endpoint_list_index_shape` — `idx_webhook_deliveries_endpoint` indexdef contains `endpoint_id`, `created_at DESC`, `id DESC`.
  - `test_check_constraints_enforced` — raw-SQL inserts via `_execute` (ops-queue pattern): seed one valid endpoint, then assert `IntegrityError`/`DBAPIError` raised for: endpoint with `events = '{}'` (empty array), endpoint with `events = '{call.started,bogus}'`, **endpoint with `events = '{ping}'`** (ping is NOT subscribable — the load-bearing asymmetry), endpoint with `disabled_reason = 'manual'`, delivery with `status = 'sent'`, delivery with `event = 'bogus'`, delivery with `attempts = -1`; and that a delivery with `event = 'ping'` **inserts fine**.
  - `test_fk_delete_rule_cascade` — via `referential_constraints`: `webhook_deliveries→webhook_endpoints` = CASCADE.
  - `test_downgrade_seed_upgrade_roundtrip` — `subprocess.run([sys.executable, "-m", "alembic", "downgrade", "0013"], ...)` (conftest env dict), assert both tables gone; **then seed one minimal business row** (raw `_execute` INSERT into `calls` with only NOT NULL columns, nullable FKs left NULL — the spec §10.1 "downgrade → seed → upgrade" sequence: upgrading over a *populated* pre-0014 database must work); then `upgrade head` and assert both tables + `idx_webhook_deliveries_due` back **and the seeded `calls` row survived**. **Always finishes at head** (commit 6b9 discipline); runs last in the module.

- [ ] Step 2: Run test, verify it FAILS

```bash
cd apps/api && uv run pytest tests/test_webhook_migration.py -v
```
RED reason: alembic head is `0013`; `_columns` returns `{}` for both tables → `KeyError: 'id'`.

- [ ] Step 3: Implement `0014_outbound_webhooks.py` — SQL **verbatim from spec §3.3** (revision `"0014"`, `down_revision="0013"`, the two `CREATE TABLE` blocks with all three CHECKs each, both `CREATE INDEX` statements, numbered comment sections 1–4 carried over verbatim — including the secret-handling, ping-asymmetry, and last-error-type-name-only comments). `downgrade()` exactly as specced (`DROP INDEX IF EXISTS` ×2 then `DROP TABLE IF EXISTS` deliveries before endpoints).

  Edit `tests/conftest.py` `_truncate_and_dispose` — prepend FK-children-first:

```python
"TRUNCATE webhook_deliveries, webhook_endpoints, "
"call_batch_targets, call_batches, call_schedules, "
```

- [ ] Step 4: Run test, verify PASS

```bash
cd apps/api && uv run pytest tests/test_webhook_migration.py -v && ruff check migrations tests && ruff format --check migrations && uv run mypy .
```

- [ ] Step 5: Commit

```bash
git add apps/api/migrations/versions/0014_outbound_webhooks.py apps/api/tests/test_webhook_migration.py apps/api/tests/conftest.py && git commit -m "feat(api): migration 0014 — webhook_endpoints + webhook_deliveries transactional outbox"
```

---

### Task A2: SQLAlchemy models `WebhookEndpoint`, `WebhookDelivery`

**Files:**
- Modify: `apps/api/src/usan_api/db/models.py` (append after `CallBatchTarget`)
- Test: `apps/api/tests/test_webhook_models.py`

- [ ] Step 1: Write the failing test (pure `__table__` introspection, `test_batch_models.py` pattern):
  - `test_webhook_endpoint_columns_and_defaults` — `__tablename__ == "webhook_endpoints"`; column set ⊇ `{id, url, description, enabled, secret, events, consecutive_failures, disabled_reason, created_at, updated_at}`; `secret` not nullable; `events` not nullable and `isinstance(cols["events"].type, ARRAY)`; `enabled` server_default arg contains `"true"`; `consecutive_failures` server_default contains `"0"`; `updated_at.onupdate is not None`.
  - `test_webhook_delivery_columns_and_fk` — `endpoint_id` FK ondelete == `"CASCADE"` and not nullable; `status` server_default contains `'pending'`; `attempts` server_default contains `"0"`; `payload` JSONB not nullable; `next_attempt_at` not nullable with a server_default; `response_code`/`last_error`/`delivered_at` nullable; `updated_at.onupdate is not None`.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_webhook_models.py -v` — RED: `ImportError: cannot import name 'WebhookEndpoint'`.

- [ ] Step 3: Implement — two models mirroring `SmsMessage` (models.py:394–421): UUID pk `server_default=text("gen_random_uuid()")`, `Text` + `server_default` statuses, `JSONB` payload, house created/updated pattern. `events: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)` (`from sqlalchemy.dialects.postgresql import ARRAY` — extend the existing dialects import). Carry the spec's model comments: secret returned once/never logged; `disabled_reason` NULL for operator-disables vs `'circuit_breaker'` (distinguishable states); `last_error` = exception **type name only**.

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_webhook_models.py tests/test_webhook_migration.py -v && ruff check src/usan_api/db/models.py && uv run mypy .`
- [ ] Step 5: `git add apps/api/src/usan_api/db/models.py apps/api/tests/test_webhook_models.py && git commit -m "feat(api): WebhookEndpoint/WebhookDelivery ORM models (migration 0014 mirror)"`

---

### Task A3: Part A gate

- [ ] `cd apps/api && uv run pytest -v && ruff check . && ruff format --check . && uv run mypy .` — full suite green (proves the conftest TRUNCATE change broke nothing). Commit any format rewrites as `chore(api): format Part A` (with explicit `git add` of the rewritten files).

---

## Part B — Settings + SSRF guard + signing

### Task B1: Settings — the 4 `WEBHOOK_DELIVERY_*` fields

**Files:**
- Modify: `apps/api/src/usan_api/settings.py` (append after the scheduler block)
- Test: `apps/api/tests/test_settings_webhooks.py`

- [ ] Step 1: Write the failing test (clone `test_settings_scheduler.py`'s `_BASE` dict):
  - `test_webhook_delivery_defaults_are_inert` — `webhook_delivery_enabled is False`, `webhook_delivery_poll_interval_s == 10`, `webhook_delivery_timeout_s == 10`, `webhook_delivery_circuit_breaker_threshold == 10`.
  - `test_webhook_delivery_bounds_enforced` — parametrized `ValidationError`: `WEBHOOK_DELIVERY_POLL_INTERVAL_S` ∈ {"4","301"}, `WEBHOOK_DELIVERY_TIMEOUT_S` ∈ {"0","61"}, `WEBHOOK_DELIVERY_CIRCUIT_BREAKER_THRESHOLD` ∈ {"0","101"}.
  - `test_flag_on_alone_is_valid` — `Settings(**_BASE, WEBHOOK_DELIVERY_ENABLED="true")` constructs (per-row secrets ⇒ **no cross-field validator**, spec §5.1 — pin it so nobody adds one).
  - `test_namespace_disjoint_from_inbound_webhook_max_age` — same Settings instance has both `webhook_max_age_s == 300` (inbound LiveKit) and the new outbound fields; setting `WEBHOOK_DELIVERY_ENABLED` does not perturb `webhook_max_age_s`.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_settings_webhooks.py -v` — RED: `AttributeError: webhook_delivery_enabled`.

- [ ] Step 3: Implement — four fields per spec §5.1 table with a comment block noting the deliberate `WEBHOOK_DELIVERY_` prefix (disjoint from the **inbound** `WEBHOOK_MAX_AGE_S`) and that no startup cross-field validator is needed. No new optional strings ⇒ `_blank_to_none` untouched.

```python
webhook_delivery_enabled: bool = Field(default=False, alias="WEBHOOK_DELIVERY_ENABLED")
webhook_delivery_poll_interval_s: int = Field(default=10, ge=5, le=300, alias="WEBHOOK_DELIVERY_POLL_INTERVAL_S")
webhook_delivery_timeout_s: int = Field(default=10, ge=1, le=60, alias="WEBHOOK_DELIVERY_TIMEOUT_S")
webhook_delivery_circuit_breaker_threshold: int = Field(default=10, ge=1, le=100, alias="WEBHOOK_DELIVERY_CIRCUIT_BREAKER_THRESHOLD")
```

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_settings_webhooks.py tests/test_settings.py -v && ruff check . && uv run mypy .`
- [ ] Step 5: `git add apps/api/src/usan_api/settings.py apps/api/tests/test_settings_webhooks.py && git commit -m "feat(api): WEBHOOK_DELIVERY_* settings (flag default false, bounded interval/timeout/threshold)"`

---

### Task B2: `ssrf_guard.py` — registration validator + delivery-time resolver gate

**Files:**
- Create: `apps/api/src/usan_api/ssrf_guard.py`
- Test: `apps/api/tests/test_ssrf_guard.py`

- [ ] Step 1: Write the failing test — **two table-driven matrices** (spec §8.1/§8.2/§10.4):
  - `test_validate_webhook_url_rejects` — `@pytest.mark.parametrize("url", [...])`, each `pytest.raises(ValueError)`: `"http://hooks.example.com/x"`, `"https://192.168.1.1/"`, `"https://[::1]/"`, **`"https://93.184.216.34/"` and `"https://[2606:2800:220:1:248:1893:25c8:1946]/"` (PUBLIC literals — §8.1 says IP literals are rejected outright, not gated on `is_global`; an `is_private`-based implementation must fail here)**, **`"https://[fe80::1]/"`, `"https://[fd00::1]/"`, `"https://[::ffff:169.254.169.254]/"` (bracketed IPv6 decoys)**, `"https://2130706433/"`, `"https://0x7f000001/"`, `"https://0x7f.0.0.1/"`, `"https://0177.0.0.1/"`, `"https://localhost/x"`, `"https://foo.localhost/"`, `"https://printer.local/"`, `"https://foo.internal/"`, `"https://metadata.google.internal/"`, `"https://metadata.google.internal./computeMetadata"` (trailing dot), `"https://METADATA.GOOGLE.INTERNAL/"` (case), `"https://host.home.arpa/"`, `"https://intranet/"` (single-label), `"https://user:pass@hooks.example.com/"` (userinfo), `"https://hooks.example.com/x#frag"` (fragment), `"https://hooks.example.com:8080/"` (port), `"https://hooks.example.com/" + "a" * 2050` (oversize).
  - `test_validate_webhook_url_accepts` — parametrized, returns unchanged: `"HTTPS://Hooks.Example.com/path"` (scheme case-fold), `"https://hooks.example.com:8443/path?x=y"`, `"https://hooks.example.com:443/"`, `"https://hooks.example.com/hook?token=abc"`, **`"https://hooks.example.com./"` (trailing dot on an allowed host normalizes via `rstrip(".")` and is ACCEPTED — pinned so the behavior is a decision, not an accident)**.
  - `test_resolve_public_or_raise_rejects` — parametrized over monkeypatched `ssrf_guard._resolve` returning: `["169.254.169.254"]`, `["10.0.0.5"]`, `["::1"]`, `["fd00::1"]`, `["fe80::1"]`, `["100.64.0.1"]`, `["::ffff:169.254.169.254"]`, `["::ffff:10.0.0.1"]` (IPv4-mapped unwrap), `["93.184.216.34", "10.0.0.5"]` (mixed — **every** address must be global), `[]` (**empty — fail-closed**); each raises `SsrfBlocked`, and `type(exc).__name__ == "SsrfBlocked"` (the `last_error` contract).
  - `test_resolve_public_or_raise_accepts` — `["93.184.216.34"]` and `["2606:2800:220:1:248:1893:25c8:1946"]` pass.
  - `test_resolve_propagates_gaierror` — monkeypatched `_resolve` raises `socket.gaierror` → `resolve_public_or_raise` lets it **propagate unwrapped** (not swallowed, not converted to `SsrfBlocked` — the delivery worker owns the handling and the type-name rule yields `last_error='gaierror'`; collapsing into `SsrfBlocked` would destroy the DNS-vs-policy diagnostic distinction).
  - `test_resolve_real_localhost_blocked` — **no monkeypatch**: `await resolve_public_or_raise("localhost")` raises `SsrfBlocked` (exercises the real `getaddrinfo` extraction `info[4][0]`, which every other delivery-time test bypasses; loopback resolution is deterministic on any host).

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_ssrf_guard.py -v` — RED: `ModuleNotFoundError: usan_api.ssrf_guard`.

- [ ] Step 3: Implement (module docstring cites spec §8.1/§8.2 + the TOCTOU residual in full breadth + open Q2):

```python
class SsrfBlocked(Exception):
    """Delivery-time SSRF rejection. NAME IS LOAD-BEARING: type(exc).__name__
    is stored as webhook_deliveries.last_error='SsrfBlocked' (spec §5.3/§8.2)."""

_DENY_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
_DENY_EXACT = frozenset({"localhost"})
_ALLOWED_PORTS = frozenset({None, 443, 8443})  # open Q10 keeps 8443

def validate_webhook_url(url: str) -> str: ...
    # urlsplit; len<=2048; scheme.lower()=="https"; no userinfo/fragment;
    # host = hostname.lower().rstrip("."); port in _ALLOWED_PORTS;
    # reject if ipaddress.ip_address(host) parses (ANY IPv4/IPv6 literal — public
    # included; bracketed IPv6 arrives unbracketed via urlsplit().hostname);
    # reject decoy literals: re.fullmatch over dot-joined labels each matching
    # 0[xX][0-9a-fA-F]+ | 0[0-7]+ | [0-9]+ (inet_aton hex/octal/decimal forms);
    # reject _DENY_EXACT, _DENY_SUFFIXES, and single-label hosts ("." not in host).
    # Raises ValueError with a stable message naming the failed rule; returns url.

async def _resolve(host: str) -> list[str]:  # seam tests monkeypatch
    # socket.gaierror/OSError from getaddrinfo PROPAGATES — handled by the
    # delivery worker's except tuple (executor note 4), recorded by type name.
    infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    return [info[4][0] for info in infos]

async def resolve_public_or_raise(host: str) -> None:
    addrs = await _resolve(host)
    if not addrs:                      # fail-closed: all(...) over [] is vacuously true
        raise SsrfBlocked(...)
    for addr in addrs:
        ip = ipaddress.ip_address(addr)
        ip = getattr(ip, "ipv4_mapped", None) or ip   # ::ffff:a.b.c.d judged as IPv4
        if not ip.is_global:
            raise SsrfBlocked(...)
```

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_ssrf_guard.py -v && ruff check src/usan_api/ssrf_guard.py && uv run mypy .`
- [ ] Step 5: `git add apps/api/src/usan_api/ssrf_guard.py apps/api/tests/test_ssrf_guard.py && git commit -m "feat(api): ssrf_guard — registration URL validator + fail-closed delivery-time resolver gate"`

---

### Task B3: `webhook_signing.py` — canonical bytes, HMAC, secret generation

**Files:**
- Create: `apps/api/src/usan_api/webhook_signing.py`
- Test: `apps/api/tests/test_webhook_signing.py`

- [ ] Step 1: Write the failing test:
  - `test_sign_pinned_vector` — table-driven over the pinned vector: `secret = "a" * 64`, `ts_ms = 1765432100000`, `body = {"event": "ping", "occurred_at": "2026-06-10T00:00:00Z", "data": {"endpoint_id": "00000000-0000-0000-0000-000000000002"}, "delivery_id": "00000000-0000-0000-0000-000000000001"}`; assert `canonical_bytes(body) == b'{"data":{"endpoint_id":"00000000-0000-0000-0000-000000000002"},"delivery_id":"00000000-0000-0000-0000-000000000001","event":"ping","occurred_at":"2026-06-10T00:00:00Z"}'` and `sign(secret, ts_ms, raw) == "966b62c7ee18db2debabfeebccb8f943e5d78fd07115d32da75680a50254bd36"`.
  - `test_documented_verify_snippet_round_trips` — embed spec §7's `verify_usan_signature` verbatim in the test; `header = signature_header(ts_ms, sign(...))` with `ts_ms = int(time.time()*1000)` → `True`; tampered body (one byte) → `False`; `ts_ms` 301 s in the past → `False`; 299 s → `True`.
  - `test_canonical_bytes_invariant_under_key_reorder` — two dicts with identical content, different insertion order → identical bytes.
  - `test_canonical_bytes_survives_jsonb_round_trip` — uses `client`'s engine: insert a `WebhookDelivery` payload dict, read it back, `canonical_bytes(read_back | {"delivery_id": str(row.id)})` equals the pre-insert canonical form (signed bytes == sent bytes after JSONB storage, spec §10.6).
  - `test_generate_secret_is_64_hex_and_unique` — `len == 64`, `int(s, 16)` parses, two calls differ.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_webhook_signing.py -v` — RED: module missing.

- [ ] Step 3: Implement:

```python
def generate_secret() -> str:                       # secrets.token_hex(32)
def canonical_bytes(body: dict[str, Any]) -> bytes:
    """json.dumps(body, sort_keys=True, separators=(",", ":")).encode() —
    sign-what-you-send: JSONB does not preserve byte form (spec §7)."""
def sign(secret: str, ts_ms: int, raw_body: bytes) -> str:
    """hex(HMAC_SHA256(secret, f"{ts_ms}." + raw_body))."""
def signature_header(ts_ms: int, digest: str) -> str:   # f"v={ts_ms},d={digest}"
```

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_webhook_signing.py -v && ruff check . && uv run mypy .`
- [ ] Step 5: `git add apps/api/src/usan_api/webhook_signing.py apps/api/tests/test_webhook_signing.py && git commit -m "feat(api): webhook_signing — canonical JSON + HMAC-SHA256 vectors + server-side secret generation"`

---

## Part C — Payload builders, outbox enqueue, mutator hardening, trigger sites

### Task C1: `webhook_events.py` — typed payload builders + origin root-walk

**Files:**
- Create: `apps/api/src/usan_api/webhook_events.py`
- Test: `apps/api/tests/test_webhook_events.py`

- [ ] Step 1: Write the failing test (DB tests use a local `session_factory` over `async_database_url`, the `test_call_batches_repo.py` fixture pattern; seed via repos):
  - `test_payload_field_allowlists_exact` — table-driven over all six builders: `set(payload["data"].keys())` **equals exactly** spec §6.2–§6.7's field sets (`call.started`: `{call_id, elder_id, direction, attempt, parent_call_id, origin, answered_at}`; `call.completed`: + `{status, created_at, ended_at, duration_seconds}` − `{answered_at kept}`; `flag.created`: `{flag_id, call_id, severity, created_at}`; `callback.created`: `{callback_id, call_id, elder_id, requested_at, created_at}`; `batch.completed`: `{batch_id, status, target_count, final_status_histogram, completed_at}`; `ping`: `{endpoint_id}`), and envelope keys are exactly `{event, occurred_at, data}` (no `delivery_id` in the **stored** payload — injected at send time).
  - `test_phi_exclusions_pinned` — the serialized JSON of each payload contains none of: the elder's `name`, `phone_e164`, flag `reason`/`category`/`elder_id` (flag specifically), callback `requested_time_text`/`notes`, `end_reason`, `dynamic_vars` content, `livekit_room`, `recording_uri`, **`sip_call_id`, `egress_id`, the `error` JSONB content**, batch `name`, raw `idempotency_key` (seed each with sentinel strings like `"PHIPHI"` and assert absence — the three added fields are on spec §6.1's excluded-everywhere list).
  - `test_origin_root_walk_retry_child` — root with `idempotency_key="batch:<uuid>:3"`, child attempt 2 with no key (created via `calls_repo.schedule_retry` after a `NO_ANSWER` parent) → `call_completed_payload(db, child)["data"]["origin"] == {"source": "batch", "id": "<uuid>", "ordinal": 3}` (§10.9).
  - `test_origin_null_for_operator_and_inbound` — plain-key one-off → `origin is None`; `create_inbound_call` row → `origin is None`.
  - `test_call_completed_nulls_for_dnc_at_birth` — DNC_BLOCKED-at-birth row → `answered_at`/`ended_at`/`duration_seconds` all `None`, `status == "dnc_blocked"`.
  - `test_batch_completed_counts` — batch with 3 targets, 2 finalized → `target_count == 3`, histogram matches `final_status_histogram` repo aggregate, `status`/`completed_at` echoed from the batch row.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_webhook_events.py -v` — RED: module missing.

- [ ] Step 3: Implement — module docstring: "**the only place payloads are constructed** — allowlist by construction (spec §6.1); a field not on the model cannot leak". One frozen Pydantic model per event's `data`; builders return `dict[str, Any]` via `model_dump(mode="json")` wrapped in the envelope:

```python
WEBHOOK_EVENTS = ("call.started", "call.completed", "flag.created", "callback.created", "batch.completed")  # closed enum, single source for schema validation

def _envelope(event: str, data: BaseModel) -> dict[str, Any]:
    # {"event": event, "occurred_at": _utcnow().isoformat(...), "data": data.model_dump(mode="json")}

async def chain_root_origin(db: AsyncSession, call: Call) -> CallOrigin | None:
    # walk parent_call_id to the root (<= _MAX_CHAIN_HOPS, the schedule_retry bound),
    # then parse_origin(root.idempotency_key) — origin describes the CHAIN's origin
    # on every attempt (spec §6.1); None for operator one-off and inbound.

async def call_started_payload(db, call: Call) -> dict[str, Any]
async def call_completed_payload(db, call: Call) -> dict[str, Any]
def flag_created_payload(flag: FollowUpFlag) -> dict[str, Any]      # NO elder_id, NO category (§6.4)
def callback_created_payload(row: CallbackRequest) -> dict[str, Any]
async def batch_completed_payload(db, batch: CallBatch) -> dict[str, Any]
    # target_count via SELECT count(*) on call_batch_targets + final_status_histogram
    # (the call_batches.py:151 aggregate) — target_count is not a CallBatch column (§6.6)
def ping_payload(endpoint_id: uuid.UUID) -> dict[str, Any]
```

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_webhook_events.py -v && ruff check . && uv run mypy .`
- [ ] Step 5: `git add apps/api/src/usan_api/webhook_events.py apps/api/tests/test_webhook_events.py && git commit -m "feat(api): webhook_events — PHI-free allowlist payload builders + chain-root origin walk"`

---

### Task C2: `repositories/webhook_outbox.py` — fan-out enqueue (same-txn)

**Files:**
- Create: `apps/api/src/usan_api/repositories/webhook_outbox.py` (**first of three sequential edits**: C2 → D3 → E1)
- Test: `apps/api/tests/test_webhook_outbox.py`

- [ ] Step 1: Write the failing test (local `session_factory` fixture; seed endpoints by adding `WebhookEndpoint` model rows directly — the operator repo doesn't exist yet):
  - `test_enqueue_fans_out_to_enabled_subscribed_only` — endpoints: (enabled, subscribed `call.completed`), (enabled, subscribed `flag.created` only), (disabled, subscribed `call.completed`) → `enqueue_event(db, event="call.completed", payload=p)` returns 1; exactly one `webhook_deliveries` row, `status='pending'`, `attempts=0`, payload `== p`, correct `endpoint_id`.
  - `test_enqueue_zero_endpoints_zero_rows` — no endpoints → returns 0, zero rows (the ship-inert zero-cost path, §2.1).
  - `test_enqueue_same_txn_visibility` — in an **uncommitted** session, enqueue; a second engine's session sees zero rows; after `commit()` it sees one (outbox joins the caller's transaction).
  - `test_enqueue_rollback_leaves_nothing` — enqueue then `rollback()` → zero rows from a fresh session (crash-between ⇒ **neither** business change nor event, §10.7).
  - `test_enqueue_ping_ignores_subscriptions` — endpoint subscribed only to `flag.created` → `enqueue_ping(db, endpoint_id=e.id)` inserts one `event='ping'` row with `next_attempt_at <= now()` (§10.8).

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_webhook_outbox.py -v` — RED: module missing.

- [ ] Step 3: Implement (flush-only, never commit; module docstring carries the §2.1 transactional-outbox rationale, citing the sms_outbox header precedent):

```python
async def enqueue_event(db: AsyncSession, *, event: str, payload: dict[str, Any]) -> int:
    # SELECT id FROM webhook_endpoints WHERE enabled AND events @> ARRAY[:event]
    # (WebhookEndpoint.events.contains([event])); one WebhookDelivery per endpoint,
    # identical payload; flush; return count. Zero endpoints -> zero rows, no flush.
async def enqueue_ping(db: AsyncSession, *, endpoint_id: uuid.UUID) -> WebhookDelivery:
    # one 'ping' row regardless of subscriptions (the /test pipeline, §4).
```

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_webhook_outbox.py -v && ruff check . && uv run mypy .`
- [ ] Step 5: `git add apps/api/src/usan_api/repositories/webhook_outbox.py apps/api/tests/test_webhook_outbox.py && git commit -m "feat(api): webhook_outbox enqueue — same-transaction fan-out to enabled+subscribed endpoints"`

---

### Task C3: Guarded-transition hardening of the enqueue-bearing call mutators (the §2.1 prerequisite — regression tests FIRST, no webhooks yet)

**Files:**
- Modify: `apps/api/src/usan_api/repositories/calls.py` (**first of two sequential edits**: C3 → C4)
- Test: `apps/api/tests/test_calls_guarded_transitions.py`

- [ ] Step 1: Write the failing tests — **genuine two-concurrent-sessions races** (spec §10.7 — the existing A1 race tests are sequential and prove nothing). Shared helper in the test module: engine A session runs mutator A (flush, transaction held open), `asyncio.create_task` runs mutator B on engine B (must block on the row lock), `await asyncio.sleep(0.2)`, commit A, await B:
  - `test_outcome_voicemail_vs_room_finished_single_winner` — IN_PROGRESS call; A=`mark_voicemail_left_if_in_progress`, B=`mark_completed_if_in_progress(room)` → A returns the call, **B returns `None`**, final status `voicemail_left` (today both Python checks pass → both non-None → RED).
  - `test_end_call_vs_room_finished_single_winner` — A=`complete_call_if_in_progress`, B=`mark_completed_if_in_progress` → exactly one non-None.
  - `test_mark_failed_if_active_vs_dial_failure_single_winner` — DIALING call; A=`mark_dial_failure(NO_ANSWER)`, B=`mark_failed_if_active` → exactly one non-None, one terminal status.
  - `test_mark_answered_after_terminal_is_noop_no_zombie` — sequential: COMPLETED call → `mark_answered` returns `None`, status **stays COMPLETED**, `answered_at` unchanged (today it resurrects to IN_PROGRESS → RED; the pre-existing zombie bug, §2.1).
  - `test_mark_answered_from_dialing_still_transitions` — DIALING → IN_PROGRESS + `answered_at` set (pin the happy path; RINGING tolerated too — second param case).
  - `test_stale_dial_failure_after_reclaim_requeue_is_noop` — QUEUED row (as `reclaim_stuck_dialing` leaves it) → `mark_dial_failure` returns `None`, status stays QUEUED (executor note 4: DIALING-only guard — RED today).
  - `test_dial_failure_on_terminal_is_noop` — COMPLETED row → `mark_dial_failure` returns `None`, row untouched.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_calls_guarded_transitions.py -v` — RED per the parenthetical reasons above.

- [ ] Step 3: Implement in `repositories/calls.py` (minimal-diff option per spec §2.1):
  - `mark_completed_if_in_progress` / `mark_voicemail_left_if_in_progress` / `complete_call_if_in_progress` / `mark_failed_if_active` — replace the row load with a `with_for_update()` load (`db.get(Call, call_id, with_for_update=True)`; `_latest_by_room` gains a `for_update: bool = False` parameter adding `.with_for_update()`), so the in-Python status check holds under the row lock; the second racer re-reads the terminal status and returns `None`.
  - `mark_dial_failure` — `with_for_update` load; transition only when `call.status is CallStatus.DIALING` (executor note 4 — comment cites §10.7's reclaim-race row and the verified caller inventory); else return `None`.
  - `mark_answered` — `with_for_update` load; the **whole write** gated on `call.status in (CallStatus.DIALING, CallStatus.RINGING)` (RINGING = dead-state tolerance, never assigned today); else `None`.
  - `set_status` — `with_for_update` load; capture `old_status` before assignment (used by C4; write behavior unchanged).
  - Module-level `_TERMINAL_STATUSES = frozenset({COMPLETED, VOICEMAIL_LEFT, NO_ANSWER, BUSY, FAILED, DNC_BLOCKED, CANCELLED})` (C4 uses it).

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_calls_guarded_transitions.py tests/test_calls_lifecycle.py tests/test_dispatch_and_dial.py tests/test_retry_scheduling.py tests/test_webhooks.py tests/test_tools.py -v && ruff check . && uv run mypy .` (callers verified tolerant of the new `None`s — full suite at the Part C gate, Task C7).
- [ ] Step 5: `git add apps/api/src/usan_api/repositories/calls.py apps/api/tests/test_calls_guarded_transitions.py && git commit -m "fix(api): atomic guarded transitions on all terminal call mutators + mark_answered zombie fix (webhook enqueue prerequisite)"`

---

### Task C4: `call.completed` / `call.started` enqueue inside the guarded mutators

**Files:**
- Modify: `apps/api/src/usan_api/repositories/calls.py` (second sequential edit)
- Test: `apps/api/tests/test_webhook_enqueue_calls.py`

- [ ] Step 1: Write the failing test (fixture: one enabled endpoint subscribed to both `call.started` and `call.completed`, seeded via the model; helper `_deliveries(db, event)` returns pending rows):
  - `test_each_terminal_mutator_enqueues_one_completed` — parametrized over `mark_completed_if_in_progress`, `mark_voicemail_left_if_in_progress`, `complete_call_if_in_progress`, `mark_failed_if_active`, `mark_dial_failure(NO_ANSWER)`: after mutator + commit → exactly one `call.completed` row; payload `data.status` matches; `data.call_id` matches.
  - `test_noop_mutators_enqueue_nothing` — parametrized, **subscribed endpoint seeded** (the §10.7 "zero enqueues" half the guard tests alone cannot prove): stale `mark_dial_failure` on a QUEUED row → `None` + **zero** delivery rows; `mark_dial_failure` on a COMPLETED row → zero rows; `mark_answered` on a COMPLETED row → zero `call.started` rows.
  - `test_stale_dial_task_caller_path_no_child_no_event` — the composite caller path: drive `livekit_dispatch._dial_and_classify` (the `test_livekit_dispatch._seed` + mock pattern) against a re-queued QUEUED row → the internal `mark_dial_failure` no-ops, the trailing **unconditional** `schedule_retry` produces **no spurious child** (parent status non-retryable), and zero delivery rows exist.
  - `test_set_status_enqueues_only_nonterminal_to_terminal` — `set_status(QUEUED→DIALING)` → zero rows; `set_status(DIALING→FAILED)` → one row; seed COMPLETED then `set_status(→FAILED)` → **zero** new rows (terminal→terminal, §10.7).
  - `test_dnc_at_birth_enqueues` — `create_call(status=DNC_BLOCKED)` and `create_materialized_root(status=DNC_BLOCKED)` each → one `call.completed{status=dnc_blocked}`.
  - `test_cancel_queued_tips_one_event_per_returned_row` — two QUEUED tips + one IN_PROGRESS tip → returns 2, exactly two `call.completed{status=cancelled}` rows with the two cancelled ids.
  - `test_mark_answered_enqueues_started_once` — DIALING → one `call.started`; second `mark_answered` (now IN_PROGRESS, no-op) → still one.
  - `test_create_inbound_call_enqueues_started` — inbound row → one `call.started` with `direction=inbound`, `origin=null`, `elder_id` null tolerated.
  - `test_race_emits_exactly_one_completed` — re-run the C3 voicemail-vs-room_finished interleaving with the endpoint subscribed → exactly **one** `call.completed` row exists after both commits (the spec's headline double-emit fix).
  - `test_rollback_discards_transition_and_event_together` — mutator + enqueue flushed, then `rollback()` → call status unchanged **and** zero delivery rows (atomicity: crash-between ⇒ neither).
  - `test_zero_endpoints_zero_rows_zero_errors` — no endpoints → mutators behave exactly as before, zero rows.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_webhook_enqueue_calls.py -v` — RED: zero delivery rows everywhere.

- [ ] Step 3: Implement — private helpers in `calls.py`, called **inside the guarded transition path, after the flush**:

```python
async def _enqueue_call_completed(db: AsyncSession, call: Call) -> None:
    await webhook_outbox.enqueue_event(db, event="call.completed",
        payload=await webhook_events.call_completed_payload(db, call))
async def _enqueue_call_started(db: AsyncSession, call: Call) -> None: ...
```
  Sites (spec §2.1 table): the five terminal mutators; `set_status` (enqueue iff `old_status not in _TERMINAL_STATUSES and status in _TERMINAL_STATUSES`); `create_call`/`create_materialized_root` when `status is CallStatus.DNC_BLOCKED`; `cancel_queued_tips` — switch the UPDATE to `.returning(Call)` with `execution_options(populate_existing=True)`, enqueue per returned row, return `len(rows)` (signature unchanged, executor note 4); `mark_answered` + `create_inbound_call` → `call.started`. Import note: `calls.py → webhook_events → call_batches` is acyclic (verified — `call_batches` does not import `calls`).

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_webhook_enqueue_calls.py tests/test_calls_guarded_transitions.py tests/test_batches_api.py tests/test_schedule_orchestrator.py -v && ruff check . && uv run mypy .` (the last two cover both `cancel_queued_tips` callers — its internals changed in this task).
- [ ] Step 5: `git add apps/api/src/usan_api/repositories/calls.py apps/api/tests/test_webhook_enqueue_calls.py && git commit -m "feat(api): transactional call.started/call.completed outbox enqueue inside every guarded mutator"`

---

### Task C5: `flag.created` / `callback.created` enqueue in the creators

**Files:**
- Modify: `apps/api/src/usan_api/repositories/follow_up_flags.py`, `apps/api/src/usan_api/repositories/callback_requests.py`
- Test: `apps/api/tests/test_webhook_enqueue_tools.py`

- [ ] Step 1: Write the failing test:
  - `test_create_flag_enqueues_in_same_txn` — endpoint subscribed `flag.created`; `create_follow_up_flag` + commit → one row; payload data keys exactly `{flag_id, call_id, severity, created_at}` — **no `elder_id`, no `category`, no `reason`** even though the flag row carries all three (§6.4 re-pinned at the integration level).
  - `test_create_callback_enqueues_in_same_txn` — payload keys exactly `{callback_id, call_id, elder_id, requested_at, created_at}`; `requested_time_text`/`notes` sentinel strings absent from the serialized payload.
  - `test_flag_rollback_discards_both` — create + rollback → no flag row, no delivery row.
  - `test_tool_endpoint_commit_covers_enqueue` — full-stack via `client` + service JWT (`conftest.service_token`): `POST /v1/tools/flag_for_followup` with a subscribed endpoint seeded → 200 and the delivery row is committed and visible from a fresh session (proves the router's existing `db.commit()` covers the enqueue with **zero call-site edits**).

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_webhook_enqueue_tools.py -v` — RED: zero rows.

- [ ] Step 3: Implement — in each creator, after `flush()`/`refresh()`: `await webhook_outbox.enqueue_event(db, event="flag.created", payload=webhook_events.flag_created_payload(row))` (resp. `callback.created`). Same flush-only discipline; comment cites §6.4's deliberate field reduction on the flag path.

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_webhook_enqueue_tools.py tests/test_follow_up_flags_repo.py tests/test_callback_requests_repo.py tests/test_tools.py -v && ruff check . && uv run mypy .`
- [ ] Step 5: `git add apps/api/src/usan_api/repositories/follow_up_flags.py apps/api/src/usan_api/repositories/callback_requests.py apps/api/tests/test_webhook_enqueue_tools.py && git commit -m "feat(api): flag.created/callback.created outbox enqueue inside the creators (flag payload PHI-reduced per spec §6.4)"`

---

### Task C6: `batch.completed` from phase-6 drain settlement (the only emission point)

**Files:**
- Modify: `apps/api/src/usan_api/schedule_orchestrator.py` (`_complete_drained_batches`, line 573)
- Test: `apps/api/tests/test_webhook_batch_events.py`

- [ ] Step 1: Write the failing test (seed batches/targets via `call_batches` repo, the `test_schedule_orchestrator.py` fixture pattern; endpoint subscribed `batch.completed`):
  - `test_drained_running_batch_emits_completed` — running batch, all targets settled → run `_complete_drained_batches(factory, now=...)` → exactly one `batch.completed` row, `data.status == "completed"`, `data.completed_at` non-null, `target_count` == seeded target count, histogram matches.
  - `test_drained_cancelled_batch_emits_cancelled_status` — cancelled batch with zero open targets → one event with `data.status == "cancelled"` (**both** statuses emit, §6.6).
  - `test_phase6_rerun_emits_nothing_new` — run phase 6 twice → still exactly one event per batch (the `completed_at` stamp removes it from the open set — exactly-once).
  - `test_batch_event_same_txn_pre_commit_invisible` — **same-txn atomicity, not mere co-occurrence**: monkeypatch `webhook_events.batch_completed_payload` with a wrapper that (i) calls the real builder, then (ii) opens a **fresh-engine** session and records that it sees zero `batch.completed` delivery rows and the batch's `completed_at` still NULL (the stamp + enqueue are uncommitted at probe time); after `_complete_drained_batches` returns, assert the probe saw nothing and a fresh session now sees both the stamp and exactly one delivery row.
  - `test_batch_event_failure_rolls_back_stamp_and_event` — monkeypatch `webhook_events.batch_completed_payload` to raise → the exception propagates out of `_complete_drained_batches`; a fresh session sees `completed_at IS NULL` **and** zero delivery rows (stamp and event commit or roll back together — an implementation that commits the stamp in a separate txn fails here).
  - `test_cancel_endpoint_emits_no_batch_event` — via `client` + `operator_headers`: `POST /v1/batches/{id}/cancel` on a running batch with one in-flight target → **zero** `batch.completed` rows immediately after (the event arrives only at drain settlement); re-cancel → still zero (idempotent endpoint cannot double-emit, §2.1/§10.7).

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_webhook_batch_events.py -v` — RED: zero rows.

- [ ] Step 3: Implement — inside `_complete_drained_batches`'s session, **between the repo call and the commit** (spec §2.1 table): for each `batch` in `drained`, `await webhook_outbox.enqueue_event(db, event="batch.completed", payload=await webhook_events.batch_completed_payload(db, batch))`. Comment: single emission point; the cancel endpoint enqueues nothing (its `cancel_queued_tips` already emits per-call events through the C4 mutator).

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_webhook_batch_events.py tests/test_schedule_orchestrator.py tests/test_batches_api.py -v && ruff check . && uv run mypy .`
- [ ] Step 5: `git add apps/api/src/usan_api/schedule_orchestrator.py apps/api/tests/test_webhook_batch_events.py && git commit -m "feat(api): batch.completed enqueued at phase-6 drain settlement, both statuses, single emission point"`

---

### Task C7: Part C gate

- [ ] `cd apps/api && uv run pytest -v && ruff check . && ruff format --check . && uv run mypy .` — **full suite green before Part D.** Part C rewrote `repositories/calls.py` internals (guards + `cancel_queued_tips` `.returning(Call)`) and touched three more repos + the orchestrator; the per-task Step 4 lists are targeted, so this gate is where any missed caller regression surfaces — do not defer it to G1, ten tasks away. Commit any stragglers as `chore(api): Part C gate fixes` (with explicit `git add`).

---

## Part D — Operator API

### Task D1: Rate-limit allowlist for `/v1/webhook-endpoints` + `/v1/webhook-deliveries`

**Files:**
- Modify: `apps/api/src/usan_api/ratelimit.py` (`_is_operator_route`)
- Test: `apps/api/tests/test_app_security.py` (append)

- [ ] Step 1: Write the failing test (the existing C1-of-batch-plan pattern):

```python
def test_is_operator_route_matches_webhook_planes():
    from usan_api.ratelimit import _is_operator_route
    assert _is_operator_route("POST", "/v1/webhook-endpoints")
    assert _is_operator_route("GET", "/v1/webhook-endpoints/abc/deliveries")
    assert _is_operator_route("PATCH", "/v1/webhook-endpoints/abc")
    assert _is_operator_route("POST", "/v1/webhook-deliveries/abc/redeliver")
```
  Plus `test_webhook_routes_throttled_pre_auth` mirroring the existing flood test (2/minute budget, no auth → 429 after budget on `GET /v1/webhook-endpoints`).

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_app_security.py -v` — RED: returns False (the endpoints would ship unthrottled).
- [ ] Step 3: Implement — add to `_is_operator_route` (+docstring): `if path.startswith("/v1/webhook-endpoints") or path.startswith("/v1/webhook-deliveries"): return True`.
- [ ] Step 4: `cd apps/api && uv run pytest tests/test_app_security.py -v && ruff check src/usan_api/ratelimit.py && uv run mypy .`
- [ ] Step 5: `git add apps/api/src/usan_api/ratelimit.py apps/api/tests/test_app_security.py && git commit -m "fix(api): rate-limit allowlist covers /v1/webhook-endpoints and /v1/webhook-deliveries (pre-auth)"`

---

### Task D2: `schemas/webhook_endpoints.py`

**Files:**
- Create: `apps/api/src/usan_api/schemas/webhook_endpoints.py`
- Test: `apps/api/tests/test_webhook_endpoint_schemas.py`

- [ ] Step 1: Write the failing test:
  - `test_create_normalizes_events` — `events=["call.completed", "call.started", "call.completed"]` → `["call.completed", "call.started"]` (de-duplicated, **sorted**, §3.1).
  - `test_create_rejects_unknown_event`, `test_create_rejects_ping_subscription` (`["ping"]` → ValidationError — not subscribable), `test_create_rejects_empty_events`.
  - `test_create_url_runs_ssrf_validator` — `url="https://metadata.google.internal/"` → ValidationError; `"http://x.example.com"` → ValidationError; valid `https://hooks.example.com:8443/p` passes (full matrix lives in B2; this pins the schema→`ssrf_guard.validate_webhook_url` wiring).
  - `test_description_capped_500`.
  - `test_update_all_optional_and_url_revalidated` — empty PATCH body validates; `UpdateWebhookEndpointRequest(url="https://10.0.0.1/")` → ValidationError (PATCH re-runs the **full** gate, §8.1).
  - `test_responses_have_no_secret_field` — `"secret" not in WebhookEndpointResponse.model_fields`; `"secret" in WebhookEndpointCreatedResponse.model_fields` (the 201-only shape).

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_webhook_endpoint_schemas.py -v` — RED: module missing.

- [ ] Step 3: Implement: `MAX_WEBHOOK_ENDPOINTS = 10`, `MAX_DESCRIPTION_LENGTH = 500`, `MAX_PENDING_FOR_REDELIVER = 100`; `CreateWebhookEndpointRequest {url: str, description: str|None ≤500, events: list[str]}` with `_validate_url` (calls `ssrf_guard.validate_webhook_url`, ValueError→422) and `_normalize_events` (`sorted(set(v))`, every member in `webhook_events.WEBHOOK_EVENTS`); `UpdateWebhookEndpointRequest` (all-optional `url/description/events/enabled`, same validators when present); `WebhookEndpointResponse {id, url, description, enabled, events, consecutive_failures, disabled_reason, pending_deliveries: int, created_at, updated_at}` + `from_model(e, pending)`; `WebhookEndpointCreatedResponse(WebhookEndpointResponse)` + `secret: str` (a **superset** of spec §4's create-response shape — deliberate, executor note 4; the create path calls `from_model(e, pending=0)`); `WebhookDeliveryResponse {id, event, status, attempts, next_attempt_at, response_code, last_error, delivered_at, created_at, updated_at, payload}` + `from_model`; `EnqueuedDeliveryResponse {delivery_id}`.

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_webhook_endpoint_schemas.py -v && ruff check . && uv run mypy .`
- [ ] Step 5: `git add apps/api/src/usan_api/schemas/webhook_endpoints.py apps/api/tests/test_webhook_endpoint_schemas.py && git commit -m "feat(api): webhook endpoint schemas — SSRF-validated URL, normalized closed-enum subscriptions, secret-once response"`

---

### Task D3: `repositories/webhook_endpoints.py` + outbox redeliver/pending counts

**Files:**
- Create: `apps/api/src/usan_api/repositories/webhook_endpoints.py`
- Modify: `apps/api/src/usan_api/repositories/webhook_outbox.py` (second sequential edit)
- Test: `apps/api/tests/test_webhook_endpoints_repo.py`

- [ ] Step 1: Write the failing test:
  - `test_create_get_list_delete_and_count` — CRUD round-trip; `count_endpoints` tracks; delete cascades delivery rows (seed one pending first).
  - `test_pending_counts_per_endpoint` — two endpoints, 3 + 0 pending rows → `pending_counts(db) == {e1.id: 3}` (absent = 0) and `count_pending_for_endpoint(db, e1.id) == 3`.
  - `test_increment_failures_is_atomic_sql` — open-txn interleaving (the C3 helper pattern): session A `increment_failures` (uncommitted), session B `reset_failures` blocks, A commits → B proceeds; final value 0; then one more increment → **1, not 2** (no lost update against a concurrent PATCH-reset, spec §5.5).
  - `test_increment_skips_disabled_endpoint` — `enabled=false` → `increment_failures` returns `None` (the `WHERE ... AND enabled` predicate).
  - `test_trip_breaker_one_shot` — first `trip_breaker` → `True`, `enabled=False`, `disabled_reason='circuit_breaker'`; second → `False` (guarded UPDATE — exactly-once WARN/metric semantics).
  - `test_trip_vs_reenable_race` — **concurrent, not sequential** (the exactly-once claim is about races, §10.11): open-txn helper — A `trip_breaker` uncommitted, B `reenable` blocks on the row lock, A commits → B proceeds and commits → final state `enabled=True, consecutive_failures=0, disabled_reason=None` (operator re-arm wins as last writer); a subsequent `trip_breaker` returns `True` again (the one-shot guard re-armed).
  - `test_concurrent_trips_single_true` — A `trip_breaker` vs B `trip_breaker` on the same endpoint → exactly one returns `True` (one WARN, one metric increment downstream).
  - `test_reenable_resets_breaker_state` — `reenable(db, endpoint)` → `enabled=True`, `consecutive_failures=0`, `disabled_reason=None`.
  - `test_redeliver_guarded_sql_reset` (outbox) — `failed` row → `redeliver(db, id)` returns the id, row now `pending/attempts=0/next_attempt_at<=now()/response_code NULL/last_error NULL`; on a `pending` row → returns `None` (the status predicate is load-bearing — a Python check would race the poller's claim, §4).

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_webhook_endpoints_repo.py -v` — RED: module missing.

- [ ] Step 3: Implement (flush-only):

```python
# repositories/webhook_endpoints.py
async def create_endpoint(db, *, url, description, events, secret) -> WebhookEndpoint
async def get_endpoint(db, endpoint_id) -> WebhookEndpoint | None
async def list_endpoints(db) -> list[WebhookEndpoint]          # bounded by the 10-cap
async def count_endpoints(db) -> int
async def delete_endpoint(db, endpoint) -> None
async def reenable(db, endpoint) -> None                       # enabled=True, failures=0, reason=None
async def increment_failures(db, endpoint_id) -> int | None:
    # UPDATE webhook_endpoints SET consecutive_failures = consecutive_failures + 1
    # WHERE id=:id AND enabled RETURNING consecutive_failures   (atomic SQL, §5.5)
async def reset_failures(db, endpoint_id) -> None              # SET consecutive_failures = 0
async def trip_breaker(db, endpoint_id) -> bool:
    # UPDATE ... SET enabled=false, disabled_reason='circuit_breaker'
    # WHERE id=:id AND enabled RETURNING id  -> bool (one-shot)

# repositories/webhook_outbox.py (append)
async def pending_counts(db) -> dict[uuid.UUID, int]           # GROUP BY endpoint_id WHERE pending
async def count_pending_for_endpoint(db, endpoint_id) -> int
async def get_delivery(db, delivery_id) -> WebhookDelivery | None
async def list_deliveries(db, *, endpoint_id, status=None, event=None,
                          limit=50, offset=0) -> list[WebhookDelivery]  # clamp 1..100, (created_at, id) DESC
async def redeliver(db, delivery_id) -> uuid.UUID | None:
    # UPDATE ... SET status='pending', attempts=0, next_attempt_at=now(),
    # response_code=NULL, last_error=NULL
    # WHERE id=:id AND status IN ('delivered','failed') RETURNING id
```

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_webhook_endpoints_repo.py tests/test_webhook_outbox.py -v && ruff check . && uv run mypy .`
- [ ] Step 5: `git add apps/api/src/usan_api/repositories/webhook_endpoints.py apps/api/src/usan_api/repositories/webhook_outbox.py apps/api/tests/test_webhook_endpoints_repo.py && git commit -m "feat(api): webhook_endpoints repo (atomic breaker SQL) + guarded redeliver/pending-count outbox reads"`

---

### Task D4: `routers/webhook_endpoints.py` + `main.py` registration + sentinel-actor audit

**Files:**
- Create: `apps/api/src/usan_api/routers/webhook_endpoints.py` (distinct from the **inbound** `routers/webhooks.py` — do not touch that file)
- Modify: `apps/api/src/usan_api/main.py` (import + `include_router` after `batches`), `apps/api/src/usan_api/db/models.py` (**add** an `AdminAuditLog` docstring — the model has none today: the `operator-api-key` sentinel deliberately breaks the admin-session-identity assumption, §4)
- Test: `apps/api/tests/test_webhook_endpoints_api.py`

- [ ] Step 1: Write the failing test (uses `client` + `operator_headers`; flag-on tests follow executor note 6):
  - `test_create_201_returns_secret_exactly_once` — 201 body has 64-hex `secret`; subsequent `GET` (list + detail) and the PATCH response **never** contain a `secret` key (assert on raw JSON).
  - `test_create_422_invalid_url_and_events` — SSRF-rejected URL → 422; `events=["ping"]` → 422.
  - `test_create_422_over_endpoint_cap` — seed 10 → 11th create 422 (app-level count, §3.1/§8.4).
  - `test_list_includes_breaker_state_and_pending_count` — seeded pending rows → `pending_deliveries` correct; `disabled_reason`/`consecutive_failures` present.
  - `test_patch_reenable_resets_breaker` — endpoint with `enabled=false, disabled_reason='circuit_breaker', consecutive_failures=10` → `PATCH {"enabled": true}` → 200 with `consecutive_failures == 0`, `disabled_reason is None` (the operator re-arm path, §5.5).
  - `test_patch_url_revalidated` — `PATCH {"url": "https://localhost/"}` → 422.
  - `test_delete_204_cascades_pending_backlog` — delete → 204; delivery rows gone.
  - `test_test_ping_409_when_delivery_disabled` — flag default false → `POST .../test` → 409 ("a test that can never send is a lie", §4).
  - `test_test_ping_enqueues_real_pipeline_row` — flag on (note 6) → 202 `{delivery_id}`; row `event='ping'`, `status='pending'`, `next_attempt_at <= now()`; 409 when the endpoint is disabled. (Deterministic despite the live poller — executor note 9: the background poller holds the flag-off lifespan-time settings snapshot and never claims the row.)
  - `test_deliveries_list_paged_filtered` — newest-first; `?status=failed` and `?event=ping` filters; `limit` clamped ≤100; each item carries `updated_at` (the last-attempt timestamp) and the PHI-free `payload`.
  - `test_redeliver_semantics` — failed row → 202 + row reset (guarded SQL, D3); pending row → 409; disabled endpoint → 409; seed 100 pending on the endpoint → **429** (backpressure cap, §4/§8.4).
  - `test_mutations_write_sentinel_audit_rows_same_commit` — after create/patch/delete/test/redeliver, `admin_audit_log` has rows with `actor_email == "operator-api-key"` and actions `webhook_endpoint_created|updated|deleted`, `webhook_test_sent`, `webhook_redelivered`; detail carries `endpoint_id` + changed field **names**; assert the serialized detail of every row contains neither the secret value nor the URL string (§4).
  - `test_requires_operator_token` — 401 on every method without the bearer.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_webhook_endpoints_api.py -v` — RED: 404 (router unregistered).

- [ ] Step 3: Implement `routers/webhook_endpoints.py`:

```python
OPERATOR_ACTOR = "operator-api-key"   # sentinel — durable DB audit for egress config (§4)
router = APIRouter(prefix="/v1/webhook-endpoints", tags=["webhook-endpoints"],
                   dependencies=[Depends(require_operator_token)])
deliveries_router = APIRouter(prefix="/v1/webhook-deliveries", tags=["webhook-endpoints"],
                              dependencies=[Depends(require_operator_token)])
# POST ""            -> 422 cap via count_endpoints() >= MAX_WEBHOOK_ENDPOINTS;
#                       secret = webhook_signing.generate_secret(); repo create;
#                       audit record(actor_email=OPERATOR_ACTOR, action="webhook_endpoint_created",
#                       entity_type="webhook_endpoint", entity_id=str(e.id),
#                       detail={"events": body.events}) in the SAME commit;
#                       201 WebhookEndpointCreatedResponse via from_model(e, pending=0) + secret.
# GET "" / GET "/{id}"      -> pending_counts() join; never the secret.
# PATCH "/{id}"             -> apply present fields; enabled=True path calls reenable();
#                              audit detail={"changed": sorted(present_field_names)}; commit.
# DELETE "/{id}"            -> 204; audit; commit.
# POST "/{id}/test"         -> 409 if not settings.webhook_delivery_enabled; 409 if not endpoint.enabled;
#                              enqueue_ping; audit "webhook_test_sent"; commit; 202.
# GET "/{id}/deliveries"    -> list_deliveries(limit<=100 default 50, offset, status, event).
# deliveries_router POST "/{id}/redeliver" -> 404 unknown; endpoint disabled -> 409;
#                              count_pending_for_endpoint >= 100 -> 429; redeliver() None+row pending -> 409;
#                              audit "webhook_redelivered"; commit; 202.
```
  Register **both** routers in `main.py` after `batches`. Add the `AdminAuditLog` docstring with the sentinel note.

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_webhook_endpoints_api.py tests/test_app_security.py -v && ruff check . && uv run mypy .`
- [ ] Step 5: `git add apps/api/src/usan_api/routers/webhook_endpoints.py apps/api/src/usan_api/main.py apps/api/src/usan_api/db/models.py apps/api/tests/test_webhook_endpoints_api.py && git commit -m "feat(api): /v1/webhook-endpoints operator CRUD + test ping + deliveries + capped redeliver with sentinel-actor audit"`

---

## Part E — Delivery worker

### Task E1: Outbox claim (attempt-bump lease), guarded outcomes, housekeeping

**Files:**
- Modify: `apps/api/src/usan_api/repositories/webhook_outbox.py` (third sequential edit)
- Test: `apps/api/tests/test_webhook_outbox_worker.py`

- [ ] Step 1: Write the failing test (direct-session tests; manipulate `next_attempt_at`/`created_at`/`updated_at` via raw `UPDATE` for time travel):
  - `test_claim_bumps_attempts_and_ladder` — fresh row claimed at `now` → returned with `attempts == 1` and DB `next_attempt_at ≈ now+1m`; re-claim after time travel → `attempts == 2`, `+5m`; then `3` → `+30m`; then `4` → `+30m`; a row already at `attempts=4` is **never** claimed (`attempts < 4` predicate).
  - `test_claim_skips_disabled_endpoints` — pending row whose endpoint is `enabled=false` → not claimed (the `JOIN ... AND e.enabled`); re-enable → claimed with its attempt count intact.
  - `test_claim_orders_oldest_due_first_and_limit` — three due rows → returned ordered by `next_attempt_at`, capped at `limit`.
  - `test_claim_skip_locked_disjoint` — two concurrent sessions (open-txn pattern) claim disjoint rows.
  - `test_crash_after_claim_reoffers_at_next_rung` — claim, commit, write **no outcome** → row still `pending` and claimable once `now > next_attempt_at` (the crash-safe lease; no reclaim sweeper needed, §5.2/§5.4).
  - `test_mark_delivered_guarded_idempotent` — `mark_delivered` on pending → `True`, `delivered_at`/`response_code` set; second call → `False`, row untouched.
  - `test_mark_attempt_failed_guarded_and_terminal` — non-terminal: `pending` retained, `response_code`/`last_error` recorded; with `terminal=True`: `status='failed'`; on an already-`delivered` row → `False` (guarded like `mark_delivered`, review L3).
  - `test_sweep_crash_residue_coalesces_last_error` — `pending, attempts=4, updated_at 11m old, last_error='ConnectTimeout'` → swept to `failed`, `last_error` **stays** `'ConnectTimeout'`; same row with `last_error NULL` → `'crash_residual'` (COALESCE, review L4d).
  - `test_expire_stale_pending_after_7_days` — `created_at` 8 days old → `failed`, `last_error='expired'`; 6-day row untouched (§5.4).
  - `test_prune_old_30_days` — delivered/failed rows 31 days old deleted; 29-day rows and **old pending** rows kept.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_webhook_outbox_worker.py -v` — RED: functions missing.

- [ ] Step 3: Implement — `claim_due(db, *, now, limit=20) -> list[ClaimedDelivery]` executing the spec §5.2 CTE **verbatim modulo one deliberate substitution: every `now()` becomes the bound `:now` parameter** (required by the time-travel tests — un-parametrized SQL fails the ladder assertions) via `text()`; return a small frozen dataclass `ClaimedDelivery(id, endpoint_id, event, payload, attempts)`; `mark_delivered(db, delivery_id, *, response_code) -> bool` and `mark_attempt_failed(db, delivery_id, *, response_code, last_error, terminal) -> bool` as guarded `UPDATE … WHERE id=:id AND status='pending' RETURNING id`; `sweep_crash_residue`, `expire_stale_pending`, `prune_old`, `count_pending(db) -> int` per spec §5.4 SQL (each takes `now`).

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_webhook_outbox_worker.py -v && ruff check . && uv run mypy .`
- [ ] Step 5: `git add apps/api/src/usan_api/repositories/webhook_outbox.py apps/api/tests/test_webhook_outbox_worker.py && git commit -m "feat(api): outbox claim with attempt-bump lease + guarded outcomes + crash-residue/7-day-expiry/30-day-prune housekeeping"`

---

### Task E2: `webhook_delivery.py` — signed POSTs, grouping, breaker, housekeeping cycle

**Files:**
- Create: `apps/api/src/usan_api/webhook_delivery.py`
- Test: `apps/api/tests/test_webhook_delivery.py`

- [ ] Step 1: Write the failing test — monkeypatch `webhook_delivery._build_client` to return `httpx.AsyncClient(transport=httpx.MockTransport(handler))` and `ssrf_guard._resolve` to public addresses by default; settings via env + `get_settings.cache_clear()` with `WEBHOOK_DELIVERY_ENABLED=true` unless stated:
  - `test_delivers_signed_post_2xx` — endpoint seeded with **`consecutive_failures=5`** (a zero-seeded endpoint makes the reset assertion vacuous — the `reset_failures` wiring could be deleted and a 0→0 check still passes); handler captures the request; after `poll_once`: row `delivered` + `response_code=200`; endpoint `consecutive_failures` **dropped 5 → 0**; on the wire: `Content-Type: application/json`, `User-Agent: usan-voice-engine-webhooks/1.0`, **`X-Usan-Event` value `== claimed.event`** (not mere presence), `X-Usan-Delivery-Id == str(row.id)`, and `X-Usan-Signature` **verifies via the spec §7 snippet against the raw request bytes**; parsed body contains `delivery_id == str(row.id)` (signed-body dedupe key, §6.1/§10.5).
  - `test_non_2xx_schedules_retry` — handler 500 → row `pending`, `attempts=1`, `response_code=500`, `last_error == "HTTPStatusError"`; breaker `consecutive_failures == 1`.
  - `test_3xx_is_failure_never_followed` — handler 302 with `Location` → exactly one request seen (no follow), failure recorded (`follow_redirects=False`, §8.2).
  - `test_timeout_and_transport_errors_recorded_as_type_names` — handler raises `httpx.ConnectTimeout` → `last_error == "ConnectTimeout"`; never any fragment of the URL or exception text in the row (assert sentinel substring absent).
  - `test_dns_failure_no_post_feeds_breaker` — parametrized: `ssrf_guard._resolve` raises `socket.gaierror` / generic `OSError` → handler **never invoked**, attempt failed with `last_error == "gaierror"` / `"OSError"` (type-name rule), breaker incremented, row re-offered (`pending`, non-terminal). **The most common dead-receiver mode (NXDOMAIN) must not escape `deliver_one`'s except tuple, abort the endpoint group mid-`gather`, and rot as `crash_residual`.**
  - `test_terminal_attempt_marks_failed` — row at `attempts=3` (claim → 4), handler 500 → `status='failed'`.
  - `test_ssrf_block_no_post_feeds_breaker` — `_resolve` → `["10.0.0.5"]` → handler **never invoked**, attempt failed with `last_error='SsrfBlocked'`, breaker incremented (§8.2/§10.4).
  - `test_terminal_ssrf_row_failed_with_type_name` — row at `attempts=3` (claim → 4), `_resolve` → `["10.0.0.5"]` → DB row `status='failed'` **and** `last_error='SsrfBlocked'` preserved (the row-level half of the §5.3 alert-honesty rule; the metric-label half lands in F1).
  - `test_breaker_trips_once_at_threshold_and_stops_group` — `WEBHOOK_DELIVERY_CIRCUIT_BREAKER_THRESHOLD=1`; 3 due rows for one endpoint, handler 500 → exactly 1 HTTP call; endpoint `enabled=False`, `disabled_reason='circuit_breaker'`; loguru capture: exactly one WARN bound with `endpoint_id` and **no URL in any record**; remaining rows still `pending` (mid-cycle skip, §5.3 step 1 / §5.5).
  - `test_groups_deliver_concurrently_ordered_within` — endpoints A (2 rows) and B (1 row); A's first handler `await`s an `asyncio.Event` that B's handler sets → cycle completes; **wrap the cycle in `asyncio.wait_for(poll_once(...), timeout=5)` so a sequential-delivery regression FAILS instead of hanging the suite** (§5.2); A's rows arrive oldest-first.
  - `test_per_row_outcome_commits` — 2 rows, first 200 / second 500 → after the cycle a fresh session sees one `delivered` + one `pending` (per-row commit bounds the duplicate window, §5.3).
  - `test_pending_count_computed_every_cycle` — 3 pending rows, `poll_once(run_housekeeping=False)` → returned stats dict carries `pending == 3` (**spec §9: backlog visibility is per-cycle, not hourly** — the gauge `.set()` wiring on this value lands in F1).
  - `test_flag_off_no_claims_but_housekeeping_runs` — `WEBHOOK_DELIVERY_ENABLED=false`: due row stays `attempts=0` (never claimed), zero HTTP calls; an 8-day-old pending row is expired by `poll_once(..., run_housekeeping=True)` (§5.1/§10.12).
  - `test_housekeeping_skipped_unless_requested` — `run_housekeeping=False` leaves the 8-day row pending (sweep/expire/prune are hourly; the pending count is NOT — see previous test).
  - `test_housekeeping_due_helper` — pure-function cadence pin (the §10.10 "hourly cadence" row needs a named test): `_housekeeping_due(None, t)` → `True` (first cycle always); `_housekeeping_due(t, t + 3599.0)` → `False`; `_housekeeping_due(t, t + 3600.0)` → `True`.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_webhook_delivery.py -v` — RED: module missing.

- [ ] Step 3: Implement (module docstring: the §5 design — claim lease, no locks across POSTs, at-least-once, breaker):

```python
class WebhookDeliveryError(Exception): ...
_HOUSEKEEPING_INTERVAL_S = 3600.0

def _housekeeping_due(last_run: float | None, now: float) -> bool:
    # pure seam: None (first cycle) -> True; else now - last_run >= _HOUSEKEEPING_INTERVAL_S

def _build_client(settings: Settings) -> httpx.AsyncClient:   # seam for tests
    return httpx.AsyncClient(timeout=settings.webhook_delivery_timeout_s, follow_redirects=False)

async def deliver_one(factory, settings, claimed: ClaimedDelivery, client) -> str:
    # own short txn AFTER the claim-commit (sms_outbox discipline):
    # 1. load endpoint url/secret/enabled; disabled -> skip (return "skipped"; bumped lease re-offers)
    # 2. await ssrf_guard.resolve_public_or_raise(host)  -> SsrfBlocked path
    # 3. body = dict(claimed.payload); body["delivery_id"] = str(claimed.id)
    #    raw = webhook_signing.canonical_bytes(body); ts_ms = int(time.time()*1000)
    #    headers: Content-Type/User-Agent/X-Usan-Event/X-Usan-Delivery-Id/
    #    X-Usan-Signature = signature_header(ts_ms, sign(secret, ts_ms, raw))
    # 4. POST; raise_for_status; 2xx -> mark_delivered + reset_failures, commit -> "delivered"
    # 5. except (httpx.HTTPError, OSError, ValueError, SsrfBlocked) as exc:
    #    # OSError covers socket.gaierror (NXDOMAIN) from resolve_public_or_raise —
    #    # type-name rule records 'gaierror'; httpx.TransportError does NOT subclass
    #    # OSError so both families are needed (executor note 4).
    #    terminal = claimed.attempts >= 4
    #    mark_attempt_failed(..., last_error=type(exc).__name__, terminal=terminal)
    #    n = increment_failures(...);  n == threshold -> trip_breaker -> WARN (endpoint_id only)
    #    commit -> "failed" if terminal else ("ssrf_blocked" if isinstance(exc, SsrfBlocked) else "retry_scheduled")
    # Outcome strings: delivered|retry_scheduled|failed|ssrf_blocked|skipped — §5.3 label rule.
    # F1 counts ONLY the first four; "skipped" is no-attempt, never a metric label.
    # INFO log per outcome (delivery_id, endpoint_id, event, outcome, response_code; NEVER the URL).

async def poll_once(factory, settings, *, now=None, run_housekeeping=False) -> dict[str, int]:
    # EVERY-CYCLE half (flag-INDEPENDENT): count_pending -> stats["pending"]
    #   (spec §9: the backlog gauge is per-cycle; F1 wires .set() here).
    # HOURLY half (flag-INDEPENDENT, when run_housekeeping): sweep_crash_residue,
    #   expire_stale_pending, prune_old — one txn, commit.
    # delivery half (only if settings.webhook_delivery_enabled): claim_due (own txn, commit
    #   immediately — no locks across POSTs); group by endpoint_id; asyncio.gather over groups,
    #   sequential oldest-first within a group; one shared client per cycle.

async def run_poller(settings: Settings, stop: asyncio.Event) -> None:
    # byte-for-byte schedule_orchestrator.run_poller discipline (597-616):
    # logger.bind(component="webhook_delivery"); try/except per cycle;
    # run_housekeeping = _housekeeping_due(last_hk, time.monotonic()) per cycle;
    # contextlib.suppress(TimeoutError) + asyncio.wait_for(stop.wait(), interval).
```

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_webhook_delivery.py -v && ruff check . && uv run mypy .`
- [ ] Step 5: `git add apps/api/src/usan_api/webhook_delivery.py apps/api/tests/test_webhook_delivery.py && git commit -m "feat(api): webhook delivery worker — signed POSTs, per-endpoint concurrent groups, retry ladder, circuit breaker, DNS-failure handling, always-on housekeeping"`

---

### Task E3: Lifespan — the 4th poller, ALWAYS started

**Files:**
- Modify: `apps/api/src/usan_api/main.py`
- Test: `apps/api/tests/test_lifespan_poller.py` (append new tests **and patch the existing ones**)

- [ ] Step 1: Write the failing test (existing fake-poller pattern):
  - `test_lifespan_always_starts_webhook_poller` — **no** `WEBHOOK_DELIVERY_ENABLED` env set → fake `webhook_delivery.run_poller` started; stop event shared with the retry poller's and set at shutdown.
  - `test_webhook_poller_starts_even_with_flag_off` — `WEBHOOK_DELIVERY_ENABLED=false` explicitly → still started (the flag gates delivery, not the task — §5.1).
  - **Patch the existing tests in this file** (`test_lifespan_starts_and_stops_poller`, the skip-when-disabled test, and any other that enters lifespan): they monkeypatch only `retry_orchestrator.run_poller`, so after this task the **real** `webhook_delivery.run_poller` would start inside them, attempt a DB connect to the bogus `postgresql://u:p@host/db` URL, and ERROR-spam via the per-cycle try/except. Add a `webhook_delivery.run_poller` fake to each (or one module-scoped helper both use) so they stay quiet and fully faked.

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_lifespan_poller.py -v` — RED: never started.
- [ ] Step 3: Implement — `from usan_api import webhook_delivery` (extend the existing import line) and append **unconditionally** in `lifespan`: `poller_tasks.append(asyncio.create_task(webhook_delivery.run_poller(settings, stop)))`, with the §5.1 comment (housekeeping/gauge must run flag-off) **and the executor-note-9 comment** (every `client`-fixture test now runs this poller; safe because flag-off ⇒ no claims and the poller holds the lifespan-time settings snapshot — per-test `setenv` cannot turn it on).
- [ ] Step 4: `cd apps/api && uv run pytest tests/test_lifespan_poller.py -v && ruff check src/usan_api/main.py && uv run mypy .`
- [ ] Step 5: `git add apps/api/src/usan_api/main.py apps/api/tests/test_lifespan_poller.py && git commit -m "feat(api): always-on webhook delivery poller in lifespan (flag gates delivery only)"`

---

## Part F — Observability + infra plumbing

### Task F1: Metrics — two counters + pending gauge, increment-after-commit wiring

**Files:**
- Modify: `apps/api/src/usan_api/observability/custom_metrics.py`, `apps/api/src/usan_api/webhook_delivery.py` (second sequential edit)
- Test: `apps/api/tests/test_webhook_observability.py`

- [ ] Step 1: Write the failing test (`conftest.counter_value`/`gauge_value`; MockTransport fixtures from E2):
  - `test_delivered_increments_counter` — 2xx cycle → `counter_value(WEBHOOK_DELIVERIES_TOTAL, event="ping", outcome="delivered")` +1.
  - `test_retry_and_terminal_outcome_labels` — 500 on attempts 1–3 → `outcome="retry_scheduled"` ×3; attempt 4 → `outcome="failed"` ×1.
  - `test_dns_failure_outcome_label` — `_resolve` raises `socket.gaierror`, non-terminal → `outcome="retry_scheduled"` (DNS failures stay inside the contracted label set; the type name lives only in `last_error`).
  - `test_ssrf_outcome_label_rule` — SSRF block on a non-terminal attempt → `outcome="ssrf_blocked"`; on the **terminal** attempt → `outcome="failed"` (the alert-honesty rule, §5.3 — a permanently-private endpoint must still reach the failure alert; E2 already pinned the row state).
  - `test_breaker_trip_metric_exactly_once_and_skipped_uncounted` — threshold 1, three failing rows for one endpoint → `WEBHOOK_ENDPOINTS_AUTO_DISABLED_TOTAL` +1 exactly (guarded-UPDATE one-shot); **`WEBHOOK_DELIVERIES_TOTAL` gained exactly one sample total for the endpoint (the tripping failure), no sample with `outcome="skipped"` exists anywhere in the registry, and the two breaker-skipped rows incremented nothing** (the spec §9 outcome set is closed: `delivered|retry_scheduled|failed|ssrf_blocked` — `"skipped"` is a no-attempt internal string, not a label).
  - `test_pending_gauge_set_every_cycle_even_flag_off` — flag off, 3 pending rows, **`poll_once(run_housekeeping=False)`** → `gauge_value(WEBHOOK_PENDING_DELIVERIES) == 3.0` (per-cycle, NOT hourly — spec §9/§5.1; E2's stats already carry the count).

- [ ] Step 2: `cd apps/api && uv run pytest tests/test_webhook_observability.py -v` — RED: `ImportError: WEBHOOK_DELIVERIES_TOTAL`.

- [ ] Step 3: Implement — in `custom_metrics.py` (house comment style: bounded labels documented; named to avoid colliding with the inbound `WEBHOOKS_TOTAL`):

```python
WEBHOOK_DELIVERIES_TOTAL = Counter("usan_webhook_deliveries",
    "Outbound webhook delivery attempts by event and outcome.",
    labelnames=("event", "outcome"))      # outcome: delivered|retry_scheduled|failed|ssrf_blocked
                                          # CLOSED SET — "skipped" (breaker no-attempt) is never recorded
WEBHOOK_ENDPOINTS_AUTO_DISABLED_TOTAL = Counter("usan_webhook_endpoints_auto_disabled",
    "Webhook endpoints auto-disabled by the circuit breaker.")
WEBHOOK_PENDING_DELIVERIES = Gauge("usan_webhook_pending_deliveries",
    "Outbox rows with status='pending' (backlog visibility; no labels; set every poller cycle).")
```
  Wire `webhook_delivery`: `_COUNTED_OUTCOMES = frozenset({"delivered", "retry_scheduled", "failed", "ssrf_blocked"})`; increment `WEBHOOK_DELIVERIES_TOTAL.labels(event=claimed.event, outcome=outcome).inc()` **after** each row's commit **only when `outcome in _COUNTED_OUTCOMES`** (`"skipped"` falls through uncounted); `WEBHOOK_ENDPOINTS_AUTO_DISABLED_TOTAL.inc()` only when `trip_breaker` returned `True`, after commit; gauge `.set(stats["pending"])` in the **every-cycle** half (not the hourly housekeeping branch).

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_webhook_observability.py tests/test_webhook_delivery.py -v && ruff check . && uv run mypy .`
- [ ] Step 5: `git add apps/api/src/usan_api/observability/custom_metrics.py apps/api/src/usan_api/webhook_delivery.py apps/api/tests/test_webhook_observability.py && git commit -m "feat(api): webhook delivery counters + per-cycle pending gauge with increment-after-commit + closed outcome label set"`

---

### Task F2: Env plumbing (3 infra files + compose overlay) + two alert rules + contract tests

**Files:**
- Modify: `infra/docker-compose.yml`, `infra/docker-compose.prod.yml`, `infra/.env.example`, `infra/.env.prod.example`, `infra/grafana/provisioning/alerting/usan_alerts.yml`, `scripts/tests/test_alerting_provisioning.py`
- Test: `apps/api/tests/test_infra_webhooks_env.py` (clone of `test_infra_scheduler_env.py`, including its `_ComposeLoader` + `_api_env` helpers)

- [ ] Step 1: Write the failing tests:
  - `test_compose_api_service_has_webhook_env` — all 4 `WEBHOOK_DELIVERY_*` keys in dev compose api environment.
  - `test_dev_compose_enables_delivery_flag` — `env["WEBHOOK_DELIVERY_ENABLED"] == "${WEBHOOK_DELIVERY_ENABLED:-true}"` (scheduler precedent — otherwise `/test` 409s in every dev stack, §10.14).
  - `test_prod_overlay_pins_flag_off` — prod overlay `== "${WEBHOOK_DELIVERY_ENABLED:-false}"`.
  - `test_env_example_keys_commented_with_inbound_outbound_block` — `# WEBHOOK_DELIVERY_…=` for all 4 keys, **and** the disambiguation comment block present (assert a stable sentinel line, e.g. `"WEBHOOK_MAX_AGE_S above is the INBOUND LiveKit"`).
  - `test_env_prod_example_pins_false` — all 4 keys present; `"WEBHOOK_DELIVERY_ENABLED=false"` literal.
  - `test_alert_rule_uids_in_env_contract` — `usan_alerts.yml` text contains `usan-webhook-delivery-failed` **and** `usan-webhook-endpoint-auto-disabled`.
  - In `scripts/tests/test_alerting_provisioning.py`: extend `test_alert_rules_present`'s expected set with both new uids (the nodata/execerr loop test covers them automatically, §9) and add `test_webhook_alert_rules_shape` — both rules: `for == "0m"`, severity `warning`, expr contains `usan_webhook_deliveries_total{outcome="failed"}` / `usan_webhook_endpoints_auto_disabled_total` with `[30m]` windows.

- [ ] Step 2: RED — `cd apps/api && uv run pytest tests/test_infra_webhooks_env.py -v` (keys missing) and `python3 -m pytest scripts/tests/test_alerting_provisioning.py -v` from the repo root (uid set fails).

- [ ] Step 3: Implement — compose: 4 keys on the api service (dev `:-true` flag, prod overlay re-pin `:-false`; the other three mirror their defaults). `.env.example`: commented block with the inbound-vs-outbound `WEBHOOK_*` comment (spec §5.1). `.env.prod.example`: live-value `false` + commented tuning keys + the §11.3 enable-sequence comment. `usan_alerts.yml`: clone `usan-sms-delivery-failed` twice — `usan-webhook-delivery-failed` (`sum(increase(usan_webhook_deliveries_total{outcome="failed"}[30m])) > 0`, `relativeTimeRange.from: 1800`) and `usan-webhook-endpoint-auto-disabled` (`sum(increase(usan_webhook_endpoints_auto_disabled_total[30m])) > 0`), both `for: 0m`, `noDataState: OK` / `execErrState: Alerting` with the standing rationale comment, severity `warning`, annotation on the breaker rule citing the §11.5 runbook (the breaker mutes the failure alert — the trip itself must page).

- [ ] Step 4: `cd apps/api && uv run pytest tests/test_infra_webhooks_env.py -v` then, from the repo root, `python3 -m pytest scripts/tests -v`.
- [ ] Step 5: `git add infra apps/api/tests/test_infra_webhooks_env.py scripts/tests/test_alerting_provisioning.py && git commit -m "feat(infra): WEBHOOK_DELIVERY_* env plumbing (dev on / prod pinned off) + delivery-failed and breaker-trip alert rules"`

---

## Part G — Full-suite verification

### Task G1: Gate

- [ ] Step 1: Run, in order, every line **from the repo root** (cwd resets between subagent bash calls):

```bash
cd apps/api && uv run pytest -v --tb=short && ruff check . && ruff format --check . && uv run mypy .
cd services/agent && uv run pytest -v && ruff check . && uv run mypy .   # untouched but must stay green
python3 -m pytest scripts/tests -v --tb=short                            # repo root, NOT services/
# Agent-untouched check — the base ref depends on stack state:
#   while PR #56 is unmerged (pre-rebase):  git diff --name-only feat/calls-ui...HEAD -- services/agent
#   after the executor-note-8 rebase onto origin/main: git diff --name-only origin/main...HEAD -- services/agent
# (the stale local feat/calls-ui merge-base drifts after the rebase and main-landed agent
#  changes — e.g. dependabot bumps — become false positives). Either form MUST print nothing.
cd apps/api && uv run --with pytest-cov pytest tests/ \
  --cov=usan_api.ssrf_guard --cov=usan_api.webhook_signing --cov=usan_api.webhook_events \
  --cov=usan_api.webhook_delivery --cov=usan_api.repositories.webhook_outbox \
  --cov=usan_api.repositories.webhook_endpoints --cov=usan_api.repositories.calls \
  --cov=usan_api.schemas.webhook_endpoints --cov=usan_api.routers.webhook_endpoints \
  --cov-report=term-missing --cov-fail-under=80
```
  The coverage command is a **hard gate**: no `|| true`; `--cov` scoped to the new modules **plus `repositories/calls.py`** — the race-hardened mutators are the highest-risk changed code in the plan and must not hide outside the gate.

- [ ] Step 2: Walk the **spec §10 conformance checklist** — every row must tick against a named passing test:
  - [ ] §10.1 migration contract + seeded roundtrip → `test_webhook_migration.py` (all eight; `test_downgrade_seed_upgrade_roundtrip` seeds between downgrade and upgrade and ends at head)
  - [ ] §10.2 models/CHECKs/cascade → `test_webhook_models.py` + `test_check_constraints_enforced` + `test_fk_delete_rule_cascade` (metadata) + D3's `test_create_get_list_delete_and_count` (behavioral cascade)
  - [ ] §10.3 settings → `test_settings_webhooks.py` (defaults/bounds/no-cross-field/namespace)
  - [ ] §10.4 SSRF matrices (both layers, table-driven, public + bracketed-IPv6 literals, fail-closed empty, mapped unwrap, gaierror propagation, real-resolver localhost, terminal-SSRF→failed) → `test_ssrf_guard.py` + `test_ssrf_block_no_post_feeds_breaker` + `test_dns_failure_no_post_feeds_breaker` + `test_terminal_ssrf_row_failed_with_type_name` + `test_ssrf_outcome_label_rule`
  - [ ] §10.5 signature vector + snippet + tolerance + tamper + delivery_id-in-signed-body → `test_webhook_signing.py` + `test_delivers_signed_post_2xx`
  - [ ] §10.6 canonical serialization survives JSONB → `test_canonical_bytes_survives_jsonb_round_trip`
  - [ ] §10.7 same-txn outbox + genuine concurrency (one terminal transition + one event per race; stale dial-failure no-op **with zero enqueues**; caller-path stale dial task; zombie no-op; terminal→terminal zero; cancel_queued_tips per-row; DNC-at-birth; re-cancel emits nothing; phase-6 exactly-once both statuses **+ pre-commit invisibility + joint rollback**) → `test_calls_guarded_transitions.py`, `test_webhook_enqueue_calls.py`, `test_webhook_batch_events.py`
  - [ ] §10.8 fan-out rules + ping bypass + schema dedupe → `test_webhook_outbox.py` + `test_create_normalizes_events`
  - [ ] §10.9 origin root-walk → `test_origin_root_walk_retry_child`, `test_origin_null_for_operator_and_inbound`
  - [ ] §10.10 worker (ladder, enabled join, attempts<4, grouping/ordering with timeout guard, mid-cycle trip, crash re-offer, COALESCE sweep, 7-day expiry, per-row commits, guarded UPDATEs, 3xx/timeout/DNS failures, no redirects, 30-day prune, flag-off housekeeping, hourly cadence via `test_housekeeping_due_helper`, per-cycle pending count) → `test_webhook_outbox_worker.py` + `test_webhook_delivery.py`
  - [ ] §10.11 breaker (atomic SQL, one-shot trip, **concurrent trip-vs-reenable + trip-vs-trip races**, non-vacuous success reset, re-enable resume) → `test_webhook_endpoints_repo.py` + `test_breaker_trips_once_at_threshold_and_stops_group` + `test_delivers_signed_post_2xx` + `test_patch_reenable_resets_breaker`
  - [ ] §10.12 lifespan always-on + flag-off behavior + existing lifespan tests fully faked → `test_lifespan_poller.py` (new + patched) + `test_flag_off_no_claims_but_housekeeping_runs`
  - [ ] §10.13 API (CRUD, secret-once, cap, sentinel audit, /test 409, redeliver 409/409/429, pagination, pending counts, ratelimit) → `test_webhook_endpoints_api.py` + `test_app_security.py`
  - [ ] §10.14 env plumbing + both alert uids → `test_infra_webhooks_env.py` + `test_alerting_provisioning.py`
  - [ ] §10.15 ruff + mypy → the Step 1 commands (CI runs mypy even though CLAUDE.md omits it)

- [ ] Step 3: Commit anything outstanding (explicit `git add`), then stop. **Do not tag or deploy from this plan** — rollout (spec §11) is a separate operator sequence: VM `.env` refresh (flag still false) **before** the `v*` tag; the tag deploy runs migration `0014`; Grafana restart loads the two new rules; enable per §11.3. Re-run the executor-note-8 stacked-branch ritual when PR #55/#56 squash-merge.

---

## Planner disposition (deviations & their grounds)

- **Settings in B1, metrics in F1** — dependency-driven placements copied from the batch plan's executor note 1; Parts D/E read settings, Parts C–E stay metrics-free so F1's tests can fail first.
- **`mark_dial_failure` guards DIALING-only** (spec §2.1 prose says queued/dialing; §10.7's reclaim-race row requires QUEUED to no-op; all callers verified DIALING) — recorded in executor note 4 and the C3 implementation comment.
- **`SsrfBlocked` exception name carries the `last_error` contract** — ruff selects no pep8-naming rules, so the suffix-less name is lint-clean.
- **`cancel_queued_tips` keeps `-> int`** — `.returning(Call)` internally; both callers (`routers/batches.py:211`, `schedule_orchestrator.py:563`) consume counts unchanged.
- **`webhook_outbox.py` is built in three sequenced slices** (C2 enqueue → D3 operator reads/redeliver → E1 worker claim/outcomes/housekeeping) so each slice lands failing-test-first without forward references.
- **201 create response is a superset of spec §4's shape** — single response-model hierarchy; all additive fields PHI-free operator state (executor note 4).
- **`deliver_one` catches `OSError`** so DNS failures record `last_error='gaierror'` by type name instead of escaping the except tuple (executor note 4).

## Review disposition

Adversarial integration review (0 CRITICAL / 0 HIGH / 3 MEDIUM / 5 LOW) and test-strategy review (2 HIGH / 9 MEDIUM / 7 LOW) applied as follows. Overlapping findings (skipped-label, checklist-name) counted once.

**HIGH — applied:**
- TS-H1 (DNS `gaierror` unhandled in both layers) → B2 `test_resolve_propagates_gaierror` + real-resolver `localhost` case; E2 except tuple widened to `OSError`, `test_dns_failure_no_post_feeds_breaker`; F1 `test_dns_failure_outcome_label`; executor note 4.
- TS-H2 (no Part C gate; `cancel_queued_tips` callers untested until G1) → new Task C7 full-suite gate; C4 Step 4 gained `test_batches_api.py` + `test_schedule_orchestrator.py`.

**MEDIUM — all folded:**
- Int-M1 = TS-M4 (`outcome="skipped"` leaks into the closed metric label set) → F1 counts only the four contracted outcomes via `_COUNTED_OUTCOMES`; `test_breaker_trip_metric_exactly_once_and_skipped_uncounted` asserts no skipped sample and zero increments for skipped rows.
- Int-M2 (pending gauge hourly instead of per-cycle, vs spec §9/§5.1) → `count_pending` moved to the every-cycle half of `poll_once`; E2 `test_pending_count_computed_every_cycle`; F1 `test_pending_gauge_set_every_cycle_even_flag_off` uses `run_housekeeping=False`.
- Int-M3 (real always-on poller runs inside every existing lifespan/`client` test) → E3 patches the existing `test_lifespan_poller.py` tests with a `webhook_delivery.run_poller` fake; new executor note 9 records why `client` tests tolerate the live poller (flag-off + lifespan-time settings snapshot — load-bearing for D4's ping test).
- TS-M1 (public IP literals absent from registration reject matrix) → B2 adds `93.184.216.34` + public IPv6 literal (kills an `is_global`-based implementation).
- TS-M2 (`batch.completed` lacked same-txn atomicity proof) → C6 adds `test_batch_event_same_txn_pre_commit_invisible` + `test_batch_event_failure_rolls_back_stamp_and_event`.
- TS-M3 (breaker one-shot only tested sequentially) → D3 adds `test_trip_vs_reenable_race` + `test_concurrent_trips_single_true` (open-txn helper).
- TS-M5 (hourly housekeeping cadence unmapped to any test) → extracted pure `_housekeeping_due` helper + `test_housekeeping_due_helper`; §10.10 checklist row now names it.
- TS-M6 (success-resets-failures pinned vacuously) → `test_delivers_signed_post_2xx` seeds `consecutive_failures=5` and asserts 5 → 0.
- TS-M7 (no-op mutator paths never asserted enqueue-free with a subscribed endpoint) → C4 adds `test_noop_mutators_enqueue_nothing` + caller-path `test_stale_dial_task_caller_path_no_child_no_event`.
- TS-M8 (bare `git commit -m` with untracked files commits nothing) → explicit `git add <files>` on every Step 5, B1 through F1, plus the gate-task stragglers.
- TS-M9 (A1 roundtrip dropped the spec's seed step) → `test_downgrade_seed_upgrade_roundtrip` seeds a `calls` row between downgrade and upgrade and asserts it survives.

**LOW — all folded:**
- Int-L1 (201 superset undeclared; `from_model` create-path arg) → recorded as deliberate deviation in executor note 4 + D2/D4 (`from_model(e, pending=0)`).
- Int-L2 = TS-L2 (checklist names `test_fk_cascade_delete`) → renamed to `test_fk_delete_rule_cascade` in A1 and G1.
- Int-L3 (nonexistent "autouse cache-clear teardown in `conftest.client`") → note 6 reworded to the real mechanism (`client` fixture `finally`, conftest.py:152; the only autouse settings fixture lives in `test_lifespan_poller.py`).
- Int-L4 (anchor drift) → conftest TRUNCATE 104–110; `run_poller` 597–616; D4 now says it **adds** the `AdminAuditLog` docstring.
- Int-L5 (E1 "verbatim CTE" vs `:now` parametrization self-contradiction) → reworded "verbatim modulo `now()` → `:now`".
- TS-L1 (G1 agent-diff base ref breaks after the note-8 rebase) → conditional base ref documented in G1 Step 1.
- TS-L3 (registration matrix gaps) → bracketed-IPv6 decoys added; trailing-dot-on-allowed-host pinned as ACCEPTED; real-resolution `localhost` case added.
- TS-L4 (PHI sentinels missing `sip_call_id`/`egress_id`/`error` JSONB) → added to C1 `test_phi_exclusions_pinned`.
- TS-L5 (terminal SSRF row state unpinned) → E2 `test_terminal_ssrf_row_failed_with_type_name`.
- TS-L6 (concurrency test hangs on regression; header value unasserted) → `asyncio.wait_for(..., timeout=5)` wrapper; `X-Usan-Event` value assertion in the 2xx test.
- TS-L7 (coverage gate excludes `repositories/calls.py`) → `--cov=usan_api.repositories.calls` added to G1.

**Rejected (option-level, within otherwise-applied findings):**
- TS-H1 alternative "catch-and-wrap `gaierror` inside `_resolve`" — rejected: wrapping as `SsrfBlocked` collapses DNS-vs-policy into one `last_error`, destroying the operator's diagnostic distinction; the widened except tuple keeps the type-name rule honest.
- Int-L1 alternative "trim the 201 response to the spec-exact shape" — rejected: a second trimmed model buys nothing; the superset is PHI-free, backward-compatible, and now a recorded deviation.
- TS-M5 alternative "drive a fake-clock `run_poller` two cycles" — rejected: the pure `_housekeeping_due` helper pins the same contract with zero sleeps and no event-loop choreography.

## Files read (for reference)
- Spec: `docs/superpowers/specs/2026-06-10-outbound-webhooks-design.md` (full)
- Format reference: `docs/superpowers/plans/2026-06-10-plan-batch-calling.md`
- `apps/api/src/usan_api/`: `repositories/calls.py` (all mutators + `cancel_queued_tips` 554–574), `repositories/follow_up_flags.py`, `repositories/callback_requests.py`, `repositories/admin_audit.py`, `repositories/call_batches.py` (histogram 151, drained-complete 330), `routers/tools.py` (flag/callback/end_call commits), `routers/calls.py` (set_status sites 79–94, outcome 220–240), `routers/webhooks.py` (room_finished 55), `schedule_orchestrator.py` (phase 6 573–595, run_poller 597–616), `sms_outbox.py`, `telnyx_messaging.py`, `main.py` (lifespan 61–87), `settings.py` (incl. `_scheduler_requires_gate` 157–168), `ratelimit.py` (31–50), `observability/custom_metrics.py`, `schemas/call.py` (`parse_origin` 50–70), `db/models.py` (`SmsMessage` 394–421, batch models 424–490, `AdminAuditLog` 334 — no docstring today)
- `apps/api/migrations/versions/0013_ops_queue_status_workflow.py` (header format); `apps/api/pyproject.toml` (ruff select, mypy strict)
- `apps/api/tests/`: `conftest.py` (TRUNCATE 104–110, `client` finally cache-clear 152, `counter_value`/`gauge_value`, `service_token`), `test_sms_outbox.py` (factory-patch pattern), `test_settings_scheduler.py`, `test_infra_scheduler_env.py`, `test_lifespan_poller.py` (autouse `_clear_settings`), `test_ops_queue_migration.py` (helper shapes), `test_livekit_dispatch.py` (`_seed` defaults DIALING)
- `infra/`: `.env.example`, `.env.prod.example`, `grafana/provisioning/alerting/usan_alerts.yml`; `scripts/tests/test_alerting_provisioning.py`
