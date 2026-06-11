# admin-ui

React SPA for managing agent profiles, prompts, tools, calls, and admin users.
Served behind Caddy; talks to the FastAPI backend under `/v1/*` with Google-SSO
admin sessions.

## Development

```bash
npm install
npm run dev        # vite dev server
npm run typecheck  # tsc --noEmit
npm run lint       # eslint . --max-warnings 0
npm run test       # vitest run
```

## Prompt variables

Prompt fields (greeting, system prompt, voicemail message, …) support
`{{variable}}` tokens. The editor palette, unknown-token warnings, and PHI
warnings are all driven by the variable catalog fetched from
`GET /v1/admin/variable-catalog` (see `src/config/variableCatalog.ts`) — the
server is authoritative; the UI never hand-duplicates the list.

### Tiers

- **builtin** — the 10 frozen variables the API substitutes from call context
  (`first_name`, `elder_name`, `call_direction`, `current_time`, `current_date`,
  and the five PHI history variables `last_check_in`, `last_check_in_line`,
  `last_mood`, `last_pain`, `today_meds`).
- **custom** — operator-declared definitions managed on the **Variables** page
  (`/custom-variables`, Config nav group; mutations are ADMIN-role only).
  Definitions are documentation/UX only: name (immutable slug — delete and
  recreate to rename), description, example, and a PHI flag. **Values are never
  stored with the definition** — they arrive per call via `dynamic_vars`.
  CRUD mutations invalidate the catalog query, so the palette and warnings
  pick up changes immediately.

### Custom variables in SMS templates (renders-empty caveat)

SMS bodies substitute only non-PHI builtins plus clock variables. Per-call
`dynamic_vars` values **never** enter the SMS substitution map, so any custom
variable in an SMS template body renders as empty text:

- **Non-PHI custom (or undeclared) token** — saving warns
  (`{{name}} is not substituted in SMS — it will render as empty text.`) and
  the SMS editor shows a matching non-blocking notice. The save still succeeds.
- **PHI custom token** — the server rejects save, publish, *and* rollback with
  a field-level 422. The editor shows a "blocked at save" notice; the static
  zod check stays frozen on the 5 builtin PHI names — the server is
  authoritative for customs.

### Policy section (per-profile call policy)

The profile editor has a **Policy** section (`PolicySection.tsx`) mirroring the
API's optional `policy` config:

- **Quiet hours narrowing** — `quiet_hours_start_local` / `quiet_hours_end_local`
  (`HH:MM`) may only narrow within the statutory 09:00–21:00 local window,
  never widen it. Unset fields fall back to statutory bounds (shown as
  placeholders, never values).
- **Retry overrides** — `retry_delay_multiplier` (0.5–4.0) scales every retry
  ladder delay; `retry_max_attempts.{no_answer,voicemail_left,busy,failed}`
  (0–4) caps retries against the **chain-global** attempt number (0 disables;
  attempts past the builtin ladder reuse the final rung's delay).

Enforcement is entirely API-side and re-resolved at dial/retry time; an absent
`policy` section reproduces today's statutory/builtin behavior exactly. The
zod `policySchema` is a 1:1 mirror of the pydantic bounds so server 422 `loc`
paths map onto form fields.
