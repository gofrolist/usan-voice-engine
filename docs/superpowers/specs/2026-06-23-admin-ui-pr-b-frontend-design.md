# PR B — Admin-UI Call-Orchestration Frontend Design

**Date:** 2026-06-23
**Status:** Approved for planning
**Author:** Evgenii Vasilenko (with Claude Code)
**Parent spec:** `docs/superpowers/specs/2026-06-23-admin-ui-call-orchestration-self-service-design.md` (§6 = the UI sketch this elaborates)
**Backend:** PR A merged to `main` 2026-06-23 (squash `1c89945`, #127) — the `/v1/admin/*` contract this binds to.

## 1. Purpose & scope

PR A added SSO-gated, RLS-isolated `/v1/admin/*` endpoints for the full call-orchestration
plane (contacts lifecycle, recurring schedules, ad-hoc "call now", DNC). PR B is the
**admin-ui (React SPA)** that consumes them so an org admin can run a daily-wellness program
end-to-end with zero operator access.

**In scope:** contact create/edit/detail/delete, a per-contact schedules section + a global
schedules list, a DNC list/add/remove screen, and a call-now confirm modal with an
out-of-window acknowledgement. A simple key-value editor for call-personalization
`dynamic_vars`.

**Out of scope (carries the parent spec's non-goals):** batch-calling UI, outbound webhook
configuration, org-admin self-service of RetellAI-compat API keys, the `admin_profile_tests`
authz relaxation, a full contact-`metadata` editor, and any call to the RetellAI-compat
sub-app. The admin-ui edits authoritative **native** config only.

**No backend changes.** PR A's merged contract is sufficient; PR B is frontend + tests only.

## 2. Existing admin-ui facts that shape the design

Verified against `apps/admin-ui` during brainstorming:

- **Stack:** React 19 + TypeScript + Vite + React Router v7 + **React Query v5** +
  **Tailwind v4** + **React Hook Form + Zod**. Hand-rolled UI primitives; no component library.
- **API client:** `src/lib/api.ts` — typed `fetch` wrapper (`api.get/post/patch/del`),
  `credentials:"include"`, `ApiError{status,detail}`, 401 → full-page redirect to login.
  Data via `useQuery`/`useMutation`; mutations `invalidateQueries` the list key.
- **Session/role:** `useSession()` → `Me`; `useIsAdmin()` = `is_super_admin ||
  active_org?.role === "admin"`. Route guards `RequireAdmin` / `RequireSuperAdmin` redirect
  viewers to `/calls`. Page-level gates use `if (!isAdmin) return <p>Admins only.</p>`.
- **Primitives (`src/components/ui/` + `src/components/`):** `Dialog` (modal, portal,
  Esc/backdrop close), `ConfirmDialog` (destructive confirm, busy state — **no checkbox
  pattern today**), `Table`, `Tabs`, `Button` (primary/secondary/danger/ghost), `Input`,
  `Select`, `Textarea`, `Badge`, `Spinner`, toast store (`pushToast`). **No drawer exists.**
- **Already built:** `features/contacts/ContactsPage` (flat paginated table, masked phone,
  inline tz/profile `Select` edits, **no create/detail/delete**, page-gated admin-only);
  `features/calls/CallsPage` + `CallDetailPage` (rich filtered console; origin badge already
  renders "Ad hoc"; supports the `?contact_id=` deep-link). **No Schedules or DNC UI.**
- **Tests:** vitest + jsdom; mock the api module via `vi.mock("../lib/api")`; `meFixture`
  helper for role gating; tests live in `src/test/*.test.tsx` (and alongside features).
- **Masking:** `masked_phone` arrives pre-masked from the API (`***1234`); the client never
  masks and never receives raw `phone_e164`.

## 3. Decisions (resolved during brainstorming)

| # | Decision | Rationale |
|---|---|---|
| D1 | **Contact detail page** `/contacts/:id` is the home for per-contact edit, schedules, and call-now; **plus** a standalone global Schedules list and a standalone DNC page. | Schedules and call-now are inherently per-contact; the detail page gives them context. Matches the parent spec's "both". |
| D2 | **Modals, not drawers.** Create/edit/confirm use the existing `Dialog`/`ConfirmDialog`. | The codebase has no drawer primitive; modals are the established pattern. |
| D3 | **KV editor for call vars; contact `metadata` minimal.** A simple add-row key→scalar editor for schedule + call-now `dynamic_vars`; `metadata` is an advanced JSON textarea (defaults to `{}`). | `dynamic_vars` drive wellness-call personalization; `metadata` is an ops bag not worth a rich editor in this slice. |
| D4 | **Page-level admin gate** on all four surfaces, matching `ContactsPage`. | Consistency with today's UI; backend permits VIEWER reads, so relaxing later is trivial. |
| D5 | **DNC removal re-enters the full E.164.** | The DNC list returns masked phones only and `DELETE /v1/admin/dnc/{phone_e164}` is keyed by the full number (phone is the PK). Consistent with masked-phone-on-edit; avoids a backend change. |
| D6 | **Masked-phone-on-edit.** Edit form's phone field starts empty (helper shows current masked); blank → omit `phone_e164` from PATCH; create requires it. | PHI minimization (parent spec §5). |
| D7 | **Reuse `/calls?contact_id=` for "recent calls."** | The deep-link already exists; no new query/screen. |
| D8 | **Server is the validation source of truth.** Client Zod mirrors constraints for fast feedback; `422`/`409` map to inline form errors. | Avoids drift from the backend's authoritative validators. |

## 4. Routes & navigation

- **New route:** `/contacts/:id` → `ContactDetailPage` (wrapped admin-only).
- **New routes:** `/schedules` → `SchedulesPage`; `/dnc` → `DncPage` (both admin-only).
- **Nav (`NavSidebar.tsx`):** add `Schedules` and `DNC` items to the **Config** group
  (`adminOnly: true`), alongside the existing `Contacts`.
- **Contacts list:** rows become links to `/contacts/:id`; add a `+ New contact` button
  (admin-only) opening `ContactFormDialog` in create mode.

## 5. Component / file structure

```
features/contacts/
  ContactsPage.tsx        (extend: + New contact, row → detail link)
  ContactDetailPage.tsx   (new: header + Edit/Delete/Call-now + Schedules section + recent-calls link)
  ContactFormDialog.tsx   (new: create + edit modal; masked-phone-on-edit; metadata JSON textarea)
  CallNowDialog.tsx       (new: dynamic_vars KV + profile override + REQUIRED out-of-window ack)
  hooks.ts                (extend: useContact, useCreateContact, useUpdateContact,
                           useDeleteContact, useCallNow)
features/schedules/
  SchedulesPage.tsx       (new: global filtered list — contact/slot/skipped_window)
  ScheduleSection.tsx     (new: per-contact list embedded in ContactDetailPage)
  ScheduleFormDialog.tsx  (new: shared create/edit; window/days/slot/enabled/override/vars)
  hooks.ts                (new)
features/dnc/
  DncPage.tsx             (new: list + Add modal + remove-with-typed-phone confirm)
  AddDncDialog.tsx        (new)
  hooks.ts                (new)
components/ui/
  KeyValueEditor.tsx      (new: add-row key→scalar; scalar-only + 8192-byte cap)
  DaysOfWeekPicker.tsx    (new: 7-day toggle row; non-empty)
types/api.ts              (extend: ContactDetail, ContactCreate/Update, ScheduleResponse,
                           Create/UpdateScheduleRequest, AdminDNCResponse, AdminCreateCallRequest)
routes.tsx                (extend: /contacts/:id, /schedules, /dnc)
```

## 6. Screen behavior

### 6.1 Contacts list (extend)
Keep the paginated table + inline tz/profile selects. Add a `+ New contact` button (admin)
→ `ContactFormDialog` (create). Rows link to `/contacts/:id`. On create success: invalidate
`["contacts"]`, close, toast. `409` (duplicate phone/external_id) → inline field error.

### 6.2 Contact detail `/contacts/:id`
- **Header:** name, masked phone, timezone, assigned profile. Buttons: `Edit`
  (`ContactFormDialog` edit mode), `Delete` (`ConfirmDialog`; backend hard-deletes — the
  plan confirms the contact→schedules and contact→calls FK on-delete behavior and the
  warning copy reflects the verified outcome), `📞 Call now` (`CallNowDialog`).
- **Schedules section** (`ScheduleSection`): lists the contact's schedules (morning/evening),
  each showing window, days, enabled toggle, profile override, var-count. `+ Add` is shown
  only for a **free** slot (one-per-slot); `Edit` opens `ScheduleFormDialog` (slot read-only);
  `Delete` confirms. Enabled toggle is a PATCH.
- **Recent calls:** a link to `/calls?contact_id={id}` (existing screen).
- `404` on the contact → "Contact not found" state.

### 6.3 Schedules global `/schedules`
Filtered table (filters: contact, slot, `last_result=skipped_window` = "who missed today").
Columns: contact (link), slot, window, days, enabled, `next_run_at`, `last_result`. Filter
changes reset pagination (matches `CallsPage`). Edit/delete reuse `ScheduleFormDialog` /
`ConfirmDialog`. **Create happens on contact detail** (slot-availability context); the global
page manages existing schedules.

### 6.4 DNC `/dnc`
Paginated list (masked phone, reason, added_at). `Add to DNC` (admin) → `AddDncDialog`
(full E.164 + optional reason; `201`). Remove (admin) → `ConfirmDialog` containing a **full
E.164 input** (per D5) → `DELETE /v1/admin/dnc/{phone}`; `404` if already gone → toast.

### 6.5 Call-now (`CallNowDialog`)
Shows contact name + masked phone. Optional `dynamic_vars` (`KeyValueEditor`) and
`profile_override` (`Select` of live profiles). A **required** checkbox "This calls the
contact outside their normal window" — Confirm disabled until checked. `POST /v1/admin/calls`:
- `202` → toast "Call queued" (+ optional link to the call).
- `200` with `status=dnc_blocked` → inline "This number is on the Do-Not-Call list; the call
  was blocked." (not an error toast).
- `503` → "Outbound calling is not available in this environment."
- `422` → inline field errors (bad `dynamic_vars` / unknown `profile_override`).

## 7. Forms & validation

React Hook Form + `zodResolver`. Zod schemas mirror the backend (§ "backend contract"):

- **Contact:** `name` 1–200; `phone_e164` `^\+[1-9]\d{7,14}$` (required on create, optional/
  replacement on edit); `timezone` non-empty (server validates IANA); `external_id`/
  `preferred_voice` ≤200; `metadata` parsed from a JSON textarea (must be an object).
- **Schedule:** `contact_id`; `slot` morning|evening (create only; immutable on edit);
  `window_start_local` < `window_end_local`; window **must intersect `[09:00, 21:00)`**;
  `days_of_week` non-empty, no dups; `dynamic_vars` scalar-only ≤8192 bytes; `enabled`;
  optional `profile_override`. PATCH sends window start+end **together** or neither.
- **DNC:** `phone_e164` E.164; `reason` ≤1000.
- **Call-now:** `contact_id`; `dynamic_vars` scalar-only ≤8192 bytes; optional
  `profile_override`; ack checkbox (UI-only gate, not sent).

Server `422`/`409`/`404`/`503` are authoritative and surface inline or via toast.

## 8. Testing (vitest)

One test file per page/dialog, mocking `api` via `vi.mock`, using `meFixture` for roles:

- **ContactsPage:** `+ New contact` visible for admin / page-gated for viewer; create flow;
  `409` duplicate → field error.
- **ContactDetailPage:** renders header/sections; delete confirm; opens edit + call-now;
  `404` state.
- **ContactFormDialog:** create requires phone; **edit blank phone omits `phone_e164`**;
  invalid E.164 → error; metadata JSON parse error.
- **CallNowDialog:** **Confirm disabled until ack checked**; `202` toast; `200 dnc_blocked`
  inline; `503` inline.
- **SchedulesPage:** filters → query params; `skipped_window` filter; row → contact link.
- **ScheduleFormDialog:** window start≥end → error; non-intersecting window → error; empty
  days → error; **slot read-only on edit**; window-pair-together on PATCH.
- **DncPage:** add flow; **remove requires the typed full E.164**; `404`-already-gone toast.
- **KeyValueEditor:** add/remove rows; non-scalar/oversize → blocked.

Target ≥80% coverage on new modules. CI gates: `Lint admin-ui (apps/admin-ui)` and
`vitest (apps/admin-ui)` must be green.

## 9. Delivery

- Single squash **PR B** on a fresh branch off the current `main` (PR A already merged).
- Frontend + tests only; **no backend, no migration, no new env keys.**
- Ships on the next `v*` tag — the **same** tag should carry PR A's backend so the API the UI
  needs is live when the UI ships. Merging to `main` changes nothing live until tagged.

## 10. Risks & mitigations

| Risk | Mitigation |
|---|---|
| DNC remove-by-typed-phone friction annoys admins | Accepted per D5/PHI; clear helper text. Revisit with a backend opaque-id only if it proves a real blocker. |
| Client window/days validation drifts from server rules | Server `422` is authoritative and always surfaced; client checks are fast-feedback only. |
| Contact delete surprises (cascade vs orphan of schedules/calls) | The plan verifies the contact→schedules / contact→calls FK `ondelete` behavior in the models; `ConfirmDialog` copy reflects the verified outcome before merge. |
| Profile-override select lists archived/non-live profiles | Filter to live profiles client-side; server `422` on a non-live override is the backstop. |
| Schedules global page and per-contact section diverge | Both use the one shared `ScheduleFormDialog` (DRY). |
