# RetellAI Parity Phase 4b-3 — Unknown-Recipient Inbound SMS (auto-create + reply)

> Design spec. Status: **approved** (2026-06-30). Implementation ships **inert** behind a
> default-False flag; no `v*` tag is cut by this phase.

## 1. Goal

Close the last inbound-SMS parity gap. Today, an inbound SMS that arrives at a provisioned DID
and matches **no open chat** is a silent `200`-noop — it is logged by `message_id` only and
nothing is created, even though the destination number's `inbound_sms_agents` binding is
persisted. This phase makes that case **number-driven**, matching RetellAI: resolve the
destination DID's inbound binding, **auto-create** an `sms_chat`, and run **one** agent reply
turn.

Scope is deliberately bounded to **single-org** (the seeded default org). The cross-org
DID→org routing surface is **out of scope** (see §10).

## 2. Parity framing

RetellAI's inbound model is number-driven, not create-driven (oracle
`apps/api/tests/compat/oracle/openapi-final.yaml`):

- A phone number carries `inbound_sms_agents` (array of `AgentWeight`, weighted-random pick per
  inbound SMS) with a scalar `inbound_sms_agent_id` fallback (`:6252-6261`, `:540-560`).
- An inbound SMS to that number auto-creates a `Chat` (`chat_type=sms_chat`, `chat_status=ongoing`)
  bound to the selected agent, then runs the reply. No prior `create-sms-chat` call is required.
- The produced `ChatResponse` is **number-agnostic** — required `{chat_id, agent_id, chat_status}`,
  **no** `from_number`/`to_number`/`contact`/`customer` fields (`:2408-2534`). The phone identity
  stays internal.

Where the oracle is **silent** (we do not over-fit): the no-binding default behavior, the
weighted-selection determinism/tie-breaking, the `inbound_sms_webhook_url` body schema, whether
`chat_started` fires on an auto-created inbound chat, and the entire cross-org routing rule.
There are **no** captured live oracle fixtures for inbound auto-create — only the OpenAPI schema.

## 3. Locked decisions (from brainstorming, 2026-06-30)

1. **Org scope = single-org auto-create.** Keep org = the seeded default org; build org-resolution
   as one injectable seam so true cross-org can drop in later. **No** `SECURITY DEFINER`, no new RLS
   surface, no new migration. (Rejected: full cross-org routing now — highest RLS-isolation risk,
   oracle-silent, no live 2nd-org inbound consumer yet.)
2. **No Contact row** for unknown senders. The auto-created `sms_chat` links by phone string only
   (`chat_sessions` has no `contact_id` FK; the oracle has no contact object). Avoids materializing a
   per-org-unique `contacts` row for every spam / wrong-number / autoresponder sender. (Rejected:
   lazy-upsert a Contact.)
3. **First-entry deterministic agent pick.** Select `inbound_sms_agents[0]` (fallback to the
   `inbound_sms_agent_id` scalar), isolated behind a small picker function so weighted-random can
   drop in later. Mirrors the established `outbound_sms_agents[0]` pattern; deterministic and
   testable. (Rejected: full weighted-random — nondeterministic, and identical to first-entry in the
   single-agent common case.)
4. **No greeting** is seeded — an inbound-initiated session opens with the real user message.
5. **Channel-lenient** — like `create_sms_chat`, do not enforce `channel='chat'` strictness on the
   auto-create path (consistent with the intentional 4c-1 asymmetry).
6. **New independent ship-inert flag** `telnyx_inbound_sms_autocreate_enabled` (default False),
   separate from `telnyx_inbound_sms_reply_enabled`, for independent staging/rollback.

## 4. Current-state facts (grounding)

- **Webhook entrypoint:** `apps/api/src/usan_api/routers/webhooks.py:104-148` — `POST /webhooks/telnyx`.
  Order: Ed25519 verify (401/400) → `parse_inbound_sms` → opt-out/STOP (`is_opt_out_keyword` → DNC,
  `outcome="opt_out"`, 200) → `handle_inbound_sms_reply` (owns the message if it returns `True`) →
  `_route_inbound_family_task` → `WEBHOOKS_TOTAL(type="telnyx_sms", outcome=...)` → 200.
- **Unknown-recipient drop point:** `_route_inbound_family_task` (`webhooks.py:177-194`) calls
  `find_contacts_by_phone(sender)`; when there are no contacts it logs
  `"Inbound SMS from unmatched number; no task created (FR-014)"` (binds only `message_id`) and
  returns. This is where unknown senders currently die.
- **Reply engine (4b-2):** `apps/api/src/usan_api/compat/sms_reply.py:28-89` —
  `handle_inbound_sms_reply`: flag guard (`:34`); `find_open_sms_chat` (`:38`);
  `if session is None: return False` (`:39-40`); dedup via `IntegrityError`→rollback (`:63-66`).
  Outcomes: `sms_reply` / `sms_reply_dedup` / `sms_reply_unconfigured` / `sms_reply_failed`.
- **Open-chat matcher:** `apps/api/src/usan_api/repositories/chats.py:56-75` — `find_open_sms_chat`:
  `chat_type='sms_chat' AND from_number=our_DID AND to_number=sender AND status=ONGOING AND
  archived_at IS NULL`, `FOR UPDATE`, newest-started wins. RLS scopes to the default org.
- **Agent resolution:** `apps/api/src/usan_api/compat/chat_service.py:83-98` — `_resolve_sms_agent`:
  override wins, else `outbound_sms_agents[0]` via `get_by_e164` within the caller org.
  `inbound_sms_agents` is currently read by **no** code.
- **Reply generation (reusable as-is):** `chat_service.py:194-246` — `generate_agent_reply(db,
  settings, session)`: loads config from `session.agent_profile_id`, builds full message history,
  runs one text-only Vertex turn.
- **Auto-create template:** `chat_service.py:101-162` — `create_sms_chat`: gate → from==provisioned →
  resolve agent → render greeting (`role='agent'`) → `add_session(from=our DID, to=recipient)` →
  flush → send → commit; any exception → rollback + 502.
- **Data model:** `apps/api/src/usan_api/db/models.py:1278-1349` — `chat_sessions` (no `contact_id`;
  `from_number`/`to_number` nullable; `agent_profile_id` NOT NULL) + `chat_messages` (`role`
  free-form Text: `'agent'`/`'user'`/`'sms'`; partial-unique
  `uq_chat_messages_provider_msg` on `(organization_id, provider_message_id)`).
- **DID binding:** `apps/api/src/usan_api/db/models.py:1088-1120` — `PhoneNumber` (TenantScoped,
  FORCE-RLS); JSONB `AgentWeight` lists `inbound_agents`/`outbound_agents`/`inbound_sms_agents`/
  `outbound_sms_agents`. `repositories/phone_numbers.py:79-82` — `get_by_e164` (no org predicate;
  RLS-scoped).
- **Org context:** `apps/api/src/usan_api/db/session.py:83-99` — `get_db` →
  `resolve_default_org_id` → `set_tenant_context`. Webhook org is **always** the seeded default org.
- **Conventions:** ship-inert flag pattern `settings.py:157-161`
  (`telnyx_inbound_sms_reply_enabled`); PHI masking `masking.py:10-13` (`mask_phone`); surface
  coverage `tests/compat/test_surface_coverage.py:26` requires `KNOWN_GAPS = frozenset()`.

## 5. Architecture & data flow

One new step is inserted into the existing webhook, between the reply engine and the family-task
fall-through. The existing order — and STOP-first precedence — is preserved:

```
Ed25519 verify → parse_inbound_sms → opt-out/STOP (DNC, 200)        [unchanged: STOP always first]
handled = handle_inbound_sms_reply(db, settings, inbound)           [4b-2: matches an OPEN sms_chat]
if not handled:
    handled = handle_inbound_autocreate(db, settings, inbound)      [4b-3: NEW]
if not handled:
    _route_inbound_family_task(db, inbound)                         [unchanged: caregivers + log]
→ WEBHOOKS_TOTAL(type="telnyx_sms", outcome=...) → 200
```

`handle_inbound_autocreate` returns `True` when it **owns** the message — bound-DID traffic it has
taken responsibility for (created + replied, unconfigured-skip, dedup, or a caught terminal failure).
It returns `False` **only** when a gate declines (flag off, no binding, or a known family contact), so
the family-task fall-through still runs. This mirrors the 4b-2 reply engine's ownership contract
exactly (`sms_reply.py:31-33`).

## 6. The new unit — `compat/inbound_autocreate.py`

`handle_inbound_autocreate(db, settings, inbound) -> bool` — gated, best-effort, single-transaction:

- **Gate 0 — flag.** `telnyx_inbound_sms_autocreate_enabled` off → `False` (true no-op; no DB work).
- **Gate 1 — binding.** `phone_numbers.get_by_e164(inbound.to_number)` (RLS → default org); select the
  inbound agent via `_pick_inbound_sms_agent(number)` (first entry of `inbound_sms_agents`, else the
  `inbound_sms_agent_id` scalar). No provisioned number / no binding → `False`.
- **Gate 2 — family-contact guard.** If `find_contacts_by_phone(inbound.from_number)` matches a known
  family contact → `False` (do not hijack the caregiver relay; `_route_inbound_family_task` handles
  it).
- **Gate 3 — send-ready.** If `not _sms_send_ready(settings)` or `not settings.gcp_project` → own the
  message but skip (no session is created): outcome `sms_autocreate_unconfigured`, return `True`.
  Mirrors `sms_reply.py:44-49` — a bound DID is SMS-agent territory, so it is never relayed to
  family-task even when a reply cannot be generated, and no orphan session is written.
- **Create + reply, one transaction.** Resolve the picked agent to its `agent_profile_id` (reuse a
  parameterized `_resolve_sms_agent` reading `inbound_sms_agents`) →
  `add_session(chat_type='sms_chat', from_number=our DID, to_number=sender, agent_profile_id=...)` →
  persist the inbound message `role='sms'` (`chats_repo.next_seq` + `add_message`) with
  `provider_message_id` → `flush` (the **dedup point**; `IntegrityError` → rollback → outcome
  `sms_autocreate_dedup`, return `True`) → `generate_agent_reply(...)` (one Vertex turn, persisted
  `role='agent'`) → `telnyx_messaging.send_sms(...)` → **commit OUTSIDE the send `try`** (so a
  commit-failure is not mislabeled as a send-failure and cannot double-send on retry, per
  `sms_reply.py:85-87`). Any exception in the generate/persist/send block → rollback → outcome
  `sms_autocreate_failed`, return `True`. On success: outcome `sms_autocreate`, return `True`.

Reused helpers (all already imported by `sms_reply.py`): `_sms_send_ready`, `generate_agent_reply`
(`compat/chat_service.py`); `telnyx_messaging.send_sms`; `to_e164` (`usan_api.phone`);
`chats_repo.next_seq` / `add_message` / the session-create helper. The session-create + first-turn
path is factored to share `create_sms_chat`'s session/message plumbing rather than duplicating it.

`_pick_inbound_sms_agent` is a small pure function (list-first-entry + scalar fallback) so
weighted-random can replace it later without touching the wiring. Agent resolution mirrors
`_resolve_sms_agent`/`create_sms_chat` but reads the **inbound** binding list — the existing resolver
is parameterized (`binding="inbound"|"outbound"`) rather than forked.

## 7. Idempotency

The auto-created chat is oriented exactly like an outbound-originated one
(`from=our DID, to=sender, status=ONGOING`). Two consequences give correctness for free:

- **Subsequent turns** from the same sender match `find_open_sms_chat` and are handled by the 4b-2
  **reply engine** — auto-create never fires twice for a conversation. Auto-create only ever handles
  the **first** message of a new inbound conversation.
- **Duplicate / concurrent delivery of the same message** is serialized by the existing
  `uq_chat_messages_provider_msg (organization_id, provider_message_id)` partial-unique index.
  Because the inbound `role='sms'` row is persisted in the **same transaction** as the session, the
  loser of a concurrent race takes an `IntegrityError` and its **whole** transaction (session +
  message) rolls back → outcome `sms_autocreate_dedup`, return `True` (idempotent; mirrors
  `sms_reply.py:63-66`). A Telnyx redelivery after commit instead matches the now-open chat and is
  dedup'd by the **reply** engine.

No session-level advisory lock and **no new migration** are required.

## 8. Error handling & PHI

- **Never raises out of the webhook.** Always returns `200`; Telnyx idempotency comes from the
  unique index, not from non-2xx retries. Unexpected failures (Vertex down, Telnyx send error) are
  caught → outcome `sms_autocreate_failed`, return `True` (owns the message; avoids retry storms),
  mirroring the 4b-2 reply-engine failure semantics.
- **Outcomes** (new `WEBHOOKS_TOTAL(type="telnyx_sms", outcome=...)` labels): `sms_autocreate`,
  `sms_autocreate_dedup`, `sms_autocreate_unconfigured`, `sms_autocreate_failed`.
- **PHI-safe logging only.** Log `message_id` + `type(exc).__name__` + `mask_phone(...)`. Never log
  the inbound text, the agent reply, the resolved `agent_id`, or any raw E.164.

## 9. Testing

TDD per task; the 4b-2 reply tests are the structural template. New
`tests/compat/test_inbound_autocreate.py` covers:

- flag-off → no-op (no DB writes, family-task still runs);
- bound DID + unknown sender → exactly one `sms_chat`, message sequence `sms` → `agent`, reply sent;
- DID with no `inbound_sms_agents` binding → no-op, family-task runs;
- known family contact + bound DID → declined (Gate 2), no chat created, family-task path taken;
- bound DID but send-not-configured (messaging/Vertex off) → owned + skipped (Gate 3), no session
  created, outcome `sms_autocreate_unconfigured`, family-task **not** run;
- dedup: same `provider_message_id` delivered twice → exactly one session, idempotent;
- second **distinct** inbound from the same sender → matched by the reply engine, not auto-create;
- `_pick_inbound_sms_agent` first-entry and scalar-fallback unit cases;
- PHI-safe logs (no inbound text; phone masked).

Plus `tests/test_settings.py` (flag default False) and webhook-wiring tests (insertion order,
STOP-still-first, outcome labels). Tests run on the non-superuser `app_session` fixture and tolerate
sibling rows under `pytest -n auto`.

## 10. Out of scope

- **Cross-org DID→org routing** and any `SECURITY DEFINER` lookup — deferred to a future phase when a
  2nd org has live inbound SMS. Org stays the seeded default org.
- **Contact auto-create** — decided against (chats are number-linked; oracle has no contact object).
- **Weighted-random agent selection** — first-entry only; weighted is a follow-up behind the picker
  seam.
- **`inbound_sms_webhook_url` per-event host override** — the oracle defines no body schema;
  building it now is speculative.
- **`chat_started` webhook emission** on auto-created inbound chats — oracle-unconfirmed, no delivery
  infra in scope.
- **Voice** inbound unknown-recipient handling — SMS only.
- **Global DID-uniqueness invariant** — moot under single-org.

## 11. Files touched

- **Create:** `apps/api/src/usan_api/compat/inbound_autocreate.py`;
  `apps/api/tests/compat/test_inbound_autocreate.py`.
- **Modify:** `apps/api/src/usan_api/settings.py` (one inert flag);
  `apps/api/src/usan_api/routers/webhooks.py` (3-line insertion + outcome labels);
  `apps/api/src/usan_api/compat/chat_service.py` (parameterize `_resolve_sms_agent` for inbound);
  `apps/api/tests/test_settings.py`; webhook-wiring tests.
- **Docs:** `docs/deployment/inbound-sms-autocreate.md` (operator activation note).

## 12. Invariants

- Single alembic head stays **0047** (no migration).
- **No new served operation** → `KNOWN_GAPS = frozenset()` stays empty; surface-coverage test green.
  The auto-created chat surfaces through the existing get-chat / list-chats.
- Ships **inert** — flag default False; no `v*` tag cut by this phase.
- `organization_id` is server-set by RLS (default org); app code never sets it.
- `services/agent` is untouched; no cross-service import.
- PHI-safe logging on the new path; `mask_phone` for any phone, never log message bodies.
- `exclude_none` convention preserved (this path adds no new HTTP response body).

## 13. Activation (operator, post-merge, future)

Inert until **all** of: `TELNYX_INBOUND_SMS_AUTOCREATE_ENABLED=true` (in **both** the compose `api`
`environment:` map **and** the VM `.env` — the compose-env-passthrough two-place rule), a provisioned
DID carrying an `inbound_sms_agents` (or `inbound_sms_agent_id`) binding, and the existing Vertex /
`GCP_PROJECT` reply path. Activation order and the secret-durability gotcha follow the 4b-2 reply
deployment note.
