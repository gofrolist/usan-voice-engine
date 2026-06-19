# Automated invite email via Google Workspace (keyless DWD)

**Date:** 2026-06-19
**Status:** Approved — implementation
**Scope:** `apps/api` (invite send path) + `apps/admin-ui` (toast feedback) + `infra` (compose env)

## Problem

Org member invitations are **copy-link-only** (P3, `2026-06-17-tenancy-p3-invitations-design.md`).
The admin clicks **Invite**/**Resend**, the API returns an `accept_url`, and the admin UI
copies it to the clipboard ("Invite link copied"). **No email is ever sent** — the admin
must paste the link to the invitee out-of-band. Operators read the "Resend" button as
"send an email," so invitees never receive anything and report not getting the link.

## Goal

When an admin clicks **Invite** or **Resend**, the API emails the accept link to the
invitee automatically, sending from `noreply@usanretirement.com` via the Gmail API.
Authentication is **keyless** (the VM's attached service account self-signs a delegated
token — the same ADC trust already used for GCS signed URLs in `object_storage.py`), so
**no password or key file is stored**. The feature ships **inert** behind a flag; until
enabled, behavior is exactly today's copy-link flow.

## Non-goals

- No third-party email vendor (no SendGrid/SES/etc.) — Google Workspace only.
- No HTML email framework / templating engine — stdlib `email.message` + a small inline template.
- No async outbox / retry queue — invites are admin-triggered and rare; send synchronously
  in-request for immediate UI feedback. (The existing `notification_outbox` poller is for
  family SMS alerts and is intentionally not reused here.)
- No change to the accept flow, token model, or DB schema.

## Decisions

- **Keyless domain-wide delegation (DWD).** The VM service account mints a delegated
  `gmail.send` access token impersonating the sender mailbox, via IAM Credentials
  `signJwt` + the OAuth JWT-bearer grant. No stored secret. Mirrors the keyless trust the
  GCS signing path already relies on (`serviceAccountTokenCreator` on self).
- **Sender:** `noreply@usanretirement.com` (display name `USAN Admin`), overridable via env.
- **Best-effort, non-blocking-on-failure.** The invite is committed first (the link always
  works); the email send is attempted after and never fails the request. The response
  carries the outcome so the UI can fall back to copy-link.
- **No `object_storage.py` refactor.** The plan originally proposed extracting a shared ADC
  credentials helper. `test_object_storage.py` patches that module's internal
  `_signing_credentials` cache directly, and the GCS path signs PHI recording URLs — touching
  it for a cosmetic DRY win is not worth the regression risk. `gmail_sender.py` carries its
  own cached-creds helper following the identical proven pattern. A future DRY pass can unify them.
- **No new Python dependencies.** Reuses `google.auth` (already present), `httpx` (already
  present), and stdlib `email`/`base64`/`json`.

## Architecture

Two new, single-purpose modules plus thin wiring:

### `apps/api/src/usan_api/gmail_sender.py` — transport
- Cached ADC credentials (`google.auth.default(scopes=[cloud-platform])` + refresh,
  thread-safe, mirrors `object_storage._signing_creds`). The blocking refresh runs via
  `asyncio.to_thread`.
- `async def _delegated_access_token(settings, sa_email, adc_token) -> str`: builds the JWT
  claim set `{iss: sa_email, sub: sender, scope: gmail.send, aud: token-endpoint, iat, exp}`,
  signs it via `POST iamcredentials.googleapis.com/.../{sa}:signJwt` (bearer = ADC token),
  then exchanges the signed JWT at `POST oauth2.googleapis.com/token`
  (`grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`). Returns the `gmail.send` access
  token. Cached per-sender until ~60s before expiry.
- `class GmailMailer` implementing a `Mailer` protocol with
  `async def send(*, to, subject, text_body, html_body) -> None`: builds a MIME
  `multipart/alternative` message via stdlib `email.message.EmailMessage`, base64url-encodes
  the raw bytes, and `POST gmail.googleapis.com/gmail/v1/users/{sender}/messages/send`
  with the delegated bearer token. Raises `GmailSendError` on any transport/HTTP/parse failure.
- All endpoints are fixed Google hosts — no user-controlled URLs, no SSRF surface.
- **Never logs** the token, signed JWT, or the `raw` body.

### `apps/api/src/usan_api/invite_email.py` — content + orchestration
- `def render_invite_email(settings, *, email, role, accept_url, expires_at, invited_by) ->
  tuple[str, str, str]`: returns `(subject, text_body, html_body)`. Subject
  `You're invited to USAN Admin`. Body states the role, who invited them, the accept link,
  and the expiry; HTML has a button + a visible fallback link; text mirrors it.
- `async def send_invite_email(settings, *, invite, accept_url, mailer=None) -> bool`:
  best-effort. Builds content, sends via the mailer (default `GmailMailer(settings)`) under an
  `asyncio.wait_for(..., timeout=invite_email_timeout_s)` ceiling that bounds the **whole** send
  — including the ADC metadata-server refresh (`asyncio.to_thread`), which the per-HTTP-call
  httpx timeout does NOT cover — so a hung metadata server can never block the admin's request.
  Returns `True` on success. On **any** exception (including the timeout), logs a warning (invite
  id + recipient + error type — never the token) and returns `False`. Never raises.

### `routers/admin_invites.py` — wiring
- After `create_invite`/`resend_invite` commit, if `settings.invite_email_enabled`, call
  `await invite_email.send_invite_email(settings, invite=inv, accept_url=...)` and pass the
  resulting bool into `_out(...)`. When the flag is off, no attempt is made and `email_sent`
  is `None`.
- The send happens **after** `db.commit()` so a slow/failed send never holds the txn open or
  rolls back a valid invite. Consequently the **action** audit (`invite.create`/`invite.resend`,
  recorded atomically with the invite, before the send) does NOT carry the delivery outcome —
  `email_sent` is surfaced in the API response and in the application logs (a warning on
  failure, no token), not in `admin_audit`. Folding the outcome into the audit would require
  either sending before commit (breaking the commit-before-send invariant) or a second write;
  the action record is the security-relevant fact, so we keep it atomic.

### `schemas/invites.py`
- `InviteOut` gains `email_sent: bool | None = None`
  (`None` = not attempted / feature off; `True`/`False` = attempted result).

### `settings.py`
- `invite_email_enabled: bool = False` (alias `INVITE_EMAIL_ENABLED`)
- `invite_email_sender: str = "noreply@usanretirement.com"` (alias `INVITE_EMAIL_SENDER`)
- `invite_email_from_name: str = "USAN Admin"` (alias `INVITE_EMAIL_FROM_NAME`)
- `invite_email_timeout_s: int = 10` (ge 1, le 60, alias `INVITE_EMAIL_TIMEOUT_S`)
- `model_validator`: `invite_email_enabled=True` requires a non-blank `invite_email_sender`
  (fail at startup — mirrors `_scheduler_requires_gate`).

### `apps/admin-ui`
- `types/api.ts` `Invite` gains `email_sent: boolean | null`.
- `InvitesSection.tsx`: on Invite/Resend success, branch on `email_sent`:
  - `true` → toast `Invitation emailed to {email}`.
  - `false` → toast `Couldn't email the invite — link copied, send it manually` + copy link.
  - `null` → today's behavior: copy link + `Invite link copied`.
  - **"Copy link" stays** as the manual fallback.

### `infra/docker-compose.yml`
- Add to the `api` service `environment:` (ship-inert defaults; no prod-overlay change needed
  since the default is already off):
  - `INVITE_EMAIL_ENABLED: ${INVITE_EMAIL_ENABLED:-false}`
  - `INVITE_EMAIL_SENDER: ${INVITE_EMAIL_SENDER:-noreply@usanretirement.com}`
  - `INVITE_EMAIL_FROM_NAME: ${INVITE_EMAIL_FROM_NAME:-USAN Admin}`
  - `INVITE_EMAIL_TIMEOUT_S: ${INVITE_EMAIL_TIMEOUT_S:-10}`

## Data flow

```
Admin clicks Invite/Resend
  -> POST /v1/admin/invites[/{id}/resend]   (ADMIN-only, existing)
  -> repo.create_invite / repo.resend       (token + expiry, existing)
  -> db.commit()                            (invite is now valid -- link works)
  -> if invite_email_enabled:
        send_invite_email(...)             (best-effort, never raises)
          -> GmailMailer.send
              -> _delegated_access_token     (ADC -> signJwt -> jwt-bearer token, cached)
              -> Gmail users.messages.send    (MIME, base64url)
  -> InviteOut{..., email_sent: True|False|None}
  -> admin UI toast reflects the outcome
```

## Error handling

- **Email failure never loses an invite.** Commit precedes send; any send error is caught,
  logged (no token), and surfaced as `email_sent=False`. The admin falls back to copy-link.
- **Misconfigured flag-on** (sender blank) fails at startup via the model validator.
- **No PHI/secret in logs.** Recipient email + invite id only; never token, JWT, or access token.

## Testing (>=80%, no live Google calls)

- **`gmail_sender`** (unit, httpx + `google.auth.default` stubbed like `test_object_storage`):
  - delegated-token mint posts the right JWT claims to signJwt and exchanges for a token;
  - `send` builds a `multipart/alternative` MIME with the accept link and posts base64url `raw`
    to the correct `users/{sender}/messages/send` URL with the bearer token;
  - any httpx error raises `GmailSendError`.
- **`invite_email`** (unit): `render_invite_email` includes the link, role, and expiry in both
  parts; `send_invite_email` returns `True` on a fake mailer, `False` when the mailer raises
  (and never propagates).
- **`admin_invites`** (integration, mailer monkeypatched at `admin_invites.invite_email.
  send_invite_email`, matching the existing `repo.create_invite` patch style):
  - flag off (default) -> `email_sent` is `null`, no send attempted, existing tests unchanged;
  - flag on + send returns `True` -> `email_sent: true`;
  - flag on + send returns `False` -> `email_sent: false`, still `201`/`200`, `accept_url` present.
- **`InvitesSection.test.tsx`**: emailed toast on `email_sent:true`; fallback copy+toast on
  `email_sent:false`; legacy copy on `null`.
- **`settings`**: `INVITE_EMAIL_ENABLED=true` with blank sender raises at startup.

## Rollout (one-time, Workspace super-admin)

1. Create the `noreply@usanretirement.com` mailbox in Google Workspace.
2. Admin console -> Security -> API controls -> **Domain-wide delegation** -> add the VM service
   account's **client ID** with scope `https://www.googleapis.com/auth/gmail.send`.
3. Enable the **Gmail API** in the GCP project (the SA already holds
   `serviceAccountTokenCreator` on itself from GCS signing).
4. Refresh the VM `.env` with the new keys (set `INVITE_EMAIL_SENDER`), deploy a `v*` tag,
   then flip `INVITE_EMAIL_ENABLED=true`. (New `.env` keys need a VM reboot or IAP-SSH `.env`
   refresh before the tag deploy — per the deploy-env-not-refreshed operational note.)

Until step 4 flips the flag, nothing changes: the merge ships inert.
