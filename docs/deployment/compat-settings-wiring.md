# RetellAI-compat settings wiring & activation

The RetellAI-compatible API (`/compat/*`) ships with the code but is **inert**: it is
always mounted yet returns **401** until a super-admin mints a compat API key. There is
**no master enable flag** — deploying the code activates nothing on its own.

## The 5 settings

| Key | Default | Purpose |
|-----|---------|---------|
| `COMPAT_DOCS_ENABLED` | `false` | Mounts the compat OpenAPI/docs at `/compat/docs` (separate from native `DOCS_ENABLED`). |
| `COMPAT_WEBHOOK_ALLOWED_HOSTS` | `""` | Comma-separated attested FQDNs allowed to receive PHI-bearing compat webhooks. Empty ⇒ nothing leaves (fail-closed), enforced at registration **and** delivery. |
| `COMPAT_DEFAULT_TIMEZONE` | `America/New_York` | Timezone for Contacts the compat layer lazily upserts (RetellAI has no Contact concept). |
| `COMPAT_KEY_RATE_LIMIT` | `600/minute` | Dedicated elevated rate-limit bucket for the compat key. |
| `COMPAT_WEBHOOK_DELIVERY_ENABLED` | `false` | Gates the claim+POST half of the compat webhook poller. Housekeeping runs regardless. |

## The 3-layer plumbing (and the BOTH-places gotcha)

`usan-prod-env` (Secret Manager) → `startup.sh` writes `/opt/usan/infra/.env` on boot →
`docker compose` interpolates → the api container receives the value via `environment:`.

A new key **no-ops unless it is in BOTH** the compose `environment:` map (`infra/docker-compose.yml`)
**and** the VM `.env`. The `v*` tag deploy runs `compose up --env-file infra/.env` and **never
re-fetches the secret**, so any value change must reach the VM `.env` **before** the tag is cut
(reboot to re-fetch via `startup.sh`, or IAP-SSH and edit `/opt/usan/infra/.env` by hand).

## Activation runbook (going live with PHI-bearing webhooks)

1. Deploy the merged code (`git tag vX.Y.Z && git push origin vX.Y.Z`). The surface is still
   key-inert.
2. Admin UI → **System → Compat API Keys** → **Create key** for the client (act-as the target
   org first). Hand off the one-time token securely; it is never shown again.
3. Set `COMPAT_WEBHOOK_ALLOWED_HOSTS=<attested CRM webhook FQDN>` and
   `COMPAT_WEBHOOK_DELIVERY_ENABLED=true` in the filled prod `.env`.
4. Push the filled `.env` to Secret Manager (`gcloud secrets versions add usan-prod-env
   --data-file=…`) **and** refresh the VM `/opt/usan/infra/.env` BEFORE cutting the tag.
5. Verify the api runs as the non-superuser `usan_app` role (RLS enforcing) — the tenant
   isolation guarantee for compat traffic.
6. When shrinking the allow-list later, audit `compat_webhook_endpoints.webhook_url` against the
   new list. Delivery-time re-validation (`_guard_host`) already blocks sends to removed hosts,
   but stale registrations should still be reviewed.

## Why deploying is safe

No key exists by default, so every compat endpoint 401s. Webhook delivery and docs default OFF.
Merging and even deploying the code changes no reachable behavior until step 2 is taken
deliberately by an operator.
