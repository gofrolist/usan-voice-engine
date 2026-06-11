# Phase A4 — Small Unlocks: per-call profile override, custom variables, per-profile policy (design)

**Date:** 2026-06-10
**Status:** Approved (brainstorming) — pending implementation plan
**Branch:** `feat/small-unlocks`, stacked on the open PR chain #55 → #56 → #57 (see §9; alembic head on this stack is `0014`, `origin/main` is at `0011`; this phase ships `0015`)
**Predecessors:** Admin-UI Phase 2 (variables, PR #53 / `ce1961b`), Phase 3 (tool catalog, PR #54 / `ef2fa81`), A1 batch/scheduled calling (**open PR #55, not merged**), A2 calls console (open PR #56), A3 webhooks (open PR #57)
**Related specs:** `2026-06-08-admin-ui-phase2-variable-substitution-design.md`, `2026-06-09-admin-ui-phase3-tools-design.md`, `2026-06-10-batch-scheduled-calling-design.md`

---

## 1. Goals

Three independent, individually small unlocks. This is deliberately a cleanup phase — each feature finishes a seam the codebase already half-built:

- **A. `profile_override` on ad-hoc enqueue** — `POST /v1/calls` gains the same optional `profile_override` that schedules and batches got in A1 (open PR #55). The `Call.profile_override` column (migration 0010, on `main`), retry-child inheritance, materialization propagation, and runtime resolution precedence already exist on the stack; only the ad-hoc request schema, its idempotency contract, and the response echo are missing.
- **B. Custom variable definitions** — a `custom_variables` table + admin CRUD, merged into `GET /v1/admin/variable-catalog` with `tier="custom"` (the `Literal` already includes it). Definitions are **documentation/UX, not values**: values continue to arrive per call via `dynamic_vars`. The editor palette, unknown-token warnings, and PHI warnings pick them up automatically through the existing catalog fetch.
- **C. Per-profile policy overrides** — `AgentConfig` gains an optional `policy` section: quiet-hours narrowing (within the statutory 09:00–21:00, never wider) and bounded retry overrides (per-status max attempts + one delay multiplier). Enforced entirely API-side by generalizing the existing pure functions; the agent is untouched.

## 2. Non-goals (this phase)

- Per-elder variable **value** storage or any change to variable value resolution (values stay in `dynamic_vars`).
- A full retry-ladder editor (arbitrary per-rung delays). Only max-attempts + one multiplier.
- Statutory quiet-hours **widening** — never, in any phase. 09:00–21:00 local is the TCPA outer bound.
- Variable rename (custom variable `name` is immutable after create; delete + recreate instead).
- Making the zod PHI check dynamic (see §6.3 — server stays authoritative; client gets a non-blocking notice).
- **Quiet-hours gating of ad-hoc immediate dials.** `POST /v1/calls` dials through `dialer.schedule_dial → _dial_and_classify` (`livekit_dispatch.py:225`), which has **no quiet-hours gate at all** — statutory included. That statutory gap is pre-existing and tracked since A1 (the `feat/batch-calling` tip carries "track ad-hoc /v1/calls gap"). A4 does not close it; policy quiet hours therefore first apply to an ad-hoc call's **retries** (which go through `schedule_retry` + the poller). Whoever closes the statutory gap must thread the resolved policy through the same seam (Open question 5).
- Workspace scoping, per-status delay multipliers, agent-side policy awareness.
- `profile_override` on schedules/batches — already implemented in A1 (**open PR #55**: `schemas/schedule.py:66/:97`, `schemas/batch.py:64/:78`, validation in both routers, target-over-batch precedence in `schedule_orchestrator.py:508-510`). A4 does not touch those paths. Caveat: because #55 is unmerged, review changes there can move these line refs; re-verify at implementation time.

## 3. Architecture

### 3.1 Feature A — `profile_override` on `POST /v1/calls`

`CreateCallRequest` (`apps/api/src/usan_api/schemas/call.py`) gains:

```python
profile_override: uuid.UUID | None = None
```

- **Ordering (load-bearing):** the idempotency replay pre-check (`routers/calls.py:123`) runs **first**; liveness validation runs only on the create path, on both the dispatch and DNC branches. An identical replay whose override profile was archived after the original enqueue must return **200 with the original call**, never 422 — that is the retry-on-timeout contract idempotency keys exist for. This matches the schedules pattern, where liveness is validated on create/update only.
- **Validation (create path only):** `enqueue_call` calls the existing `is_live_profile(db, profile_id)` (`repositories/agent_profiles.py:326`) and 422s when the profile is not ACTIVE + published — same wrapper shape as `_require_live_override` (`routers/schedules.py:69-75`), same error text as `_OVERRIDE_ERROR` (`routers/batches.py:49`). Note the auth tier differs: `enqueue_call` is operator-token scope (`require_operator_token`), not admin-session; the validation logic is identical and grants no new authority (see §7).
- **Idempotency contract:** `_idempotent_replay` (`routers/calls.py:35-42`) extends its payload-match check:

  ```python
  if (existing.elder_id != body.elder_id
          or existing.dynamic_vars != body.dynamic_vars
          or existing.profile_override != body.profile_override):
      raise HTTPException(409, "idempotency_key reused with a different payload")
  ```

  Without this, a replay with a different override silently returns the old call. The helper has exactly **two** consumers — the pre-check (`calls.py:123`) and the IntegrityError race fallback (`calls.py:70`) — both get this for free. The DNC path is covered via the shared pre-check, not by calling the helper itself.
- **Persistence:** `calls_repo.create_call` (`repositories/calls.py:56-81`) gains a `profile_override: uuid.UUID | None = None` kwarg; both call sites in `enqueue_call` (dispatch path and DNC path) pass it.
- **Response echo:** `CallResponse` (`schemas/call.py:93`) gains `profile_override: uuid.UUID | None`, populated in `from_model`. Without it the override is invisible to the operator system and to day-2 triage. (Surfacing it in the A2 calls console UI is deferred — Open question 6.)
- **Everything downstream already exists** (on the PR stack): retry children inherit the override (`calls.py:307`), materialized roots carry it (`:418/:435`), and runtime resolution honors it (`runtime.py:38-53` → `resolve_agent_config` precedence walk: override → elder profile → direction default). Note: `tools.py:244` is **send_sms** passing `profile_override=call.profile_override` into its config resolution — it is *not* a schedule_callback propagation path. `schedule_callback` (`tools.py:143-166`) writes a `callback_requests` row (human triage queue, `admin_tools.py`) that never auto-materializes a call; if a callback-to-call path ever ships, override inheritance there is an unhandled gap (Open question 7).
- **Agent:** zero changes.

### 3.2 Feature B — custom variable definitions

**Storage decision: a `custom_variables` table** (migration 0015, §4), not definitions embedded in the `AgentConfig` document. Rationale: definitions are a **global catalog** (like the built-ins: "a GLOBAL constant, NOT a per-version snapshot" — `variable_catalog.py` module docstring), shared across all profiles, and managed by a dedicated CRUD page. Embedding per-profile copies would fork the catalog per version snapshot and contradict Phase 2's model. The cost is that pydantic validators can't see the table; §3.2.1 places the custom-PHI check at the router layer where DB access exists.

- **Definition shape:** `name` (snake-case slug, unique, immutable), `description`, `example`, `phi` (bool, creator-chosen), timestamps. The pydantic create-validator rejects names colliding with the 10 frozen `BUILTIN_NAMES` (authority stays in code; the DB enforces only slug shape + uniqueness). A new `CustomVariable` model is added to `db/models.py`.
- **Catalog merge:** `GET /v1/admin/variable-catalog` (`routers/admin_variable_catalog.py`) goes from static constant to DB-backed (the route gains a `db: AsyncSession = Depends(get_db)` dependency it does not have today): `list(BUILTIN_VARIABLES)` + customs mapped to `VariableSpec(tier="custom", default="", ...)`, builtins first in canonical order, customs alphabetical. `default` is always `""` for customs — **definitions carry no values**; values are supplied per call via `dynamic_vars` exactly as today.
- **Builtin shadowing guard:** collision is rejected at create time, but a *future* builtin added to `BUILTIN_VARIABLES` can collide with a pre-existing custom row. The catalog merge **drops** customs whose name is in `BUILTIN_NAMES` and logs a warning ("custom variable {name} shadowed by builtin; ignored"), so the merged catalog never contains duplicate names. Pinned by a test.
- **Unknown-token warnings (server):** the save path (`routers/admin_profiles.py:109-137`) fetches custom names once and passes them as the already-existing-but-unused second parameter: `unknown_tokens(text, known_names=custom_names)` (`schemas/agent_config.py:49`). Declared customs stop warning as "unknown"; undeclared tokens keep warning. Warn-don't-block, via the established `warnings` response channel.
- **Sensitive-field PHI warnings (server parity):** `phi_tokens_in_sensitive_fields` (`schemas/agent_config.py:117-135`) is generalized exactly like `unknown_tokens`: a keyword `phi_names` parameter defaulting to `PHI_BUILTIN_NAMES`; the save path passes builtins ∪ custom `phi=true` names. Without this the authoritative server `warnings` channel stays silent on custom PHI in `voicemail_message`/`greeting` while the catalog-driven client warns — and prompts are the one channel with **no** fail-closed defense (the agent substitutes `dynamic_vars` into all prompt fields), so the warning is the defense. Warn-don't-block, matching builtin-PHI-in-sensitive-fields behavior.
- **Agent:** zero changes. The agent's `prompt_vars.py` substitutes `dynamic_vars` over builtin defaults already; a declared-or-undeclared custom token behaves identically (value present in `dynamic_vars` → substituted; absent → existing fallback). No new hand-mirror.

#### 3.2.1 Custom variables in SMS bodies — block and warn placement

Two distinct facts drive this design. First, the Phase 3 hard block (`_reject_phi_in_templates`, inside the `SmsToolConfig`/`ToolsConfig` validators) only knows `PHI_BUILTIN_NAMES`. Second — verified — `render_sms_body` (`sms_render.py`) builds its substitution map from **builtins-minus-PHI + clock vars only**; `dynamic_vars` (the only channel carrying custom values) never enters it, so **every custom token in an SMS body renders `""`**, PHI or not.

1. **PHI customs — authoritative 422 at save, publish, AND rollback.** One shared helper (`custom_phi_sms_violations(config_dict, phi_names) -> list[Violation]`) scans SMS template bodies for `phi=true` custom names; the `admin_profiles` **save**, **publish**, and **rollback** handlers all run it (each has DB access for the phi-name fetch). Rollback matters: `POST /{profile_id}/rollback/{version}` (`routers/admin_profiles.py:188-213`) re-publishes an old snapshot via `repo.rollback → repo.publish` with no pydantic re-entry — without the helper there, an old snapshot referencing a now-`phi=true` custom republishes cleanly, bypassing the "authoritative" gate. Clone-from copies only a draft (no publish), so the next save/publish catches it — acceptable, stated here deliberately. The pydantic validators keep blocking the 5 builtins exactly as today.
   **422 `loc` contract (load-bearing):** violations are fabricated with the exact path `["body", "config", "tools", "sms", "templates", <i>, "body"]`. This is *more* granular than the existing pydantic builtin block (whose `model_validator` loc collapses to `("body","config","tools")`) — for customs the server 422 is the PRIMARY enforcement (the client only shows a notice, §6.3), so field-level loc quality is load-bearing. The client-side mapping fix this requires is in §6.3.
2. **Non-PHI customs — warn, don't block.** Any non-PHI custom (or undeclared) token in an SMS body gets a save-path warning on the existing `warnings` channel: *"`{{name}}` is not substituted in SMS — it will render as empty text."* Hard-blocking only declared customs would be perverse (declare → blocked, leave undeclared → allowed); warn-don't-block matches the Phase 2 unknown-token tier. A matching non-blocking notice renders in the ToolsSection editor (§6.1).
3. **Send time (already fail-closed, verified):** the `render_sms_body` invariant above is the send-time defense; A4 adds a regression test pinning it (custom token + value present in `dynamic_vars` → renders `""`) rather than threading a DB fetch into the render path. Consequence: flipping a variable to `phi=true` after a template referenced it is safe immediately (renders empty), and the next save/publish/rollback 422s.
4. **Client:** static zod `PHI_TOKEN_NAMES` stays frozen on the 5 builtins (never drifts). Custom-PHI-in-SMS surfaces as a **non-blocking notice** computed from the fetched catalog (the `phiTokens.ts` pattern); the client-pass/server-422 case degrades via `mapServerErrors` — which needs the §6.3 fix to land the error on the right field.

### 3.3 Feature C — per-profile policy overrides

#### 3.3.1 Config shape

New section in `schemas/agent_config.py`, added to `AgentConfig` as `policy: PolicyConfig | None = None` — optional-with-default per the forward-compat invariant (`agent_config.py:271-276`): every published snapshot and older draft keeps validating.

```python
class RetryMaxAttempts(BaseModel):
    model_config = ConfigDict(frozen=True)
    no_answer: int | None = Field(default=None, ge=0, le=4)        # builtin equivalent: 2
    voicemail_left: int | None = Field(default=None, ge=0, le=4)   # builtin: 1
    busy: int | None = Field(default=None, ge=0, le=4)             # builtin: 1
    failed: int | None = Field(default=None, ge=0, le=4)           # builtin: 1

class PolicyConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    quiet_hours_start_local: str | None = None   # "HH:MM", must be >= "09:00"
    quiet_hours_end_local: str | None = None     # "HH:MM", must be <= "21:00"
    retry_delay_multiplier: float | None = Field(default=None, ge=0.5, le=4.0)
    retry_max_attempts: RetryMaxAttempts | None = None
```

- **Time fields are `str`, full stop** — `"HH:MM"` validated by format regex + narrowing rules, parsed to `datetime.time` *only inside the validator and at consumption*. They are never stored as `datetime.time` on the model: the save path persists `body.config.model_dump()` (python mode, `admin_profiles.py:103`) into JSONB, where a `time` object raises `TypeError` — and even `mode="json"` would round-trip as `"09:30:00"`, which the zod `HH:MM` mirror would reject on `form.reset(profile.draft_config)`. A save → read → `form.reset` round-trip test pins this (§8).
- Each quiet-hours field may be set independently (the unset side stays statutory). A `model_validator` enforces **narrowing only** — effective start ≥ 09:00, effective end ≤ 21:00, effective start < effective end. Minute granularity is supported.
- **Retry semantics — defined against the chain-global attempt counter, because that is what the code keys on.** `next_retry_delay(status, attempt)` receives `parent.attempt`, the chain-global attempt number, and status can change across a chain (no_answer → busy → no_answer). Therefore: `retry_max_attempts.<status> = N` means *"a call ending with this status schedules a retry iff its chain-global attempt number ≤ N."* The builtins already have this semantic (builtin `busy: 1` means "retry a busy only when it was the chain's first attempt", not "at most one busy retry ever"); the builtin equivalents in the field comments above are expressed in it. `0` disables retries for that status. There are **no per-status counters** — an override `busy: 3` lets a busy outcome at attempt 3 retry even if attempts 1–2 were no_answer. Mixed-status chains are first-class test cases (§8).
- Attempts above the built-in ladder length reuse the final rung's delay (index clamp). `retry_delay_multiplier` scales every rung delay uniformly (0.5–4.0). This is the entire retry surface — no ladder editor.
- **Chain-length invariant (load-bearing).** Today three places hardcode "ladders end by attempt 3": `_MAX_CHAIN_HOPS = 3` (`repositories/calls.py:610`, powering `get_chain_tip` and therefore `cancel_queued_tips`), and the 3-hop root walk in `schedule_retry` (`calls.py:285`, powering the batch-cancellation guard). With `le=4`, a chain can reach root + 4 children; a depth-4 tip would escape `get_chain_tip` (batch cancel flips nothing; the QUEUED tip still dials). *(Corrected from an earlier overstatement, matching the plan + the code comment at the walk site:)* the `schedule_retry` root walk is **not** broken at `le=4` — the deepest parent that can still schedule a retry is attempt 4, only 3 hops from its root, which `range(3)` already reaches; deriving that bound is drift-prevention consistency, not a bug fix. The load-bearing escapes are `get_chain_tip`/`cancel_queued_tips` (and the `webhook_events` origin root walk, which loses origin attribution at policy max depth). Fix: `retry_policy.py` gains a single-source constant `MAX_CHAIN_ATTEMPTS = 5` (root + 4 retries, the `le=4` ceiling); `_MAX_CHAIN_HOPS` (in both `repositories/calls.py` and `webhook_events.py`) and the `schedule_retry` root-walk bound are **derived** from it (`MAX_CHAIN_ATTEMPTS - 1`). A test pins `walk bound >= max policy chain depth` so the next person who raises `le=` cannot reintroduce the escape.
- The ladders restructure as data (`{status: [delay, ...]}`) so `next_retry_delay` can clamp/scale without duplicating the if-chain.
- **Agent:** untouched. Policy is consumed exclusively server-side; the agent's `AgentConfig` mirror tolerates the riding-along `policy` key via pydantic's default `extra="ignore"` (`services/agent/src/usan_agent/agent_config.py:95` sets only `frozen=True`) — this load-bearing default gets its own agent-side regression test (§8) so a future `extra="forbid"` cleanup can't break runtime config fetch.

#### 3.3.2 Enforcement seam — re-resolve at consumption time (explicit decision)

Policy is **re-resolved at every consumption site, never snapshotted onto the `Call`**. Quiet hours are a TCPA compliance control: a snapshot taken at enqueue can sit queued for hours (3 h voicemail rung, overnight clamps) and a compliance-tightening publish must take effect immediately. This extends the codebase's own dial-time-truth principle ("a clamp is a promise about the past — never dial on a stale one", `livekit_dispatch.py:353-357`). Cost: 1–3 indexed lookups + one ≤24 KB `model_validate` per retry-schedule/dial — negligible at eldercare volumes; a cache is a later add if ever needed.

Implementation:

1. **Generalize the pure functions with keyword defaults (zero-diff for existing callers and tests):**

   ```python
   # quiet_hours.py
   def next_allowed(dt_utc, tz_name, *, start_local: time = time(9, 0),
                    end_local: time = time(21, 0)) -> datetime: ...

   # retry_policy.py — ladders as data
   def next_retry_delay(status, attempt, *, max_attempts: int | None = None,
                        delay_multiplier: float = 1.0) -> timedelta | None: ...
   ```

   Unknown-timezone behavior is unchanged: `ValueError`, callers fail CLOSED.
2. **New repo helper** `resolve_call_policy(db, *, profile_override, elder_profile_id, direction) -> ResolvedPolicy` in `repositories/agent_profiles.py` — a thin wrapper over the existing `resolve_agent_config` precedence walk (override → elder → direction default). `ResolvedPolicy` carries parsed `start_local`/`end_local` (`datetime.time`), `delay_multiplier`, and the per-status max-attempts map, with statutory/builtin defaults filled in.
   **Precedence is whole-profile, not per-field merge (explicit):** the policy comes from the *same* profile `resolve_agent_config` picks; if that profile resolves but has `policy=None`, the result is **statutory defaults** — even when the elder's assigned profile narrows. Consequence: attaching a `profile_override` whose config lacks `policy` loosens effective quiet hours back to statutory relative to the elder's profile. This stays within the statutory bound (no TCPA exposure) and is consistent with how every other config section resolves; a precedence test pins it (§8).
3. **Wire at the four existing consumption sites** — each already has the `Call` and `Elder` (all resolution inputs) in hand; **no signature changes**:
   - `repositories/calls.py` `schedule_retry` (`:266` delay, `:273` quiet-hours clamp) — resolves inside.
   - `livekit_dispatch.py:360` — the dial-moment quiet-hours re-check in `dispatch_and_dial` (poller-claimed dials: retries + materialized calls). This is what makes a tightened window effective for already-queued calls; it re-queues via the existing `requeue_for_quiet_hours`. **Explicit caveat:** ad-hoc immediate dials (`_dial_and_classify`) bypass this entirely — see the §2 non-goal.
   - `schedule_orchestrator.py:347` (**daily-schedule occurrence root** clamp, `sched:{id}:{date}` key) and `:483` (batch target) materialization clamps — with the window composition rules of §3.3.3.

Documented caveat: a **tightened** window publishes instantly for poller-dialed calls (dial-time re-check); a **loosened** one (back toward statutory) only affects future clamps — already-clamped `scheduled_at` values stay put. Fail-closed direction; acceptable.

#### 3.3.3 Policy × schedule/batch window composition (explicit)

Schedule and batch windows are validated against the **statutory** bounds at save time, so today the materialization clamps are no-ops. A narrowed policy breaks that invariant, and naive ordering produces window violations: with policy start 11:00 and a batch window 09:00–10:00, `next_allowed(now)` → `schedule_windows.next_run_at(...)` (which re-intersects with the *statutory* constants, `schedule_windows.py:22-27,58-65`) pushes the dial back inside the policy-forbidden zone; the dial-moment re-check then requeues to 11:00 — **outside the operator's window, a day late**. Separately, at `:347` the clamp runs *after* the `now >= end_utc` skip check, so a policy clamp can push `scheduled_at` past the schedule's window end (late call instead of `skipped_window`). Rules:

1. **Thread policy bounds into the window math.** `schedule_windows` (`_effective_window` / `next_run_at`) gets the same keyword-default generalization as `next_allowed` (statutory defaults, policy bounds passed at the two orchestrator sites). The effective dialing interval is `window ∩ [policy_start, policy_end)` — computed in one place, not by sequential clamps fighting each other.
2. **Empty intersection ⇒ skip observably.** When `window ∩ policy = ∅` (for the occurrence/target's day), the occurrence/target is marked with the existing `skipped_window` outcome — never scheduled outside the window, never silently dropped. The generalized helper returns `None` for the policy-induced empty case rather than raising; the existing "window never intersects quiet hours" `ValueError` remains reserved for statutory misconfiguration caught at save time.
3. **Clamp-before-skip at `:347`.** The occurrence root computes the policy-aware effective time first, then evaluates the skip condition against it — a clamp past the window end becomes `skipped_window`, not a late dial.

Both composition cases (window-before-policy-start, clamp-past-window-end) are in the §8 wiring matrix. A publish-time warning when a profile's narrowing conflicts with windows of schedules/batches that reference it is deferred (Open question 4) — it needs a cross-entity join the save path doesn't do today.

## 4. Data model — migration `0015_custom_variables.py`

Only Feature B needs DDL. Feature A's column shipped in 0010; Feature C lives inside the `agent_profile_versions.config` JSONB document. `revision="0015"`, `down_revision="0014"`, raw-SQL style per 0014. A matching `CustomVariable` SQLAlchemy model is added to `db/models.py`.

```sql
-- Operator-declared prompt variables (catalog tier "custom"). Definitions are
-- documentation/UX only — values arrive per call via Call.dynamic_vars. name is
-- immutable after create (rename would silently orphan {{tokens}} in templates).
-- Collision with the 10 frozen builtin names is enforced in the Pydantic layer.
CREATE TABLE custom_variables (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    example TEXT NOT NULL DEFAULT '',
    phi BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_custom_variables_name_slug CHECK (name ~ '^[a-z][a-z0-9_]{0,63}$')
);
```

`downgrade()`: `DROP TABLE IF EXISTS custom_variables`.

Deleting a custom variable performs a hard delete with **no referential check against profile configs** — tokens referencing it simply revert to "unknown" warnings (warn-don't-block tier, consistent with Phase 2).

## 5. API surface

| Method & path | Auth | Change |
|---|---|---|
| `POST /v1/calls` | operator token | `CreateCallRequest.profile_override?` — idempotency replay pre-check FIRST (identical replay returns 200 even if the profile was archived since); create path 422 if not live (via `is_live_profile`); included in idempotency payload-match (409 on mismatch); echoed via `CallResponse.profile_override` (`from_model`) |
| `GET /v1/admin/variable-catalog` | admin session | now DB-backed (route gains a `db` dependency): builtins + customs (`tier="custom"`, `default=""`); builtin-shadowed customs dropped + logged |
| `GET /v1/admin/custom-variables` | admin session | list, alphabetical |
| `POST /v1/admin/custom-variables` | admin session + **ADMIN role** | create; 409 duplicate name (repo domain exception); 422 on slug/builtin-collision; audited |
| `PATCH /v1/admin/custom-variables/{id}` | admin session + **ADMIN role** | edit `description` / `example` / `phi` only (`name` immutable); 404 unknown; audited with changed-field detail incl. `{"phi": {"old": …, "new": …}}` |
| `DELETE /v1/admin/custom-variables/{id}` | admin session + **ADMIN role** | hard delete; 404 unknown; audited |
| `PUT/POST /v1/admin/agent-profiles/*` save / publish / **rollback** | admin session | new 422 from the shared helper: `phi=true` custom name in an SMS template body, loc `["body","config","tools","sms","templates",<i>,"body"]`; `warnings` now excludes declared customs from unknown-token output, adds non-PHI-custom-in-SMS "renders empty" warnings, and includes custom-PHI sensitive-field warnings |
| `GET /v1/runtime/agent-config` | agent | unchanged shape; `policy` rides along in the document and is ignored by the agent (`extra="ignore"`) |

The CRUD router copies the `admin_users.py` precedent exactly: router-level `Depends(require_admin_session)`, write-gating `Depends(require_admin_role(AdminRole.ADMIN))`, `get_actor_email` actor, `admin_audit.record(...)` **before** the single `db.commit()` (actions `custom_variable.create|update|delete`), private `_to_out` mapper, registration in `main.py` alongside the other admin routers.

**`phi` flips are allowed in both directions, silently but audited.** `false→true`: existing ACTIVE templates referencing the name keep rendering `""` (send-time invariant — fail-closed immediately) and the next save/publish/rollback 422s; no proactive affected-profiles scan in the PATCH response (documented as accepted; the audit row's old/new detail reconstructs *when* the variable became PHI). `true→false`: unblocks SMS save and removes warnings; same audit trail.

## 6. UI design (admin-ui)

### 6.1 Custom Variables page (Feature B)

- New route `/custom-variables` under the **Config** nav group (`NavSidebar.tsx` GROUPS), pattern-copied from the Admin Users page: table (name, description, example, PHI badge), create/edit/delete dialogs, mutations visible to ADMIN role only.
- Help text in the create dialog: *"Names, descriptions, and examples are operator configuration — never put PHI in them. Mark a variable PHI if its per-call value will contain health information; PHI variables are blocked in SMS templates."*
- react-query mutations invalidate **both** `["custom-variables"]` and the variable-catalog key — `useVariableCatalog()`'s 5-min `staleTime` assumed a constant catalog; invalidation makes the palette/warnings refresh immediately after CRUD. Update the stale comment at `variableCatalog.ts:23-24`.
- Palette (`VariablePalette.tsx` — the "Custom" tier group already renders), unknown-token warnings (`unknownTokens.ts`), and prompt PHI warnings (`phiTokens.ts`) need **zero changes** — they consume the fetched catalog.
- SMS templates editor (`ToolsSection`): **two** catalog-driven non-blocking notices — (a) a body referencing a `phi=true` custom variable ("blocked at save"), (b) a body referencing *any* custom variable ("custom variables are not substituted in SMS — renders empty", mirroring the §3.2.1 server warning). The static zod builtin-PHI superRefine is untouched.

### 6.2 Policy section (Feature C)

- `fieldMeta.ts`: add `"policy"` to `SectionKey` + `SECTION_LABELS`; `fieldMeta` entries keyed `"policy.quiet_hours_start_local"` etc. Help text for `retry_max_attempts` documents the chain-global semantics and the final-rung-repeat behavior.
- `ProfileEditorPage.tsx`: append to `SECTION_ORDER` and the conditional render block; optional rail summary via targeted `form.watch`.
- `PolicySection.tsx`: modeled on `TimingSection.tsx` (`Field` + controls); two time inputs + multiplier + four bounded number inputs. **Unset state:** every control shows the effective default as placeholder (09:00 / 21:00 / ×1.0 / builtin attempts), never as a value; `<input type="time">` yields `""` (not `null`) when cleared, so the zod schema applies an empty-string→`null` transform — otherwise pristine forms fail validation.
- `agentConfigSchema.ts`: `policySchema` mirroring `PolicyConfig` 1:1 — `HH:MM` regex (`/^([01]\d|2[0-3]):[0-5]\d$/`), same numeric bounds, narrowing + start<end via `.superRefine` with `path`, and **`.optional().nullable()`-shaped like `toolsSchema.sms`** so `form.reset(profile.draft_config)` accepts older drafts lacking the key. Object paths identical to pydantic field names (server 422 `loc` mapping depends on it).

### 6.3 Server-error mapping fix (load-bearing for §3.2.1)

`mapServerErrors` (`ProfileEditorPage.tsx:94-108`) currently filters loc segments **by value**: `item.loc.filter((p) => p !== "body" && p !== "config")`. For the custom-PHI loc `["body","config","tools","sms","templates",0,"body"]` this eats the trailing field name `body` and maps the error to `tools.sms.templates.0` (the row) instead of the body input. Fix: slice the leading envelope **positionally** (`item.loc.slice(2)` after asserting the `["body","config"]` prefix) and join. Additionally, the **publish and rollback** mutation paths must route 422s through `mapServerErrors` too (today only save does), since both can now return field-loc'd custom-PHI errors. A vitest asserts a fabricated server 422 renders an error on `tools.sms.templates.0.body` inside `ToolsSection`.

## 7. Security

- **No new PHI surfaces.** Custom-variable definitions are operator config, PHI-free by convention (UI help text warns; names are slug-constrained at the DB level). Values keep flowing through the existing `dynamic_vars` channel with its existing 8 KB cap and handling.
- **Custom-PHI SMS block:** save/publish/**rollback**-time 422 via one shared helper (server-authoritative, §3.2.1) + the pinned send-time invariant that custom values never enter the SMS substitution map. The PHI-flip-after-publish edge renders `""`, fail-closed. Custom-PHI in sensitive prompt fields gets server-side warnings at parity with builtins (`phi_names` generalization) — the warning is the only defense on the prompt channel, by the same warn-don't-block decision Phase 2 made for builtins.
- **`profile_override` at operator-token tier:** bounded blast radius — the override can only select among profiles an admin already published as ACTIVE (`is_live_profile`); it grants no new config authority.
- **Quiet hours:** narrowing-only is enforced by pydantic bounds server-side (zod is a mirror, not the gate); statutory widening is structurally impossible — `PolicyConfig` bounds re-validate on every snapshot read (`_resolved_from_profile` falls through to defaults on `ValidationError`, `agent_profiles.py:284-296`). Unknown-timezone behavior stays fail-CLOSED. **Known scope limit:** policy (like statutory) quiet hours do not gate ad-hoc immediate dials (§2 non-goal); they bind retries, poller dials, and materialized calls.
- **Dial-moment re-check scope (deliberate accept):** the dial-moment re-check (`livekit_dispatch.py`, `dispatch_and_dial`) re-enforces the **statutory + policy quiet hours only** — operator batch/schedule windows are **materialization-time-only** and are not re-intersected at dial time. Consequence: a policy tightening published *after* a batch/schedule call materialized can requeue it to a time outside the operator's (non-statutory) dial window. Per the A1 precedent (the window is a materialization throttle, not a dial cap — documented, not oversold): the compliance bounds (statutory ∩ policy) are never breached; the operator window is a shaping preference, accepted as such. See Open question 9.
- **Retry bounds:** the `MAX_CHAIN_ATTEMPTS` single-source ceiling keeps batch cancellation and chain-walk guards sound at any allowed policy value (§3.3.1).
- **Audit & abuse:** all custom-variable mutations audited via `admin_audit.record`, with old/new detail on `phi` transitions; policy changes are audited through the existing profile save/publish audit. Existing `/v1/admin/*` rate limiting covers the new routes (`ratelimit.py:42`).
- Error messages carry variable **names** and field paths only — never `dynamic_vars` values.

## 8. Testing strategy (TDD — tests first per task)

- **Feature A:** schema test (`profile_override` optional/UUID); 422 on non-live profile (draft, archived, unpublished); **ordering test: enqueue with override → archive the profile → identical replay → 200** (replay pre-check beats liveness); idempotency replay 200 on identical payload incl. override, 409 on differing override (`None`→set, set→set, and set→None, at both helper consumers — the pre-check via HTTP, the **IntegrityError race fallback directly** via the established flaky-pre-check seam from `test_calls.py`, not just indirectly through the shared helper unit test); DNC path persists override; `create_call` kwarg threading; `CallResponse.profile_override` echo; runtime resolution end-to-end (enqueue with override → `/v1/runtime/agent-config` returns the override's published config).
- **Feature B:** CRUD API tests mirroring `test_admin_users_api.py` (happy paths, 404, 401 no session, 403 non-ADMIN mutation, 409 duplicate, 422 bad slug / builtin collision / name-change attempt); audit-row assertions incl. `phi` old/new detail; catalog merge test extending `test_variable_catalog_api.py` (order, `tier="custom"`, `default=""`, **builtin-shadowed custom dropped + logged**); save-path tests — declared custom no longer in `warnings`, undeclared still warns, non-PHI custom in SMS body → "renders empty" warning, `phi=true` custom in SMS body → 422 with loc `["body","config","tools","sms","templates",<i>,"body"]`, custom-PHI in `voicemail_message` → sensitive-field warning; **rollback test: snapshot referencing a now-`phi=true` custom → 422 from the shared helper**; **regression test pinning the send-time invariant** (custom token in body renders `""` even with the value present in `dynamic_vars`).
- **Feature C:** pure-function parametrized tests — `next_allowed` with narrowed windows incl. minute granularity, DST boundary, defaults unchanged; `next_retry_delay` with multiplier, truncation (`max_attempts=0`), extension (final-rung repeat), **mixed-status chains** (no_answer→busy→no_answer, per the chain-global semantics of §3.3.1), defaults reproduce the exact v1 ladder; **invariant test: `_MAX_CHAIN_HOPS` and the `schedule_retry` walk bound derive from `MAX_CHAIN_ATTEMPTS` and cover the deepest policy-allowed chain** (incl. an end-to-end batch-cancel test at max depth: tip is cancelled, root walk finds the `batch:` root); `PolicyConfig` validator tests (widening rejected, start≥end rejected, one-sided narrowing OK, bad `HH:MM` rejected); **JSONB round-trip test: save policy → read draft → values are `"HH:MM"` strings a zod mirror accepts**; forward-compat test (legacy config without `policy` validates — extend `test_legacy_config_still_deserializes`); `resolve_call_policy` precedence walk + statutory fallback + **whole-profile precedence pin (override-without-policy over elder-with-policy → statutory)**; wiring tests at all four sites (schedule_retry delay+clamp, dispatch dial-time re-queue under a narrowed window, both orchestrator clamps) **plus the §3.3.3 composition matrix: policy ∩ window = ∅ → `skipped_window` (batch target), clamp-past-window-end → `skipped_window` (schedule occurrence), window push never lands inside the policy-forbidden zone**; **agent-side regression test: config payload with an unknown `policy` key validates (`extra="ignore"` pinned)**.
- **admin-ui (vitest):** `policySchema` mirror bounds + superRefine paths + empty-string→null transform + older-draft `form.reset` without `policy`; PolicySection render, placeholders, error display; custom-variables page CRUD flows; palette shows fetched customs; both SMS custom notices (PHI + renders-empty); **`mapServerErrors` positional-slice test: fabricated 422 loc lands on `tools.sms.templates.0.body`**; publish/rollback 422 routing.
- 80%+ coverage; `ruff` + `mypy` on both Python packages locally before push (CI runs mypy — not in CLAUDE.md).

## 9. Rollout

- **No feature flags.** Each feature is inert until used: `profile_override` absent ⇒ today's behavior; empty `custom_variables` table ⇒ catalog byte-identical to the static constant; `policy` absent on every existing config ⇒ pure functions run with statutory/builtin defaults (keyword-default generalization is zero-diff). Policy enforcement does not warrant a flag: narrowing-only bounds plus the §3.3.3 skip-observably rule mean the worst misconfiguration is *fewer* calls dialing (visible as `skipped_window`), never a compliance breach.
- **Stacked-branch reality (read before opening the PR).** `origin/main` is at `ef2fa81` (#54) with migration head **0011**. Migrations 0012 (A1), 0013 (A2), 0014 (A3) exist only on the open PR stack: **#55 `feat/batch-calling` (base: main) ← #56 `feat/calls-ui` ← #57 `feat/outbound-webhooks`**; `feat/small-unlocks` starts at the #57 tip (`b7d2b8c`). Consequences:
  - A4's PR **cannot target `main`** until #55 → #56 → #57 land, in order; `down_revision="0014"` hard-couples A4's merge to #57.
  - Every predecessor squash-merges, so a plain `git rebase origin/main` would replay all predecessors' commits. Use the established recipe after **each** predecessor lands: `git rebase --onto origin/main <prev-plan-tip> feat/small-unlocks` (first predecessor tip to peel from: `b7d2b8c`; re-pin after each rebase).
  - The A1 `profile_override` plumbing this spec cites as existing (§2, §3.1) lives in **open PR #55** — review changes there can invalidate line refs and even behavior; re-verify the cited seams when each predecessor merges.
- Ship order within the branch: migration 0015 + `CustomVariable` model → Feature B API (CRUD, catalog merge + `db` dependency, save/publish/rollback PHI helper, warnings generalizations) → Feature A → Feature C API (config shape, pure-function generalization, `MAX_CHAIN_ATTEMPTS` derivation, `resolve_call_policy`, four wirings + window composition) → admin-ui (custom-variables page, PolicySection, `mapServerErrors` fix). Single PR (squash) per the established plan-PR workflow.
- Deploy: standard `v*` tag path; `alembic upgrade head` to 0015 runs via the existing migration step. No new env vars, no infra/terraform changes, no agent image changes.
- Docs: update the admin-ui README variable section to mention the custom tier, the SMS renders-empty caveat, and the Policy section.

## 10. Open questions

1. **Variable rename** — deferred (immutable name + delete/recreate). If operators ask, a rename would need a config-scan-and-warn pass; revisit with evidence.
2. **Retry extension warning** — should setting `max_attempts` above the built-in ladder length surface a save-time warning ("extra attempts reuse the final delay")? Default: no, document in `fieldMeta` help text.
3. **Catalog visibility for operator tokens** — `dynamic_vars` suppliers are external operator systems; do they eventually need a read-only catalog endpoint at operator-token scope? Out of scope now.
4. **Publish-time policy-vs-window conflict warning** — warn at profile publish when the narrowing empties the window intersection of schedules/batches referencing the profile. Needs a cross-entity join the save path doesn't do; the §3.3.3 `skipped_window` outcome keeps the failure observable meanwhile.
5. **Ad-hoc dial quiet-hours gate** — the statutory gap on `_dial_and_classify` is tracked since A1; when it's closed, thread the resolved policy through the same seam (the §3.3.2 helper is ready). Owner/timing TBD — must land before RetellAI cutover if ad-hoc calls are operator-batch-driven.
6. **Calls-console exposure of `profile_override`** — the A2 console (open PR #56) doesn't show it; the API echo ships now, the UI column/detail field is a follow-up once #56 merges.
7. **Callback-to-call materialization** — no such path exists today (callbacks are a human triage queue); if one ever ships, `profile_override` inheritance there is an explicitly unhandled gap.
8. **`resolve_call_policy` caching** — revisit only if dispatcher/retry volume ever makes the per-call lookups visible in metrics; not expected at eldercare volumes.
9. **Dial-moment re-check of operator windows** — the dial-moment re-check enforces statutory + policy quiet hours but not batch/schedule windows (§7 deliberate accept): a post-materialization policy tightening can requeue a call outside the operator's window. Re-intersecting the owning batch/schedule window at dial time would need an owner lookup the dial path doesn't do today (chain-root walk → batch/schedule join); revisit only if operators report off-window dials in practice.
