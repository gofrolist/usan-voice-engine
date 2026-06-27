# RetellAI Parity — Phase 4b-2: Inbound Two-Way SMS Reply Engine (Design)

**Status:** proposed
**Date:** 2026-06-27
**Program:** RetellAI full-API-parity (any RetellAI client repoints base URL with zero changes)
**Predecessor:** Phase 4b-1 (`create-sms-chat`) — MERGED squash `4d2587c` (#142). 4b decomposed → 4b-1 (outbound create) + 4b-2 (this: inbound two-way reply engine).

---

## 1. Goal

Make `sms_chat` sessions **conversational**: when the recipient of an `sms_chat` texts back, an agent reply is generated (Vertex, BAA-contained) and sent back via Telnyx, and both the inbound text and the reply are appended to the existing chat session — so the already-served `get-chat` / `v3/list-chats` / transcript reflect the two-way conversation with **no new served operation**.

## 2. Background — what already exists (extend-not-add)

The inbound Telnyx SMS path is **already built**:

- **Route:** `POST /webhooks/telnyx` → `telnyx_webhook` in `apps/api/src/usan_api/routers/webhooks.py`. Mounted via `app.include_router(webhooks.router)` (`main.py`), prefix `/webhooks`, rate-limit-exempt. **Not** the compat sub-app (that surface is RetellAI-key-gated).
- **Signature:** `telnyx_inbound.verify_telnyx_webhook` — Ed25519 over `f"{timestamp}|" + raw_body`, headers `telnyx-signature-ed25519` + `telnyx-timestamp`, key `settings.telnyx_inbound_public_key` (SecretStr, default None). **Fails closed** when unset. Forged sig + stale replay → **401**; bad JSON → **400**; non-`message.received` / unmatched → **200** no-op.
- **Parse:** `parse_inbound_sms` (`schemas/inbound_sms.py`) → `InboundSms(message_id, from_number, text, event_type)`. `payload.from` is an **object** (E.164 at `.phone_number`). `payload.to` is **not currently consumed**.
- **Existing routing (unchanged):** opt-out keywords (STOP/UNSUBSCRIBE/…) → DNC; then family-task intake for known family contacts. Both always return 200.

Phase 4b-1 stored, on each `sms_chat` `ChatSession` row: `from_number` = our provisioned sender (= `settings.telnyx_from_number`), `to_number` = the recipient, `chat_type = "sms_chat"`, `status = ONGOING`. The greeting was persisted as a `role="agent"` `ChatMessage`.

## 3. Scope

**In scope (4b-2):**
- Extend `parse_inbound_sms` to also capture `payload.to` (our number) for session matching.
- A reply-engine branch in `telnyx_webhook`: match the open `sms_chat`, generate a Vertex reply, persist, send back.
- Per-inbound-message **dedup** (migration `0044`: `provider_message_id` on `chat_messages` + partial unique).
- Oracle-faithful inbound role `"sms"`.
- Dedicated inert flag `telnyx_inbound_sms_reply_enabled` (default `False`).

**Out of scope (deferred):**
- **Auto-creating** an inbound session for an *unknown* recipient (no open `sms_chat`). Inbound→agent bindings are stored-but-not-honored; only `outbound_sms_agents[0]` resolves today. An inbound-binding resolution path is a future phase. No match → fall through to the **existing** family-task intake, unchanged.
- Cross-org / multi-tenant inbound routing (runtime stays single-tenant / default-org).
- Tool calls in the reply turn (`tools=[]`, text-only — matches `create_chat_completion`).

## 4. Architecture & flow

New branch in `telnyx_webhook`, ordered **after** signature-verify + opt-out, **before** family-task:

```
verify signature (401/400) -> parse_inbound_sms (None -> 200 no-op)
  +- opt-out keyword?  -- yes -> _route_inbound_opt_out -> 200
  +- handle_inbound_sms_reply(db, settings, inbound) == True -- yes -> 200   (engine owns the message)
  +- _route_inbound_family_task -> 200                                       (no open sms_chat / engine off)
```

`handle_inbound_sms_reply` returns:
- **`False`** — the engine does **not** own this message → caller falls through to family-task. Cases: flag off; no `to_number`; no open `sms_chat` matches.
- **`True`** — the engine owns the message (matched an open `sms_chat`) → caller returns 200 and does **not** run family-task. Cases: reply sent; dedup-hit; matched-but-unconfigured; reply failed.

Rationale: a sender mid-`sms_chat` is a chat participant, **not** a family contact relaying a task — once a session matches, the message must never also create a family task, even if we cannot reply.

**Opt-out precedence:** a `STOP` mid-conversation is honored as an opt-out (DNC), never as a chat reply — opt-out is checked first (unchanged).

## 5. Components

### Create

**`apps/api/src/usan_api/compat/sms_reply.py`** — the reply engine.

```python
async def handle_inbound_sms_reply(
    db: AsyncSession, settings: Settings, inbound: InboundSms
) -> bool:
    """Match an open sms_chat for this inbound and drive one agent reply turn.
    Returns True iff the engine owns the message (matched a session), else False.
    PHI/secret-safe: logs only message_id + type(exc).__name__."""
```

Flow:
1. `if not settings.telnyx_inbound_sms_reply_enabled: return False` (engine off → today's behavior).
2. `if not inbound.to_number: return False`.
3. Normalize: `our = to_e164(inbound.to_number) or inbound.to_number`; `recipient = to_e164(inbound.from_number) or inbound.from_number` (mirrors the opt-out/family-task E.164 normalization).
4. `session = await chats_repo.find_open_sms_chat(db, our_number=our, recipient=recipient)` (FOR UPDATE). `if session is None: return False`.
5. **Matched — we own the message.** If `not _sms_send_ready(settings) or not settings.gcp_project`: log + `WEBHOOKS_TOTAL(type="telnyx_sms", outcome="sms_reply_unconfigured")`, `return True` (drop the reply; never family-task a chat participant).
6. **Persist inbound (dedup):** `seq = next_seq`; `add_message(role="sms", content=inbound.text, provider_message_id=inbound.message_id)`; `flush()`. On `IntegrityError` (duplicate provider id): `rollback`, `outcome="sms_reply_dedup"`, `return True`.
7. **Reply turn:** in a `try`: `reply = await chat_service.generate_agent_reply(db, settings, session)`; persist `role="agent"` reply; `flush()`; `telnyx_messaging.send_sms(settings, to_number=recipient, body=reply)`. On any `Exception`: `rollback` (whole txn — no orphan inbound/reply), log type-name, `outcome="sms_reply_failed"`, `return True`.
8. `await db.commit()` (**outside** the send try — wrapping it would mislabel a commit-fail as a send-fail → double-send risk); `outcome="sms_reply"`; `return True`.

### Modify

**`apps/api/src/usan_api/compat/chat_service.py`** — extract the shared Vertex turn so the reply engine and `create_chat_completion` do not duplicate it:

```python
async def generate_agent_reply(
    db: AsyncSession, settings: Settings, session: ChatSession
) -> str:
    """Load the published config, build the system prompt + multi-turn contents from the
    FULL message history, run ONE text-only Vertex turn, return the reply text. The caller
    must have already persisted+flushed the latest user/sms turn so it appears in history.
    Raises on Vertex failure (caller owns rollback)."""
    cfg = await _load_published_config(db, session.agent_profile_id)
    bare_vars, _ = unpack_dynamic_vars(session.dynamic_vars)
    values = build_vars({}, bare_vars, timezone="", now=datetime.now(UTC))
    system_instruction = substitute(cfg.prompts.system_prompt, values)
    history = await chats_repo.list_messages(db, session.id)
    contents = [
        {"role": "model" if m.role == "agent" else "user", "parts": [{"text": m.content}]}
        for m in history
    ]
    turn = await run_vertex_turn(
        model=cfg.llm.model, temperature=cfg.llm.temperature,
        system_instruction=system_instruction, tools=[], contents=contents, settings=settings,
    )
    return turn.text
```

`create_chat_completion` steps 5–7 become `turn_text = await generate_agent_reply(db, settings, session)` (behavior identical; the `role=="agent" else "user"` map already maps `"sms"` → `"user"`). The 4a tests must still pass.

**`apps/api/src/usan_api/repositories/chats.py`**:
- `add_message(..., provider_message_id: str | None = None)` → pass to `ChatMessage(...)` (additive; api_chat callers unaffected).
- New `find_open_sms_chat(db, *, our_number: str, recipient: str) -> ChatSession | None`:
  ```python
  stmt = (
      select(ChatSession)
      .where(
          ChatSession.chat_type == "sms_chat",
          ChatSession.from_number == our_number,
          ChatSession.to_number == recipient,
          ChatSession.status == ChatStatus.ONGOING,
          ChatSession.archived_at.is_(None),
      )
      .order_by(ChatSession.started_at.desc(), ChatSession.id.desc())  # multiple-open -> newest
      .limit(1)
      .with_for_update()
  )
  return (await db.execute(stmt)).scalars().first()
  ```
  RLS scopes to the default org automatically (no `organization_id` predicate). FOR UPDATE serializes concurrent inbound turns for the same conversation.

**`apps/api/src/usan_api/db/models.py`**:
- `ChatMessage`: add `provider_message_id: Mapped[str | None] = mapped_column(Text)`, and to `__table_args__` a partial-unique index `Index("uq_chat_messages_provider_msg", "organization_id", "provider_message_id", unique=True, postgresql_where=text("provider_message_id IS NOT NULL"))`.
- `ChatSession`: add a matcher index to `__table_args__` `Index("ix_chat_sessions_sms_match", "from_number", "to_number", postgresql_where=text("chat_type = 'sms_chat'"))`.

**`apps/api/src/usan_api/schemas/inbound_sms.py`**: add `to_number: str = ""` to `InboundSms`; in `parse_inbound_sms`, read `inner.get("to")` (a **list** of objects) → first element's `phone_number` (defensive; default `""`). Existing opt-out/family-task paths ignore the new field.

**`apps/api/src/usan_api/routers/webhooks.py`**: insert the `handle_inbound_sms_reply` branch (per §4). Preserve the exact 401/400/200 contract and exactly-once `WEBHOOKS_TOTAL` counting (the engine increments its own metric only when it returns `True`; the family-task path keeps its `outcome="ok"`).

**`apps/api/src/usan_api/settings.py`**: add `telnyx_inbound_sms_reply_enabled: bool = False`.

### Migration `0044` (`apps/api/migrations/versions/0044_chat_provider_message_id.py`)

`revision="0044"`, `down_revision="0043"`. `upgrade()`:
- `op.add_column("chat_messages", sa.Column("provider_message_id", sa.Text(), nullable=True))`
- `op.create_index("uq_chat_messages_provider_msg", "chat_messages", ["organization_id", "provider_message_id"], unique=True, postgresql_where=sa.text("provider_message_id IS NOT NULL"))`
- `op.create_index("ix_chat_sessions_sms_match", "chat_sessions", ["from_number", "to_number"], postgresql_where=sa.text("chat_type = 'sms_chat'"))`

`downgrade()` drops both indexes then the column. TenantScoped/FORCE-RLS convention (0040/0042/0043). Applied as the `usan` OWNER by the deploy's owner-migration step (per the migrations-need-owner fix #124) — no manual step.

## 6. Data flow — number mapping

When the recipient replies, Telnyx delivers `from = recipient`, `to = [our number]`. Our stored row has `from_number = our number`, `to_number = recipient`. Hence the matcher pairs **inbound `to` → session `from_number`** and **inbound `from` → session `to_number`**. Both sides normalized to E.164.

## 7. Dedup semantics

`provider_message_id = inbound.message_id` (Telnyx message id) on the inbound `"sms"` row; partial unique on `(organization_id, provider_message_id)`. A Telnyx redelivery of an already-committed message → `IntegrityError` on flush → dedup-hit (no second reply). The FOR UPDATE session lock + the unique index together serialize concurrent redeliveries. On the **ack-200/rollback** failure path nothing is committed, so a re-text (new message id) retries cleanly.

## 8. Error / retry semantics (decided)

The webhook **always returns 200** for a signature-valid, parseable `message.received` (matching the existing contract); only forged/stale signature → 401 and bad JSON → 400 (unchanged). On any reply-engine failure (Vertex / send / DB), the **whole transaction is rolled back** (no orphan PHI) and the outcome is recorded via `WEBHOOKS_TOTAL` + a type-name-only log. No 5xx → no Telnyx retry storm and no double-reply; a lost reply surfaces on the metric and the recipient can re-text.

## 9. Inert flag & deployment

Gated by the new **`telnyx_inbound_sms_reply_enabled`** (default `False`) so the auto-responder stages/rolls-back independently of inbound parsing, opt-out, and family-task. A live reply additionally requires `telnyx_messaging_enabled` + the three Telnyx messaging secrets (send) **and** `gcp_project` (Vertex) **and** `telnyx_inbound_public_key` (inbound signature). Merged ≠ deployed: inert until a `v*` tag deploys migration `0044` and the operator enables the flags/secrets. **No `v*` tag in this phase.**

## 10. PHI / security constraints

- Logs carry only `message_id` + `type(exc).__name__` — never `from`/`to`, the inbound text, the reply body, or dynamic vars; re-raise with `from None` where applicable.
- `organization_id` is **server-set** via the RLS GUC; app code never sets it. Matcher reads + inserts are default-org-scoped by RLS.
- Vertex (`vertexai=True`, ADC) keeps conversation PHI inside the BAA boundary; no Gemini Developer API.
- Signature verification is unchanged and still fails closed.
- `apps/api` and `services/agent` do not import each other (the reply engine stays within `apps/api`/`compat`).

## 11. Testing

- **Unit:** `parse_inbound_sms` captures `to_number` from the `to` list (and `""` when absent); `InboundSms.to_number` default. `find_open_sms_chat` matches the right ongoing `sms_chat`, ignores ended/archived/wrong-number/`api_chat`, picks newest on multiple-open, FOR UPDATE. `add_message` persists `provider_message_id`; the partial unique rejects a duplicate `(org, provider_message_id)`.
- **Behavioral (signed webhook, reusing the existing Telnyx-signing test helper):** flag-off fall-through; matched+configured → 200, inbound `role="sms"` + reply `role="agent"` persisted, `send_sms` called once with `recipient` + reply body; dedup (same message id twice → one reply); no-session fall-through to family-task; matched-but-unconfigured → 200, no send, no inbound persisted; send-failure → 200, whole-txn rollback (no inbound/reply persisted); opt-out STILL wins mid-chat.
- **Conformance / freeze:** a session carrying an `sms` inbound + `agent` reply still conforms to the oracle `ChatResponse` and SDK round-trips (`role:"sms"` is a valid plain string); `get-chat` transcript + `message_with_tool_calls` reflect both turns.
- **Regression:** the existing webhook contract tests (signature 401, replay 401, bad JSON 400, non-message 200, opt-out, family-task) must remain green; 4a `create_chat_completion` tests must remain green after the `generate_agent_reply` extraction.

## 12. Global constraints (for the plan)

- Migration `0044`, `down_revision="0043"`; additive, RLS-scoped, owner-applied on deploy.
- Inbound role is exactly `"sms"`; reply role is `"agent"`.
- `handle_inbound_sms_reply` returns `True` for every matched-session case (reply/dedup/unconfigured/failed), `False` only for flag-off / no-`to_number` / no-match.
- `db.commit()` is **outside** the send `try`.
- Webhook always 200 except the pre-existing 401 (signature/replay) / 400 (bad JSON).
- PHI-safe logging (message_id + type-name only); `organization_id` never set by app code.
- No `v*` tag; ends at squash-merge to `main`.

## 13. Open / deferred follow-ups

- **4b-3 (future):** inbound session auto-create from an inbound→agent binding for an unknown recipient (requires a new inbound-binding resolution path); cross-org inbound routing.
- The transcript string renders the inbound line as `"Sms: …"` (the existing `_line` capitalizes the role); the structured `message_with_tool_calls[].role` is the hard contract and is `"sms"`. Acceptable for v1.
