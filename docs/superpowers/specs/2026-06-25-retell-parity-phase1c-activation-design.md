# RetellAI Parity Phase 1c — Activation Design

**Status:** Approved (2026-06-25)
**Program:** RetellAI full-API-parity (Phase 1, sub-PR 1c — the final Phase-1 slice)
**Predecessors:** Phase 1a (squash `049cc89`, #133) · Phase 1b (squash `71b401e`, #134), both merged to `main`, not yet deployed.
**Spec it activates:** `docs/superpowers/specs/2026-06-24-retell-parity-phase1-core-calling-design.md`

---

## 1. Goal

Make the already-built RetellAI-compat surface **safely operable in production**:

1. A **super-admin admin-UI screen** to mint, list, and revoke compat API keys (the keys are how a RetellAI client authenticates against our surface).
2. A **TOCTOU-free webhook delivery path** — close the DNS-rebind SSRF hole on both the native and the compat webhook senders, and enforce the compat allow-list at delivery time, because this phase opens a **PHI-bearing** webhook egress path.
3. **Templated `COMPAT_*` settings** (all 5) through compose + `.env` + the prod template, plus a concrete **activation runbook** for enabling webhook delivery to an attested CRM host.

**End state:** squash-merged to `main`. **No `v*` tag is cut by this work** — the operator deploys and goes live on their own schedule, guided by the runbook. Deploying carries no activation risk: the compat surface has **no master enable flag** and stays 401-inert until a super-admin mints the first key.

## 2. Decisions (locked with the user 2026-06-25)

| Decision | Choice |
|---|---|
| Webhook posture | **Enable to an attested host.** Webhook delivery is built production-ready and SSRF-safe; the actual flip to `true` + the real host value are operator-supplied prod config applied at the operator's later deploy. Shipped `.env` templates keep ship-inert defaults. |
| SSRF fix scope | **Both surfaces, IP-pin.** A shared "resolve-and-pin" path so the validated IP is the one connected to (check == use). Fixes native (`webhook_delivery.py`) + compat (`compat/webhook_delivery.py`). |
| Delivery-time allow-list re-check | **In scope (compat only).** Re-validate the destination host is still in `COMPAT_WEBHOOK_ALLOWED_HOSTS` at delivery, not just at registration. |
| Phase end | **Merge to main only.** No `v*` tag from this work. |

## 3. Non-goals (YAGNI)

- No new compat-key backend endpoints (rename, rotation, pagination, TTL). The shipped `POST/GET/DELETE /v1/admin/compat-keys` cover create-once-token / list / revoke — the UI uses exactly those.
- No master `COMPAT_ENABLED` flag — the surface is already 401-inert without a key; a flag would be redundant.
- No new DB migration. `compat_api_keys` exists (migration `0036`); nothing in 1c changes schema.
- No automatic pruning of webhook registrations when the allow-list shrinks — the delivery-time re-check (§5.B) makes the allow-list authoritative at send time, which is the security-relevant outcome. A registration audit is a runbook step, not code.
- Native webhook delivery has no host allow-list concept; the delivery-time allow-list re-check is compat-only by design.

## 4. Work item A — Compat Keys admin UI (`apps/admin-ui`)

No backend change. Built against the three existing super-admin endpoints:
`POST /v1/admin/compat-keys` (returns plaintext `token` once), `GET /v1/admin/compat-keys` (list), `DELETE /v1/admin/compat-keys/{key_id}` (revoke). All `require_super_admin`.

### Components
- `src/features/compat-keys/CompatKeysPage.tsx` — page: heading, "Create key" button, and a `Table` of keys (columns: key prefix · label · status · created · last used · revoke action). Empty state when no keys. `Spinner` while loading; list errors → `pushToast`.
- `src/features/compat-keys/hooks.ts` — react-query hooks over `lib/api.ts`:
  - `useCompatKeys()` → `api.get<CompatKey[]>("/v1/admin/compat-keys")`
  - `useCreateCompatKey()` → `api.post<CompatKeyCreated>("/v1/admin/compat-keys", { label })`; `onSuccess` invalidates the list query and surfaces the created token to the page.
  - `useRevokeCompatKey()` → `api.del("/v1/admin/compat-keys/{id}")`; `onSuccess` invalidates the list.
- `src/features/compat-keys/CreatedKeyDialog.tsx` — modal shown **once** immediately after a successful create. Displays the full `key_…` token in a monospace field, a **copy-to-clipboard** button, and a prominent "Store this now — it will never be shown again" warning. Closing requires an explicit acknowledge (button), not just backdrop dismissal, to reduce the chance of losing the only copy.
- Revoke → `ConfirmDialog` ("Revoke this key? Any client using it will immediately lose access. This cannot be undone.").

### Wiring
- `src/routes.tsx` — add `{ path: "compat-keys", element: (<RequireSuperAdmin><CompatKeysPage /></RequireSuperAdmin>) }` (mirrors the `organizations` route).
- `src/components/NavSidebar.tsx` — add a `superAdminOnly: true` nav item to the System group: `{ to: "/compat-keys", label: "Compat API Keys" }` with an icon consistent with the existing set.
- `src/types/api.ts` — `CompatKey` (id, key_prefix, status, label?, created_at, revoked_at?, last_used_at?) and `CompatKeyCreated` (= `CompatKey` + `token`).

### Tests (`src/test/CompatKeysPage.test.tsx`, vitest + testing-library)
Mock the 3 endpoints (vi.mock `lib/api` + `toast`), `QueryClient` with `retry:false`, render under `QueryClientProvider` + `MemoryRouter`. Assert:
- list renders rows from a GET fixture;
- "Create key" → enter label → token appears in the `CreatedKeyDialog` exactly once, copy button present; after acknowledge, list refetches and the new key's prefix is shown (token is not);
- revoke → `ConfirmDialog` → DELETE called → list refetches;
- a failing create surfaces a `pushToast` error.

CI runs `npm run typecheck` (tsc) **and** `npm run build` for admin-ui — local `lint`/`vitest` do not typecheck, so both must be run before pushing.

## 5. Work item B — SSRF IP-pin + delivery-time allow-list re-check (`apps/api`)

### The defect
`ssrf_guard.resolve_public_or_raise(host)` resolves the host and verifies every address is globally routable, then returns. The delivery code then calls `client.stream("POST", url, …)`, where **httpx re-resolves the host independently** at connect time. A malicious authoritative DNS server can rebind the name between the two resolutions, so httpx connects to `169.254.169.254` / loopback / a private IP that the guard rejected. `follow_redirects` is already `False`, so the only live vector is the resolve-then-connect race. Reachable only once webhook delivery is enabled — which this phase does.

### A. Resolve-and-pin (shared, both surfaces)
- `ssrf_guard.py`: expose the validated IP. Refactor so resolution returns the validated address(es) (e.g. `resolve_public_or_raise(host) -> list[str]` of validated IPs, preserving the existing fail-closed behavior; `_resolve` remains the monkeypatch seam tests already use).
- Add a helper that performs the webhook POST **pinned to a validated IP**: connect to the literal validated IP while preserving the original `Host` header and TLS SNI / certificate hostname (httpx per-request `sni_hostname` extension + explicit `Host`). Because the connection target is the already-validated IP, the IP checked is the IP used — the rebind window is structurally eliminated. Certificate verification continues to validate against the original hostname.
- Apply this pinned path in both `webhook_delivery.py` (native) and `compat/webhook_delivery.py` (compat), replacing the current "guard then `client.stream`" sequence.
- The exact httpx mechanism (the `sni_hostname` extension vs. a custom transport) is verified by the implementer against the pinned httpx version during TDD; the **invariant the tests enforce** is: the socket connects to a guard-validated IP, and cert validation still uses the original hostname.

### B. Delivery-time allow-list re-check (compat only)
- In `compat/webhook_delivery.py`, before delivering, re-validate the destination host is present in `COMPAT_WEBHOOK_ALLOWED_HOSTS` (fail-closed: empty list ⇒ no delivery). Today the allow-list is enforced only at registration, so a host removed from the list still receives deliveries from old registrations. This makes the allow-list authoritative at send time — directly relevant because this phase opens PHI egress.

### Error handling
Both checks fail closed by raising the existing `SsrfBlocked` (or the established delivery-skip path), logged **without PHI** (no transcript/recording/number in the log line). A blocked delivery is recorded as a delivery failure per the existing retry/housekeeping semantics — it does not crash the poller.

### Tests (`tests/test_ssrf_guard.py` + the two webhook-delivery test modules)
- **Rebind:** resolver returns a public IP at validate time, but the pinned connection target is a private IP / the transport is steered to a private IP ⇒ `SsrfBlocked`; no POST reaches a private address.
- **Cert hostname preserved:** the pinned request still validates the certificate against the original hostname (assert the SNI/cert hostname is the host, not the IP).
- **Happy path:** a genuinely public, allow-listed host delivers successfully.
- **Allow-list at delivery (compat):** a registered endpoint whose host is no longer in `COMPAT_WEBHOOK_ALLOWED_HOSTS` ⇒ zero deliveries, fail-closed.
- Existing `test_ssrf_guard.py` matrices (scheme, IP-literal decoys, denylist, ports) continue to pass.

## 6. Work item C — `COMPAT_*` settings templating + activation runbook (`infra/`, `docs/`)

The 5 settings (defined in `apps/api/src/usan_api/settings.py`): `COMPAT_DOCS_ENABLED` (bool, false), `COMPAT_WEBHOOK_ALLOWED_HOSTS` (csv str, ""), `COMPAT_DEFAULT_TIMEZONE` (str, `America/New_York`), `COMPAT_KEY_RATE_LIMIT` (str, `600/minute`), `COMPAT_WEBHOOK_DELIVERY_ENABLED` (bool, false). None are currently wired into compose/.env.

### Changes
- `infra/docker-compose.yml` — add all 5 to the api service `environment:` block via `${VAR:-default}` passthrough (matching the existing feature-block style), so each resolves to the `.env` value or the safe default.
- `infra/.env` (dev) — new `# === RetellAI-compatible API (ships inert) ===` section, all keys at ship-inert defaults.
- `infra/.env.prod.example` — same section with inline docs: the ship-inert discipline, the enable sequence, and the "keys must reach the VM `.env` before the tag deploy" gotcha. Defaults remain inert.
- `infra/README.md` — a "Production deploy → RetellAI-compat" subsection documenting the 3-layer plumbing and the BOTH-places gotcha.
- `docs/deployment/compat-settings-wiring.md` — the wiring reference: Secret Manager `usan-prod-env` → `startup.sh` writes `/opt/usan/infra/.env` → compose interpolates → api container env. Explicit: a new key no-ops unless it is in **both** the compose `environment:` map **and** the VM `.env`, because the `v*` tag deploy never re-fetches the secret.

### Activation runbook (in `infra/README.md` and/or the wiring doc)
Ordered operator steps to go live with PHI-bearing webhooks:
1. Deploy the merged code (cut a `v*` tag). Surface is still key-inert.
2. In the admin UI → **Compat API Keys** → create a key for the client; hand off the one-time token securely.
3. Set `COMPAT_WEBHOOK_ALLOWED_HOSTS=<attested CRM webhook FQDN>` (operator-supplied prod value) and `COMPAT_WEBHOOK_DELIVERY_ENABLED=true`.
4. Seed these into Secret Manager `usan-prod-env` **and** refresh the VM `/opt/usan/infra/.env` **before** the tag deploy (or reboot the VM to re-fetch via `startup.sh`).
5. Verify the api runs as the non-superuser `usan_app` role (RLS enforcing) — the tenant-isolation guarantee for compat traffic.
6. (When shrinking the allow-list later) audit `compat_webhook_endpoints.webhook_url` against the new list; the delivery-time re-check (§5.B) blocks sends to removed hosts, but stale registrations should still be reviewed.

## 7. Cross-cutting

- **Data flow:** admin UI → `/v1/admin/compat-keys` (super-admin, global control-plane table). RetellAI client → compat API with `Authorization: Bearer key_…` → key resolved by prefix + constant-time hash → opens an RLS session scoped to the key's org. Webhook delivery → SSRF-pinned + allow-list-gated POST to the attested host.
- **Security posture:** the SSRF IP-pin + delivery-time allow-list are the two controls that make PHI-bearing webhook egress safe; the one-time-token UI + revoke are the controls on key lifecycle.
- **Testing gate:** API — `ruff check` + `ruff format` + `mypy` + `pytest` (parallel default). admin-ui — `npm run typecheck` + `npm run build` + `npx vitest run`. No migration.
- **Delivery:** subagent-driven-development, per-task review, final whole-branch review on the most capable model, one PR, squash-merge to `main`. No `v*` tag.

## 8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| PHI egress to an untrusted webhook host | Fail-closed allow-list enforced at **registration and delivery** (§5.B) + SSRF IP-pin (§5.A); operator-attested FQDN only. |
| DNS-rebind to metadata/loopback/private | Resolve-and-pin: connect to the validated IP, cert against the original host (§5.A). |
| RLS bypass (compat traffic reads cross-tenant) | api runs as non-superuser `usan_app`; runbook step 5 verifies it. Already true in prod per deploy history. |
| One-time token lost by operator | `CreatedKeyDialog` forces explicit acknowledge + copy affordance; revoke + re-create is the recovery (no rotation endpoint by design). |
| Elevated compat rate-limit bucket abused via a leaked key | `COMPAT_KEY_RATE_LIMIT` is a bounded bucket; revoke via the new UI is immediate. |

## 9. Acceptance

- Super-admin can create (token shown once), list, and revoke compat keys in the admin UI; non-super-admins cannot reach the screen.
- A webhook delivery whose host rebinds to a private IP is blocked; a delivery to a host no longer in the allow-list is blocked; a legitimate allow-listed public host delivers.
- All 5 `COMPAT_*` keys are configurable through compose + `.env`, inert by default, with a documented activation runbook.
- Full local gate green (API + admin-ui). Merged to `main` via squash. No deploy tag cut by this work.
