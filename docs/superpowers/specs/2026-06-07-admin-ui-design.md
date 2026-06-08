# Admin UI — Agent Configuration Console (Design)

**Date:** 2026-06-07
**Status:** Approved design; pending implementation plan
**Related:** `docs/superpowers/specs/2026-05-25-usan-voice-engine-design.md`

## 1. Overview

A self-hosted, RetellAI-style admin UI for configuring the USAN voice agent's
behavior — prompts, voice, model, conversation flow, timing, tools, and speech
tuning — without a code change or redeploy. Operators manage **multiple named
agent profiles** through a draft → publish workflow with version history and
rollback. Each phone call resolves and reads its **published** configuration at
call start.

This replaces the current reality where every "moving part" is either an
environment variable or a hardcoded constant read once at process startup.

### Goals

- Edit agent behavior (the "Retell core") in a web UI; changes take effect on the
  next call with no redeploy.
- Manage multiple named profiles (create / name / clone), assignable per elder,
  with per-direction defaults and an enqueue-time override.
- Draft → Publish with immutable version snapshots, diffs, and rollback.
- Google SSO for named, attributed operators; full audit trail.
- Preserve the hard `apps/api ⊥ services/agent` boundary — the agent never gets
  DB access.
- Never fail a call because of the config service: agents fall back to today's
  hardcoded constants if the config fetch fails.

### Non-goals (deferred / out of scope for v1)

- Operations console: call-history browser, transcript/recording viewer, live
  call monitoring (Grafana covers ops today).
- The daily-call **batch scheduler** — it does not exist in the repo and is
  external; building it is separate orchestration, not a frontend.
- Editing infrastructure secrets (DB URL, JWT keys, telephony creds) from the UI.
- Multi-tenant / org scoping (system is single-tenant).
- Per-**field** elder overrides (elders are assigned a whole profile, not field
  overrides).
- Live "test call" before publish (substituted by a publish-time diff).
- Full elder CRUD (v1 only **assigns** a profile to an existing elder).

## 2. Background — current configurable surface

All config today lives in one of three tiers, with **no runtime-mutable
application config store**:

- **Environment (Pydantic `Settings`)** — `apps/api/src/usan_api/settings.py` and
  `services/agent/src/usan_agent/settings.py`, both `@lru_cache` singletons.
  Changing a value requires a process restart (and the deploy does not refresh
  `.env`).
- **Hardcoded constants** — all prompts/scripts, LLM/STT model IDs, retry policy,
  quiet hours, voicemail window/phrases, and VAD/turn-detection live as module
  constants in `services/agent` (`pipeline.py`, `check_in.py`, `worker.py`,
  `voicemail.py`).
- **Per-row DB data** — `elders`, `dnc_list`, `calls.dynamic_vars` (domain data,
  not app config).

Notable findings that shape this design:

- `elders.preferred_voice` exists in the schema but is **not wired** into the
  pipeline.
- The agent's tool set is hardcoded in two places (`check_in.py` decorators +
  `tools=[...]`); a data-driven toggle requires a tool registry.
- Inbound vs outbound prompt handling is asymmetric (inbound personalizes via a
  template; outbound ignores `dynamic_vars`). This design unifies both through
  `config.prompts`.
- The only existing web UI is Grafana, served behind Caddy + a CIDR allowlist —
  the template this UI copies.
- There is **no user identity, session, or RBAC** today — only a static
  `OPERATOR_API_KEY`, a service JWT (agent→API), and a worker JWT.

## 3. Key decisions

| Decision | Choice |
|---|---|
| v1 scope | Agent behavior config ("Retell core"), all knob bundles incl. advanced speech tuning |
| Config model | Multiple named agent profiles (create / name / clone) |
| Apply & safety | Draft → Publish + immutable version history + rollback; calls read the active published version at start |
| Auth | Google SSO (GCP-native), allow-listed, behind Caddy + CIDR; named-user attribution |
| Profile binding | Per-elder assignment + per-direction defaults + enqueue override |
| Frontend | React SPA (Vite + TypeScript) |
| Agent ↔ config | Agent fetches resolved published config from `apps/api` at call start; falls back to hardcoded constants on failure (Approach 1) |

## 4. Architecture

Three deployables (two new) plus the existing Postgres:

```
                 ┌─────────────────────────────────────────────┐
   Operator ───▶ │  admin-ui (NEW)  React SPA (Vite+TS)         │
  (browser)      │  static assets served behind Caddy          │
                 └───────────────┬─────────────────────────────┘
                                 │ HTTPS, same-origin, Google-SSO session cookie
                                 ▼
                 ┌─────────────────────────────────────────────┐
                 │  apps/api (EXTENDED)  FastAPI                │
                 │   • /v1/admin/*   profile CRUD, draft,       │
                 │       publish, versions, rollback, audit     │
                 │   • /v1/auth/*    Google SSO + session        │
                 │   • /v1/runtime/agent-config (service JWT)   │
                 └───────────────┬─────────────────────────────┘
                                 │ SQLAlchemy / asyncpg
                                 ▼
                 ┌─────────────────────────────────────────────┐
                 │  Postgres  (new tables, Alembic 0010+)       │
                 └─────────────────────────────────────────────┘
                                 ▲
                                 │ GET /v1/runtime/agent-config?call_id=…
                 ┌───────────────┴─────────────────────────────┐
                 │  services/agent (REFACTORED)  LiveKit worker │
                 │   builds AgentSession/Agent from fetched     │
                 │   ResolvedConfig; falls back to constants    │
                 └─────────────────────────────────────────────┘
```

The agent keeps talking only to `apps/api` (reusing its existing httpx client and
service-JWT auth) — no DB credentials are added to the agent.

## 5. Data model

New Alembic migration (`0010_agent_profiles`, plus follow-ups as phases land).

### `agent_profiles`
The "agent" identity / working copy.

- `id` UUID PK
- `name` TEXT, unique, not null
- `description` TEXT, nullable
- `status` ENUM(`active`, `archived`), default `active`
- `draft_config` JSONB, not null — the working copy (validated by Pydantic)
- `published_version` INTEGER, nullable — the live version number, joined to `agent_profile_versions` on `(id, version)`; NULL = never published. (Implemented as an integer rather than an FK to the version row to avoid a circular FK; see the P1 plan.)
- `is_default_outbound` BOOL, default false
- `is_default_inbound` BOOL, default false
- `created_at`, `updated_at` TIMESTAMPTZ
- `created_by`, `updated_by` TEXT (operator email)

Constraints: partial unique indexes enforce **at most one** profile with
`is_default_outbound = true` and **at most one** with `is_default_inbound = true`.

### `agent_profile_versions`
Immutable published snapshots. **Calls read from here**, never from the draft.

- `id` UUID PK
- `profile_id` UUID FK → `agent_profiles(id)`, not null
- `version_number` INT, not null (per-profile, monotonically increasing)
- `config` JSONB, not null (frozen snapshot of the published draft)
- `published_at` TIMESTAMPTZ, not null
- `published_by` TEXT (operator email), not null
- `note` TEXT, nullable (changelog line)
- Unique `(profile_id, version_number)`

### `elders` (altered)
- add `agent_profile_id` UUID FK → `agent_profiles(id)`, nullable (assigned profile)

### `calls` (altered)
- add `profile_override` UUID FK → `agent_profiles(id)`, nullable — set at enqueue
  time (`POST /v1/calls`) to force a specific profile for that call; highest
  priority in outbound resolution (§6.2). Null means "resolve from the elder /
  default."

### `admin_users`
- `email` TEXT PK
- `role` ENUM(`admin`, `viewer`), default `admin` (all `admin` in v1)
- `added_by` TEXT, nullable
- `created_at` TIMESTAMPTZ
- Seeded from `ADMIN_BOOTSTRAP_EMAILS` env on first boot; manageable in the UI.

### `admin_audit_log`
Append-only.

- `id` UUID PK
- `actor_email` TEXT, not null
- `action` TEXT, not null (e.g. `profile.publish`, `profile.rollback`, `elder.assign_profile`)
- `entity_type` TEXT, `entity_id` TEXT
- `detail` JSONB (before/after summary, version numbers)
- `created_at` TIMESTAMPTZ

### The `config` JSONB document

Same shape in `draft_config` and each version's `config`. Validated at the API
boundary by a Pydantic model (`schemas/agent_config.py`); stored as JSONB because
it is read whole per call and will keep gaining knobs. Full field list in
[Appendix A](#appendix-a--config-document-schema).

## 6. Data flow

### 6.1 Authoring (operator → DB)

1. **Edit draft** — SPA `PUT /v1/admin/profiles/{id}/draft` writes `draft_config`
   (Pydantic-validated, attributed to the operator email). Draft edits never
   touch live calls.
2. **Publish** — `POST /v1/admin/profiles/{id}/publish` snapshots `draft_config`
   into a new `agent_profile_versions` row (`version_number = max + 1`), sets
   `published_version_id`, writes an audit entry, stores the optional `note`.
3. **Rollback** — `POST /v1/admin/profiles/{id}/rollback/{version}` copies that
   version's `config` back into `draft_config` and re-publishes it as a **new**
   version, keeping history append-only and linear.

Diffs (UI) compare two version `config` blobs (or draft vs current live).

### 6.2 Call-time resolution (agent → API, per call)

**Shipped (P2):** a single endpoint, guarded by the **worker JWT** (the resolved
config is profile-global, not per-elder PHI), always returns **200** with a usable
config:

```
GET /v1/runtime/agent-config?direction=<inbound|outbound>&call_id=<id>   (auth: worker token)
```

`direction` is required; `call_id` is optional. The `calls` row records direction,
the matched elder, and an optional `profile_override`. The API resolves server-side,
in priority order:

- **Outbound:** `call.profile_override` → matched `elder.agent_profile_id` →
  `is_default_outbound` profile.
- **Inbound:** matched `elder.agent_profile_id` → `is_default_inbound` profile.
- If the resolved profile has no published version → default profile's published
  version → no resolution → server-side `DEFAULT_AGENT_CONFIG` fallback.

The response is `ResolvedAgentConfig` = `{source, profile_id, version, config}`
(`source`/`profile_id`/`version` are for logging; `config` is the resolved
`AgentConfig`). A missing or unknown `call_id` is **not** an error — resolution
degrades to the `direction` default and ultimately to `DEFAULT_AGENT_CONFIG`,
so the agent always receives a complete config. The override-resolution reads
`Call.profile_override` and `Elder.agent_profile_id`; the **admin setters** that
populate those columns (per-elder / per-call profile assignment) are a later slice,
so the configurable lever today is the per-`direction` default profile.

### 6.3 Agent integration (refactor) — shipped (P2)

`pipeline.py` / `check_in.py` / `worker.py` build from an `AgentConfig` instead of
reading module constants. Every builder takes an optional `cfg: AgentConfig | None`
that defaults to the agent's local `DEFAULT_AGENT_CONFIG` — the single source of
default truth (the retained module constants are now thin aliases of it):

- `build_session(settings, cfg, userdata)` reads
  `cfg.voice / llm / stt / timing / speech_advanced` (incl. `answer_timeout`).
- `build_agent(cfg)` / `build_inbound_agent(cfg, dynamic_vars)` use
  `cfg.prompts.system_prompt` and register **only the tools in
  `cfg.tools.enabled`** via a tool registry (dict keyed by tool name) filtered at
  build time (`end_call` is force-included). This is how the tools toggle takes
  effect.
- All spoken text (greeting, disclosure, voicemail, goodbye, check-in flow,
  inbound opening + personalization template) comes from `cfg.prompts`.
- `worker.py` fetches the config **once per call**, after `ctx.connect()`, via
  `fetch_agent_config(settings, direction=..., call_id=...)`. On timeout / error /
  no resolution it logs a `WARNING` and falls back to the local
  `DEFAULT_AGENT_CONFIG`; a `max_call_duration` watchdog is armed from the resolved
  timing and cancelled on call end. A call is never failed because of the config
  service.

## 7. Prompt safety

Admin-authored prompts are operator input but still receive strict treatment, and
remain separate from untrusted elder-supplied data:

- **On write** (`PUT draft`): Pydantic validation — per-field length caps, type
  checks, and rejection of raw `{`/`}` in any field that is not an explicit
  personalization template (those braces break `str.format` and are the existing
  injection vector, cf. `_PROMPT_UNSAFE`).
- **Inbound personalization template:** only the whitelisted slots
  (`{elder_name}`, `{last_check_in_line}`) are allowed; the server rejects any
  other `{...}` slot and the UI shows the allowed list.
- **Elder-supplied dynamic vars:** the existing `_sanitize_prompt_value` /
  `_PROMPT_UNSAFE` handling in `check_in.py` is unchanged — that is untrusted PHI
  data, distinct from operator templates.
- Unifying both flows through `config.prompts` also resolves the current
  inbound/outbound prompt asymmetry.

## 8. Admin API surface

All under `/v1/admin`, session-authed; mutations are audit-logged. Mutations
require `admin` role; `viewer` is read-only.

| Method & path | Purpose |
|---|---|
| `GET /profiles` | list (name, status, defaults, live version, has-draft flag, assigned count) |
| `POST /profiles` | create (optional `clone_from`) |
| `GET /profiles/{id}` | draft_config + published version meta |
| `PUT /profiles/{id}/draft` | save draft (Pydantic-validated) |
| `POST /profiles/{id}/publish` | snapshot draft → new version, optional `note` |
| `GET /profiles/{id}/versions` | version history |
| `GET /profiles/{id}/versions/{v}` | one version's config (for diff) |
| `POST /profiles/{id}/rollback/{v}` | re-publish an old version |
| `POST /profiles/{id}/set-default` | set default inbound/outbound (clears prior holder) |
| `POST /profiles/{id}/archive` | archive (blocked if live default or assigned) |
| `GET /elders` | minimal list (name, masked phone, assigned profile) |
| `PUT /elders/{id}/profile` | assign a profile to an elder |
| `GET /voices` | proxy the Cartesia voice catalog for the picker |
| `GET /audit` | paged audit log |
| `GET/POST/DELETE /admin-users` | manage the Google allow-list (admin role) |

Runtime (service JWT, not session): `GET /v1/runtime/agent-config?call_id=…`.

Pydantic schemas live in `apps/api/src/usan_api/schemas/agent_config.py` and are
reused by both the write path and the runtime resolve path (single source of
truth for the config shape).

## 9. Auth — Google SSO

App-level SSO verified in `apps/api` (not IAP).

1. SPA initiates Google OAuth 2.0 **Authorization Code + PKCE**. New env:
   `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`.
2. `GET /v1/auth/login` → redirect to Google. `GET /v1/auth/callback` → API
   exchanges the code, verifies the ID token (signature, `aud`, `iss`,
   `hd`/email), and checks the email against `admin_users`. Unknown → 403,
   audit-logged.
3. On success the API issues its **own** short-lived session as an **HttpOnly,
   Secure, SameSite=Strict** cookie, signed with `JWT_SIGNING_KEY` (carries
   email + role + exp). The SPA never stores the Google token.
4. `POST /v1/auth/logout` clears the cookie. Dependency `require_admin_session`
   guards `/v1/admin/*`; `require_admin_role("admin")` gates mutations.

This is a fourth, cleanly separated auth mechanism alongside `OPERATOR_API_KEY`
(machine plane), the service JWT (agent plane), and the worker JWT.

## 10. Frontend

New top-level `apps/admin-ui/` (own `package.json`, lint, build — kept out of the
Python apps).

**Stack:** Vite + React + TypeScript; TanStack Query (server state) + React
Router; shadcn/ui + Tailwind; react-hook-form + Zod (client validation mirroring
the server Pydantic rules, kept in sync via the API's OpenAPI); Monaco for large
prompt fields (plain `<textarea>` is the lighter fallback).

**Auth in the SPA:** no token handling — relies on the HttpOnly session cookie;
on `401` redirects to `/v1/auth/login`. A `useSession()` hook reads
`GET /v1/auth/me` for the operator's email and viewer/admin gating.

**Screens:**

1. **Profiles list** — name, status, default badges, live version #, unpublished-
   draft indicator, assigned-elder count; New / Clone / Archive.
2. **Profile editor** (core) — left section nav matching the bundles
   (Prompts · Voice · LLM · Speech · Timing · Tools · Voicemail detection);
   right form per section.
   - Prompts: one Monaco field per prompt with helper text; personalization
     template shows allowed `{slots}` and validates inline.
   - Voice: Cartesia voice picker (from `GET /voices`) + speed/model/language;
     LLM model dropdown + temperature slider.
   - Speech (advanced): collapsed "Advanced — can degrade call quality" panel,
     each knob showing its default + a reset affordance.
   - Sticky header: draft status, **Save draft**, **Publish** (dialog with
     optional changelog note + a **diff of draft vs current live version**).
3. **Version history** (per profile) — list (number, who, when, note); select two
   to diff; **Rollback** with confirm.
4. **Elders / assignment** — minimal list (name, masked phone, assigned profile)
   with a per-elder profile dropdown. Only elder editing in v1.
5. **Defaults** — set default inbound/outbound profile.
6. **Audit log** — paged, filterable by actor/action/entity.
7. **Admin users** (admin role) — manage the Google allow-list.

**Safety affordances:** publish always shows a diff + confirm; advanced knobs are
collapsed with warnings + defaults; destructive actions confirm and surface
server-side guard errors clearly.

## 11. Infra & deployment

Models the existing Grafana "internal web UI behind Caddy + CIDR" pattern.

- **Serving:** `apps/admin-ui` builds to static assets served by a tiny static
  container → `${IMAGE_REGISTRY}/usan-admin-ui:${IMAGE_TAG}`. New overlay
  `infra/docker-compose.admin.yml` modeled on `grafana`: `pull_policy: always`,
  `journald` logging, **no published port** (Caddy reaches it on the bridge).
- **Caddy:** add a `{$ADMIN_DOMAIN}` site block with the L7
  `remote_ip {$ADMIN_ALLOWED_CIDR}` 403 gate (copied from Grafana — `:443` is
  SNI-shared, so the allowlist must be L7). Static assets proxy to the admin-ui
  container; `/v1/*` proxies to `api:8000` so the SPA and API are **same-origin**
  (SameSite=Strict cookie works; no CORS).
- **Terraform:** `cloudflare_dns_record` for `admin` (proxied=false → VM IP) in
  `terraform/dns.tf`. Existing `usan-allow-web` firewall already opens 80/443. New
  vars `ADMIN_DOMAIN`, `ADMIN_ALLOWED_CIDR`.
- **CI:** add a build job in `.github/workflows/build.yml` mirroring `api`; add
  `docker-compose.admin.yml` to the deploy SCP list and the `-f` compose chain.

**Deploy sequencing (sharp edges):**

- New env keys — `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`,
  `ADMIN_BOOTSTRAP_EMAILS`, `ADMIN_DOMAIN`, `ADMIN_ALLOWED_CIDR` — must be added
  to Secret Manager `usan-prod-env` **and** seeded onto the VM
  (`/opt/usan/infra/.env`) via reboot or IAP-SSH **before** the `v*` tag deploy
  (the deploy does not re-fetch the secret).
- Going live needs **both** a `v*` tag (app/compose) **and** `terraform apply`
  (DNS).
- The Google OAuth client needs the `admin.<domain>` redirect URI registered in
  the GCP console (manual step, documented in the plan).

## 12. Error handling

- **Agent fetch failure** → fall back to hardcoded constants; `WARNING` with
  `call_id`; call proceeds.
- **No published version** → default profile → constants; surfaced as "not
  published" in the list.
- **Invalid draft** → 422 with field-level errors mapped onto the form.
- **Concurrent edit / publish race** → optimistic-concurrency check
  (`updated_at`/version) → 409 "draft changed, reload."
- **SSO / non-allowlisted email** → 403, audit-logged, generic UI message.
- **Cartesia catalog unreachable** → UI degrades to a free-text voice-ID field.

## 13. Testing

Repo standard: pytest, ruff, mypy in CI; 80%+ coverage target.

- **API unit:** config Pydantic validation (caps, brace rejection, slot
  whitelist); resolution priority (override → elder → default → fallback);
  publish/version/rollback semantics; default-uniqueness constraint; audit writes.
- **API integration:** admin CRUD + draft/publish/rollback round-trips against a
  test Postgres; SSO callback with a mocked Google token (allow-listed vs
  rejected); `runtime/agent-config` JWT call-scope enforcement.
- **Agent:** `build_session` / `build_agent` from a `ResolvedConfig` (incl.
  tools-toggle filtering); the fallback-to-constants path when the fetch fails
  (extends `test_pipeline.py`).
- **Frontend:** Vitest + Testing Library for editor, publish-diff, validation;
  one Playwright smoke (login → edit → publish → rollback).
- Run `mypy` locally before pushing (CI runs it; CLAUDE.md omits it).

## 14. Phasing — PR decomposition

Each phase ships as its own squash-merged PR (per the project's plan-PR
workflow). Order chosen so each phase is independently testable.

1. **P1 — Backend foundation.** Alembic `0010` (profiles, versions, audit,
   `elders.agent_profile_id`, `calls.profile_override`, `admin_users`); config
   Pydantic schema; config
   repository; admin CRUD + draft/publish/versions/rollback/defaults endpoints
   (guarded by `OPERATOR_API_KEY` temporarily until P3). No UI. Fully testable via
   API. Scope: `api`.
2. **P2 — Agent integration. ✅ Done.** Single worker-token-guarded
   `GET /v1/runtime/agent-config?direction=&call_id=` resolve endpoint (always 200,
   returns `{source, profile_id, version, config}`); refactor of
   `pipeline.py`/`check_in.py`/`worker.py` to build from a resolved `AgentConfig`;
   tool registry; once-per-call fetch with fallback to local `DEFAULT_AGENT_CONFIG`;
   `max_call_duration` watchdog; prompt-safety validation. Override resolution reads
   `Call.profile_override` + `Elder.agent_profile_id`; the **admin setters** for
   those remain a later slice, so the live lever is the per-`direction` default.
   Config now drives calls. Scope: `api`, `agent`.
3. **P3 — Google SSO + audit attribution.** `/v1/auth/*`, session cookie,
   `admin_users` allow-list + bootstrap, `require_admin_session`/role
   dependencies; swap admin routes from `OPERATOR_API_KEY` to session auth; wire
   audit actor emails. Scope: `api`.
4. **P4 — React SPA.** `apps/admin-ui` with all screens (list, editor with all
   bundles, version history + diff + rollback, elder assignment, defaults, audit,
   admin users). Scope: new `admin-ui`.
5. **P5 — Infra & deploy.** `docker-compose.admin.yml`, Caddyfile route + CIDR,
   Terraform DNS + vars, `build.yml` job, secret seeding + OAuth redirect docs.
   Scope: `infra`, `ci`.

## 15. Open questions / future work

- Should `OUTBOUND_RINGING_TIMEOUT_S` (API dial-side) become profile-driven too?
  v1 puts `answer_timeout_s` + `max_call_duration_s` (agent-side) in the profile;
  the API can also read the ring timeout from the resolved profile if desired.
- Briefly caching the resolved config in the agent (short TTL) to drop the
  per-call round-trip — deferred; the round-trip is negligible and the fallback
  path covers outages.
- Profile-scoped retry policy and quiet hours (currently global constants) — a
  natural later extension once profiles exist.
- Wiring `elders.preferred_voice` is superseded by per-elder **profile**
  assignment; the legacy column can be dropped in a later migration.

## Appendix A — `config` document schema

Validated by `schemas/agent_config.py`. Defaults match today's constants/env.

```
prompts:
  system_prompt: str
  greeting: str
  recording_disclosure: str
  voicemail_message: str
  checkin_flow_instructions: str
  goodbye_message: str
  inbound_opening: str
  inbound_personalization_template: str   # only {elder_name}, {last_check_in_line}

voice:                  # Cartesia TTS
  cartesia_voice_id: str
  tts_model: str | null         # null → plugin default
  speed: float | null
  language: str | null

llm:
  model: str            # default "gemini-3.1-flash-lite"
  temperature: float | null

stt:                    # Cartesia STT
  model: str            # default "ink-whisper"
  language: str | null

timing:
  answer_timeout_s: float        # default 50.0 (agent-side)
  max_call_duration_s: int       # default 1800

tools:
  enabled: [str]        # subset of: log_wellness, log_medication,
                        #             get_today_meds, end_call

voicemail_detection:
  window_s: float       # default 3.0
  trigger_phrases: [str]

speech_advanced:        # maps to LiveKit AgentSession / plugin params
  vad_min_silence_s: float | null
  vad_activation_threshold: float | null
  turn_detection: str | null            # english | multilingual | vad
  min_endpointing_delay_s: float | null
  max_endpointing_delay_s: float | null
  min_interruption_duration_s: float | null
  min_interruption_words: int | null
```
