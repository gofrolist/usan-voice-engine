# Retell → voice-engine agent seeding (migration bucket B)

Seeds the client's three live Retell single-prompt agents (plus the Betty QA tester) into
the voice engine as published `agent_profiles`, carrying each agent's **prompt** and its
**tools**. This is bucket B of the RetellAI → self-hosted migration (see
`docs/superpowers/specs/2026-07-10-retell-cutover-runbook.md`, precondition §0 / sign-off §6).

## What a migrated agent becomes

Each Retell agent maps to one `AgentConfig` document:

| Retell agent | Profile | Prompt source | External tools |
|---|---|---|---|
| Clara – Morning Check-in v0.2 | **Clara — Companion** | `prompts/checkin_v0.2_retell.txt` | 27 |
| Clara – Sales v0.1 | **Clara — Sales** | `prompts/sales_clara_v0.1_retell.txt` | 15 (+kb) |
| Clara – Inbound v0.1 | **Clara — Inbound** | `prompts/inbound_clara_v0.1_retell.txt` | 12 (+kb) |
| Betty Tester | **Betty — QA Tester** | `prompts/betty_tester_retell.txt` | 0 |

- **Prompt** — the full single-prompt drives the tool-enabled conversation agents, so it goes
  into the two flow fields those builders read: `checkin_flow_instructions` (outbound,
  `build_check_in_agent`) and `inbound_personalization_template` (inbound known-contact,
  `build_inbound_agent`). A profile may serve either direction depending on the contact's
  assignment, so both carry it. `system_prompt` is deliberately left as the **thin default
  persona**: it backs only the greet-only fallback agent (`build_agent`), which registers **no
  tools** — putting the tool-driving prompt there would instruct a tool-less agent to call
  functions it doesn't have. This required raising the two large flow-field caps to 65000 — the
  real prompts are 18–40 KB. (Note: the voice engine's *unknown*-inbound path is greet-only/
  tool-less by design; a migrated inbound agent serving unknown callers with its full toolset is
  a separate voice-engine capability, out of scope for the seed.)
- **Tools** — translated by the **same** code the live compat ingest uses
  (`usan_api.compat.tool_translate`), so `external_tools` is byte-identical to what a real
  RetellAI `create-agent` would store: URL, method, params, `timeout_s`, and
  `terminates_call` (the client's `end_call` carries `end_call_after_speech_with_success` →
  hangs up after logging its disposition). Native Clara builtins stay **off** (`enabled: []`)
  — migrated agents drive the client's own edge functions, not our wellness tools.

## Generate

```bash
cd apps/api
uv run python scripts/seed_retell_profiles/build_profiles.py \
    --backend-repo ~/gofrolist/usan-retirement-backend
```

Reads the canonical prompts + `retell/<agent>/*.json` decls from the client repo, validates
each config through the real `AgentConfig` schema, and writes `profiles/<key>.json`.
Deterministic and committed — regenerate whenever the client's prompts or tool decls change.

## Apply (seed + publish)

Run where the API's `DATABASE_URL` / env is in scope (opens sessions through the app's own
session factory, so the default-org tenant context is applied exactly as for the API):

```bash
cd apps/api
uv run python scripts/seed_retell_profiles/seed_profiles.py             # dry run
uv run python scripts/seed_retell_profiles/seed_profiles.py --apply     # create + publish
uv run python scripts/seed_retell_profiles/seed_profiles.py --apply --set-defaults
```

Idempotent: a profile whose live draft already equals the generated config is skipped.
`--set-defaults` points the direction defaults at the migrated agents (companion → outbound,
inbound → inbound). **Leave it off during canary** so the cutover controls routing (runbook
§3); flip it at full cutover.

After apply, hand the client each profile's `agent_<hex>` id for their
`RETELL_*_AGENT_ID` env (runbook Q1).

## ⚠️ CONFIRM before production (not derivable from the repo)

These are our-stack re-selections or values the Retell **dashboard** holds, not git. Confirm
with the client and edit the generated JSON (or the profile in the admin UI) before cutover:

- **Voice** — every agent defaults to Cartesia `Sarah — Mindful Woman`
  (`694f9389-…`). Retell ran ElevenLabs voices that don't exist on Cartesia, so this is a
  fresh pick, not a port. Confirm/choose per agent from `GET /v1/admin/voice-catalog`.
- **LLM model / temperature** — defaults to `gemini-2.5-flash`, plugin-default temperature.
  Retell ran a frontier model; confirm the reasoning/latency/cost trade-off per agent.
- **Short operational lines** — `greeting`, `recording_disclosure`, `voicemail_message`,
  `goodbye_message`, `inbound_opening` are defaulted (the client's per-call begin message is
  dynamic — companion passes `{{bm_greeting}}`, sales varies its opener in-prompt), so these
  static fields are largely inert on the worker path. Review per agent if any is spoken.
- **Knowledge bases** — Sales and Inbound call `kb_lookup` (handled natively via RAG, not an
  HTTP tool). Create the KBs (`scam_protection`, `wellbeing_activities`) in the KB admin UI,
  then set each profile's `llm.knowledge_base_ids`. Until then those agents run without RAG.
- **`{{dynamic_variables}}`** — the prompts reference tokens (`{{phone}}`,
  `{{offer_early_payment}}`, `{{bm_greeting}}`, …) supplied per call by the client's
  dispatcher via `retell_llm_dynamic_variables` (Surface 1). No action here; just confirm the
  dispatcher passes the same names it did on Retell.
