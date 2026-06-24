# PR B — Admin-UI Call-Orchestration Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the admin-ui (React SPA) surfaces that let an org admin manage contacts, recurring schedules, ad-hoc "call now", and the DNC list against the `/v1/admin/*` endpoints merged in PR A.

**Architecture:** Extend the existing `features/contacts` feature and add `features/schedules` + `features/dnc`, plus two shared `components/ui` form primitives. A new `/contacts/:id` detail page is the home for per-contact edit, schedules, and call-now; standalone `/schedules` and `/dnc` pages cover the global views. All data flows through the existing typed `api` wrapper + React Query hooks; dialogs hand-roll `useState` (the codebase convention — no RHF/Zod in dialogs).

**Tech Stack:** React 19, TypeScript, Vite, React Router v7, React Query v5 (`@tanstack/react-query`), Tailwind v4, vitest + @testing-library/react + @testing-library/user-event.

**Spec:** `docs/superpowers/specs/2026-06-23-admin-ui-pr-b-frontend-design.md`
**Backend contract:** PR A merged `1c89945` (#127). No backend changes in this plan.
**Branch:** `feat/admin-call-orchestration-ui` (off `main`, atop `1c89945`).

## Global Constraints

- **Dialog forms use `useState` + manual validation**, mirroring `features/customVariables/DeclareVariableDialog.tsx` and `features/members/MembersPage.tsx`. **Do NOT introduce React Hook Form or Zod** into any dialog (RHF/Zod live only in the profile editor sections).
- **Server is the validation source of truth (spec D8).** Client checks are fast pre-submit feedback only; surface server `422`/`409`/`404`/`503` via inline messages or `pushToast(err.detail)`. Never swallow an error.
- **PHI: the raw `phone_e164` is never displayed.** Lists/detail show `masked_phone` (pre-masked `***1234` from the API). On edit, the phone field starts empty; blank → omit `phone_e164` from the PATCH (spec D6). DNC removal requires re-typing the full E.164 (spec D5).
- **Mutations call `pushToast(err.detail)` on error** and `invalidateQueries` the relevant list key on success — the `features/contacts/hooks.ts` and `features/profiles/hooks.ts` pattern.
- **Page-level admin gate** mirrors `ContactsPage`: `const isAdmin = useIsAdmin(); if (!isAdmin) return <p className="text-sm text-muted">Admins only.</p>;`. Write controls additionally hidden behind `{isAdmin ? … : null}`.
- **Routes** are added under the `PageLayout` children in `src/routes.tsx`, wrapped in `<RequireAdmin>` for new pages, matching the existing `/contacts` entry.
- **Tests** mock the api module by URL route (`vi.mock("../lib/api", …)`), use `meFixture(role)` for `/v1/auth/me`, render inside `<QueryClientProvider>` (+ `<MemoryRouter>` when the component uses router hooks/links). Target ≥80% coverage on new modules.
- **E.164 client regex:** `/^\+[1-9]\d{7,14}$/`. **IANA timezone** is validated server-side only (client sends a free-text/`US_TIMEZONES` value).
- **dynamic_vars cap:** `DYNAMIC_VARS_MAX_BYTES = 8192` bytes of `JSON.stringify`; values are strings (scalar-only is automatic).
- **Quiet-hours window** must satisfy `window_start_local < window_end_local` and intersect `[09:00, 21:00)` (the server's statutory window); mirror both client-side.
- **Lint/test gates (CI):** `cd apps/admin-ui && npm run lint` and `npx vitest run` must pass. Run `npx vitest run <file>` per task.

---

## File Structure

```
apps/admin-ui/src/
  lib/dynamicVars.ts                       (NEW — byte-cap helper + const)
  components/ui/KeyValueEditor.tsx         (NEW — dynamic_vars row editor)
  components/ui/DaysOfWeekPicker.tsx       (NEW — 7-day toggle)
  components/nav-icons.tsx                 (MODIFY — add SchedulesIcon, DncIcon)
  components/NavSidebar.tsx                (MODIFY — add Schedules + DNC nav items)
  routes.tsx                               (MODIFY — /contacts/:id, /schedules, /dnc)
  types/api.ts                             (MODIFY — contact/schedule/call/dnc types)
  features/contacts/
    hooks.ts                               (MODIFY — useContact/Create/Update/Delete/CallNow)
    ContactsPage.tsx                       (MODIFY — + New contact, row → detail link)
    ContactFormDialog.tsx                  (NEW)
    ContactDetailPage.tsx                  (NEW)
    CallNowDialog.tsx                      (NEW)
  features/schedules/
    hooks.ts                               (NEW)
    ScheduleFormDialog.tsx                 (NEW)
    ScheduleSection.tsx                    (NEW)
    SchedulesPage.tsx                      (NEW)
  features/dnc/
    hooks.ts                               (NEW)
    DncPage.tsx                            (NEW)
    AddDncDialog.tsx                       (NEW)
  test/                                    (NEW test files, one per unit)
```

---

## Task 1: Shared form primitives (KeyValueEditor, DaysOfWeekPicker, dynamicVars util)

**Files:**
- Create: `apps/admin-ui/src/lib/dynamicVars.ts`
- Create: `apps/admin-ui/src/components/ui/KeyValueEditor.tsx`
- Create: `apps/admin-ui/src/components/ui/DaysOfWeekPicker.tsx`
- Test: `apps/admin-ui/src/test/KeyValueEditor.test.tsx`, `apps/admin-ui/src/test/DaysOfWeekPicker.test.tsx`

**Interfaces:**
- Produces (used by Tasks 4, 5):
  - `DYNAMIC_VARS_MAX_BYTES: number`, `dynamicVarsByteSize(vars: Record<string,string>): number`
  - `KvRow { id: string; key: string; value: string }`, `rowsToRecord(rows: KvRow[]): Record<string,string>`, `recordToRows(record: Record<string,unknown>): KvRow[]`, `<KeyValueEditor rows onChange label addLabel? />`
  - This task also adds the `Weekday` union to `types/api.ts` (pure type) so `DaysOfWeekPicker` has no forward dependency on Task 5.

- [ ] **Step 1: Add the `Weekday` type to `types/api.ts`**

Append to `apps/admin-ui/src/types/api.ts` (end of file):

```typescript
// ---------------------------------------------------------------------------
// Schedules — admin self-service (admin_schedules.py). Full request/response
// types land in Task 5; the Weekday union is declared here because the shared
// DaysOfWeekPicker primitive (Task 1) needs it.
// ---------------------------------------------------------------------------

export type Weekday =
  | "monday"
  | "tuesday"
  | "wednesday"
  | "thursday"
  | "friday"
  | "saturday"
  | "sunday";
```

- [ ] **Step 2: Write the failing test for `dynamicVarsByteSize` + `KeyValueEditor`**

Create `apps/admin-ui/src/test/KeyValueEditor.test.tsx`:

```tsx
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import {
  KeyValueEditor,
  rowsToRecord,
  recordToRows,
  type KvRow,
} from "../components/ui/KeyValueEditor";
import { dynamicVarsByteSize, DYNAMIC_VARS_MAX_BYTES } from "../lib/dynamicVars";

function Harness({ initial = [] as KvRow[] }) {
  const [rows, setRows] = useState<KvRow[]>(initial);
  return (
    <>
      <KeyValueEditor rows={rows} onChange={setRows} label="Variables" />
      <output data-testid="record">{JSON.stringify(rowsToRecord(rows))}</output>
    </>
  );
}

describe("dynamicVars helpers", () => {
  it("byte size counts JSON bytes and the cap is 8192", () => {
    expect(DYNAMIC_VARS_MAX_BYTES).toBe(8192);
    expect(dynamicVarsByteSize({ a: "b" })).toBe(JSON.stringify({ a: "b" }).length);
  });

  it("rowsToRecord drops empty keys; last duplicate wins", () => {
    expect(
      rowsToRecord([
        { id: "1", key: "first_name", value: "Jane" },
        { id: "2", key: "  ", value: "ignored" },
        { id: "3", key: "first_name", value: "Janet" },
      ]),
    ).toEqual({ first_name: "Janet" });
  });

  it("recordToRows round-trips a record into editable rows", () => {
    expect(recordToRows({ a: "1", b: "2" }).map((r) => [r.key, r.value])).toEqual([
      ["a", "1"],
      ["b", "2"],
    ]);
  });
});

describe("KeyValueEditor", () => {
  it("adds, edits and removes rows, emitting the record", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Add variable" }));
    await user.type(screen.getByLabelText("Variables key"), "first_name");
    await user.type(screen.getByLabelText("Variables value"), "Jane");
    expect(screen.getByTestId("record")).toHaveTextContent('{"first_name":"Jane"}');
    await user.click(screen.getByRole("button", { name: /Remove/ }));
    expect(screen.getByTestId("record")).toHaveTextContent("{}");
  });
});
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `cd apps/admin-ui && npx vitest run src/test/KeyValueEditor.test.tsx`
Expected: FAIL (cannot resolve `../lib/dynamicVars` / `../components/ui/KeyValueEditor`).

- [ ] **Step 4: Implement `lib/dynamicVars.ts`**

Create `apps/admin-ui/src/lib/dynamicVars.ts`:

```typescript
// Shared helpers for the dynamic_vars / call-personalization editor. The server
// caps a schedule's / call's dynamic_vars at 8192 bytes of serialized JSON and
// rejects non-scalar values; we mirror the byte cap client-side for instant
// feedback (the server 422 stays authoritative).
export const DYNAMIC_VARS_MAX_BYTES = 8192;

export function dynamicVarsByteSize(vars: Record<string, string>): number {
  return new TextEncoder().encode(JSON.stringify(vars)).length;
}
```

- [ ] **Step 5: Implement `components/ui/KeyValueEditor.tsx`**

Create `apps/admin-ui/src/components/ui/KeyValueEditor.tsx`:

```tsx
import { useId } from "react";
import { Input } from "./input";
import { Button } from "./button";

// A row keeps its own identity so editing a key never reorders/loses focus. The
// emitted record contains only rows with a non-empty (trimmed) key; a later
// duplicate key wins, matching plain-object semantics.
export interface KvRow {
  id: string;
  key: string;
  value: string;
}

export function rowsToRecord(rows: KvRow[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const r of rows) {
    const k = r.key.trim();
    if (k.length > 0) out[k] = r.value;
  }
  return out;
}

export function recordToRows(record: Record<string, unknown>): KvRow[] {
  return Object.entries(record).map(([key, value], i) => ({
    id: `r${i}-${key}`,
    key,
    value: value == null ? "" : String(value),
  }));
}

interface KeyValueEditorProps {
  rows: KvRow[];
  onChange: (rows: KvRow[]) => void;
  label: string;
  addLabel?: string;
}

export function KeyValueEditor({
  rows,
  onChange,
  label,
  addLabel = "Add variable",
}: KeyValueEditorProps) {
  const baseId = useId();
  const setRow = (id: string, patch: Partial<KvRow>) =>
    onChange(rows.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  const addRow = () =>
    onChange([...rows, { id: `${baseId}-${rows.length}-${Date.now()}`, key: "", value: "" }]);
  const removeRow = (id: string) => onChange(rows.filter((r) => r.id !== id));

  return (
    <div>
      <div className="mb-1 block text-xs font-medium text-slate-600">{label}</div>
      <div className="space-y-2">
        {rows.map((r) => (
          <div key={r.id} className="flex items-center gap-2">
            <Input
              aria-label={`${label} key`}
              placeholder="first_name"
              value={r.key}
              onChange={(e) => setRow(r.id, { key: e.target.value })}
            />
            <Input
              aria-label={`${label} value`}
              placeholder="Jane"
              value={r.value}
              onChange={(e) => setRow(r.id, { value: e.target.value })}
            />
            <Button
              type="button"
              variant="ghost"
              aria-label={`Remove ${r.key || "row"}`}
              onClick={() => removeRow(r.id)}
            >
              ✕
            </Button>
          </div>
        ))}
      </div>
      <Button type="button" variant="secondary" className="mt-2" onClick={addRow}>
        {addLabel}
      </Button>
    </div>
  );
}
```

> Note: `Date.now()` is fine in app code (the no-`Date.now` rule applies only to Workflow scripts).

- [ ] **Step 6: Run `KeyValueEditor` test to verify it passes**

Run: `cd apps/admin-ui && npx vitest run src/test/KeyValueEditor.test.tsx`
Expected: PASS (4 tests).

- [ ] **Step 7: Write the failing `DaysOfWeekPicker` test**

Create `apps/admin-ui/src/test/DaysOfWeekPicker.test.tsx`:

```tsx
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { DaysOfWeekPicker } from "../components/ui/DaysOfWeekPicker";
import type { Weekday } from "../types/api";

function Harness({ initial = [] as Weekday[] }) {
  const [days, setDays] = useState<Weekday[]>(initial);
  return (
    <>
      <DaysOfWeekPicker value={days} onChange={setDays} />
      <output data-testid="days">{days.join(",")}</output>
    </>
  );
}

describe("DaysOfWeekPicker", () => {
  it("toggles days and always emits Mon-first order", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByLabelText("friday"));
    await user.click(screen.getByLabelText("monday"));
    expect(screen.getByTestId("days")).toHaveTextContent("monday,friday");
    await user.click(screen.getByLabelText("monday"));
    expect(screen.getByTestId("days")).toHaveTextContent("friday");
  });
});
```

- [ ] **Step 8: Run it to verify it fails**

Run: `cd apps/admin-ui && npx vitest run src/test/DaysOfWeekPicker.test.tsx`
Expected: FAIL (cannot resolve `DaysOfWeekPicker`).

- [ ] **Step 9: Implement `components/ui/DaysOfWeekPicker.tsx`**

Create `apps/admin-ui/src/components/ui/DaysOfWeekPicker.tsx`:

```tsx
import type { Weekday } from "../../types/api";

const DAYS: { value: Weekday; label: string }[] = [
  { value: "monday", label: "Mon" },
  { value: "tuesday", label: "Tue" },
  { value: "wednesday", label: "Wed" },
  { value: "thursday", label: "Thu" },
  { value: "friday", label: "Fri" },
  { value: "saturday", label: "Sat" },
  { value: "sunday", label: "Sun" },
];

interface DaysOfWeekPickerProps {
  value: Weekday[];
  onChange: (days: Weekday[]) => void;
}

// Seven toggle checkboxes. Emits days in canonical Mon-first order regardless of
// click order (the server normalizes too; ordering here just avoids noisy diffs).
export function DaysOfWeekPicker({ value, onChange }: DaysOfWeekPickerProps) {
  const selected = new Set(value);
  const toggle = (day: Weekday) => {
    const next = new Set(selected);
    if (next.has(day)) next.delete(day);
    else next.add(day);
    onChange(DAYS.map((d) => d.value).filter((d) => next.has(d)));
  };
  return (
    <div className="flex flex-wrap gap-1.5">
      {DAYS.map((d) => (
        <label
          key={d.value}
          className="flex cursor-pointer items-center gap-1 rounded-lg border border-line-strong px-2 py-1 text-sm text-ink"
        >
          <input
            type="checkbox"
            aria-label={d.value}
            checked={selected.has(d.value)}
            onChange={() => toggle(d.value)}
          />
          {d.label}
        </label>
      ))}
    </div>
  );
}
```

- [ ] **Step 10: Run it to verify it passes**

Run: `cd apps/admin-ui && npx vitest run src/test/DaysOfWeekPicker.test.tsx`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add apps/admin-ui/src/lib/dynamicVars.ts apps/admin-ui/src/components/ui/KeyValueEditor.tsx apps/admin-ui/src/components/ui/DaysOfWeekPicker.tsx apps/admin-ui/src/types/api.ts apps/admin-ui/src/test/KeyValueEditor.test.tsx apps/admin-ui/src/test/DaysOfWeekPicker.test.tsx
git commit -m "feat(admin-ui): shared dynamic_vars + days-of-week form primitives"
```

---

## Task 2: Contact create/edit dialog + ContactsPage wiring

**Files:**
- Modify: `apps/admin-ui/src/types/api.ts` (add `ContactCreate`, `ContactUpdate`, `ContactDetail`)
- Modify: `apps/admin-ui/src/features/contacts/hooks.ts` (add `useContact`, `useCreateContact`, `useUpdateContact`)
- Create: `apps/admin-ui/src/features/contacts/ContactFormDialog.tsx`
- Modify: `apps/admin-ui/src/features/contacts/ContactsPage.tsx` (`+ New contact`, rows link to `/contacts/:id`)
- Test: `apps/admin-ui/src/test/ContactFormDialog.test.tsx`, and extend `apps/admin-ui/src/test/ContactsPage.test.tsx`

**Interfaces:**
- Consumes: `api`, `pushToast`, `ContactSummary` (existing).
- Produces (used by Tasks 3, 4):
  - `ContactCreate`, `ContactUpdate`, `ContactDetail` types
  - `useContact(id): UseQueryResult<ContactDetail>`
  - `useCreateContact(): UseMutationResult<ContactDetail, ApiError, ContactCreate>`
  - `useUpdateContact(): UseMutationResult<ContactDetail, ApiError, { id: string; body: ContactUpdate }>`
  - `<ContactFormDialog mode="create"|"edit" contact?: ContactDetail onClose />` — self-contained dialog that calls the hooks and closes on success.

- [ ] **Step 1: Add contact types to `types/api.ts`**

Append after the existing `SetTimezoneRequest` block in `apps/admin-ui/src/types/api.ts`:

```typescript
// Admin self-service contact lifecycle (admin.py: ContactCreate/Update/Detail).
export interface ContactCreate {
  name: string;
  phone_e164: string;
  timezone: string;
  external_id?: string | null;
  preferred_voice?: string | null;
  metadata?: Record<string, unknown>;
}

// PATCH: every field optional; omitted keys are left unchanged. `phone_e164` is a
// full E.164 *replacement* — the stored number is never echoed back to the browser.
export interface ContactUpdate {
  name?: string;
  phone_e164?: string;
  timezone?: string;
  external_id?: string | null;
  preferred_voice?: string | null;
  metadata?: Record<string, unknown>;
}

export interface ContactDetail extends ContactSummary {
  external_id: string | null;
  preferred_voice: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}
```

- [ ] **Step 2: Write the failing `ContactFormDialog` test**

Create `apps/admin-ui/src/test/ContactFormDialog.test.tsx`:

```tsx
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import type { ContactDetail } from "../types/api";

const postMock = vi.fn();
const patchMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    post: (u: string, b?: unknown) => postMock(u, b),
    patch: (u: string, b?: unknown) => patchMock(u, b),
  },
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      public detail: string,
    ) {
      super(detail);
    }
  },
}));
vi.mock("../components/ui/toast", () => ({ pushToast: vi.fn() }));

import { ContactFormDialog } from "../features/contacts/ContactFormDialog";

const existing: ContactDetail = {
  id: "11111111-1111-1111-1111-111111111111",
  name: "Edna Moore",
  masked_phone: "***4567",
  timezone: "America/New_York",
  agent_profile_id: null,
  agent_profile_name: null,
  external_id: null,
  preferred_voice: null,
  metadata: {},
  created_at: "2026-06-20T09:00:00Z",
  updated_at: "2026-06-20T09:00:00Z",
};

function renderDialog(node: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>);
}

beforeEach(() => {
  postMock.mockReset();
  patchMock.mockReset();
});
afterEach(() => vi.clearAllMocks());

describe("ContactFormDialog — create", () => {
  it("requires a valid E.164 phone and POSTs the contact", async () => {
    const user = userEvent.setup();
    postMock.mockResolvedValue({ ...existing, name: "New Person" });
    const onClose = vi.fn();
    renderDialog(<ContactFormDialog mode="create" onClose={onClose} />);

    await user.type(screen.getByLabelText("Name"), "New Person");
    await user.type(screen.getByLabelText(/Phone/), "555");
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(postMock).not.toHaveBeenCalled();
    expect(screen.getByText(/E\.164/)).toBeInTheDocument();

    await user.clear(screen.getByLabelText(/Phone/));
    await user.type(screen.getByLabelText(/Phone/), "+19495551234");
    await user.type(screen.getByLabelText("Timezone"), "America/New_York");
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/v1/admin/contacts", {
        name: "New Person",
        phone_e164: "+19495551234",
        timezone: "America/New_York",
      }),
    );
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("surfaces a 409 duplicate as an inline error", async () => {
    const user = userEvent.setup();
    const { ApiError } = await import("../lib/api");
    postMock.mockRejectedValue(new ApiError(409, "phone already in use"));
    renderDialog(<ContactFormDialog mode="create" onClose={vi.fn()} />);
    await user.type(screen.getByLabelText("Name"), "Dup");
    await user.type(screen.getByLabelText(/Phone/), "+19495551234");
    await user.type(screen.getByLabelText("Timezone"), "America/New_York");
    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(screen.getByText("phone already in use")).toBeInTheDocument());
  });
});

describe("ContactFormDialog — edit", () => {
  it("starts the phone field empty and OMITS phone_e164 when left blank", async () => {
    const user = userEvent.setup();
    patchMock.mockResolvedValue(existing);
    renderDialog(<ContactFormDialog mode="edit" contact={existing} onClose={vi.fn()} />);

    const phone = screen.getByLabelText(/Phone/) as HTMLInputElement;
    expect(phone.value).toBe("");
    expect(screen.getByText(/\*\*\*4567/)).toBeInTheDocument();

    await user.clear(screen.getByLabelText("Name"));
    await user.type(screen.getByLabelText("Name"), "Edna M.");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(1));
    const [url, body] = patchMock.mock.calls[0];
    expect(url).toBe(`/v1/admin/contacts/${existing.id}`);
    expect(body).toMatchObject({ name: "Edna M." });
    expect("phone_e164" in body).toBe(false);
  });

  it("rejects an unparseable metadata JSON before submit", async () => {
    const user = userEvent.setup();
    renderDialog(<ContactFormDialog mode="edit" contact={existing} onClose={vi.fn()} />);
    await user.type(screen.getByLabelText(/Metadata/), "{not json");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(patchMock).not.toHaveBeenCalled();
    expect(screen.getByText(/valid JSON object/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 3: Run it to verify it fails**

Run: `cd apps/admin-ui && npx vitest run src/test/ContactFormDialog.test.tsx`
Expected: FAIL (cannot resolve `ContactFormDialog`).

- [ ] **Step 4: Add the contact query/mutation hooks to `features/contacts/hooks.ts`**

Replace the top type import and append the new hooks in `apps/admin-ui/src/features/contacts/hooks.ts`:

```typescript
// replace:
// import type { ContactSummary } from "../../types/api";
import type {
  ContactCreate,
  ContactDetail,
  ContactSummary,
  ContactUpdate,
} from "../../types/api";

// ... existing useContacts / useAssignProfile / useSetTimezone stay unchanged ...

export function useContact(id: string) {
  return useQuery<ContactDetail>({
    queryKey: [...CONTACTS_KEY, "detail", id],
    queryFn: () => api.get<ContactDetail>(`/v1/admin/contacts/${id}`),
  });
}

export function useCreateContact() {
  const qc = useQueryClient();
  return useMutation<ContactDetail, ApiError, ContactCreate>({
    mutationFn: (body) => api.post<ContactDetail>("/v1/admin/contacts", body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: CONTACTS_KEY });
    },
    // 409 (dup phone/external_id) / 422 surface inline in the dialog, so no toast here.
  });
}

export function useUpdateContact() {
  const qc = useQueryClient();
  return useMutation<ContactDetail, ApiError, { id: string; body: ContactUpdate }>({
    mutationFn: ({ id, body }) => api.patch<ContactDetail>(`/v1/admin/contacts/${id}`, body),
    onSuccess: (_data, { id }) => {
      void qc.invalidateQueries({ queryKey: CONTACTS_KEY });
      void qc.invalidateQueries({ queryKey: [...CONTACTS_KEY, "detail", id] });
    },
  });
}
```

- [ ] **Step 5: Implement `ContactFormDialog.tsx`**

Create `apps/admin-ui/src/features/contacts/ContactFormDialog.tsx`:

```tsx
import { useState, type FormEvent } from "react";
import { Dialog } from "../../components/ui/dialog";
import { Input } from "../../components/ui/input";
import { Textarea } from "../../components/ui/textarea";
import { Button } from "../../components/ui/button";
import type { ApiError } from "../../lib/api";
import type { ContactCreate, ContactDetail, ContactUpdate } from "../../types/api";
import { useCreateContact, useUpdateContact } from "./hooks";

const E164 = /^\+[1-9]\d{7,14}$/;

function Field({ htmlFor, children }: { htmlFor: string; children: string }) {
  return (
    <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor={htmlFor}>
      {children}
    </label>
  );
}

interface ContactFormDialogProps {
  mode: "create" | "edit";
  contact?: ContactDetail; // required when mode === "edit"
  onClose: () => void;
}

// Hand-rolled useState form (codebase convention — no RHF). On edit the phone field
// starts EMPTY (the raw number is never sent to the browser); leaving it blank omits
// phone_e164 from the PATCH. Server 409/422 surface inline below the form.
export function ContactFormDialog({ mode, contact, onClose }: ContactFormDialogProps) {
  const isEdit = mode === "edit";
  const [name, setName] = useState(contact?.name ?? "");
  const [phone, setPhone] = useState(""); // always starts blank
  const [timezone, setTimezone] = useState(contact?.timezone ?? "");
  const [externalId, setExternalId] = useState(contact?.external_id ?? "");
  const [preferredVoice, setPreferredVoice] = useState(contact?.preferred_voice ?? "");
  const [metadataText, setMetadataText] = useState(
    contact && Object.keys(contact.metadata).length > 0
      ? JSON.stringify(contact.metadata, null, 2)
      : "",
  );
  const [localError, setLocalError] = useState<string | null>(null);

  const create = useCreateContact();
  const update = useUpdateContact();
  const busy = create.isPending || update.isPending;
  const serverError =
    (create.error as ApiError | null)?.detail ?? (update.error as ApiError | null)?.detail;

  function parseMetadata(): Record<string, unknown> | undefined {
    const t = metadataText.trim();
    if (t.length === 0) return undefined;
    let parsed: unknown;
    try {
      parsed = JSON.parse(t);
    } catch {
      throw new Error("Metadata must be a valid JSON object.");
    }
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      throw new Error("Metadata must be a valid JSON object.");
    }
    return parsed as Record<string, unknown>;
  }

  function handleSubmit(e: FormEvent): void {
    e.preventDefault();
    setLocalError(null);
    const trimmedName = name.trim();
    if (trimmedName.length === 0) {
      setLocalError("Name is required.");
      return;
    }
    const wantsPhone = phone.trim().length > 0;
    if ((!isEdit || wantsPhone) && !E164.test(phone.trim())) {
      setLocalError("Phone must be E.164 format, e.g. +19495551234.");
      return;
    }
    let metadata: Record<string, unknown> | undefined;
    try {
      metadata = parseMetadata();
    } catch (err) {
      setLocalError((err as Error).message);
      return;
    }

    if (isEdit && contact) {
      const body: ContactUpdate = { name: trimmedName };
      if (wantsPhone) body.phone_e164 = phone.trim();
      if (timezone.trim()) body.timezone = timezone.trim();
      body.external_id = externalId.trim() || null;
      body.preferred_voice = preferredVoice.trim() || null;
      if (metadata !== undefined) body.metadata = metadata;
      update.mutate({ id: contact.id, body }, { onSuccess: onClose });
      return;
    }

    const body: ContactCreate = {
      name: trimmedName,
      phone_e164: phone.trim(),
      timezone: timezone.trim(),
    };
    if (externalId.trim()) body.external_id = externalId.trim();
    if (preferredVoice.trim()) body.preferred_voice = preferredVoice.trim();
    if (metadata !== undefined) body.metadata = metadata;
    create.mutate(body, { onSuccess: onClose });
  }

  return (
    <Dialog open onClose={onClose} title={isEdit ? "Edit contact" : "New contact"}>
      <form onSubmit={handleSubmit} className="space-y-3">
        <div>
          <Field htmlFor="cf-name">Name</Field>
          <Input id="cf-name" value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div>
          <Field htmlFor="cf-phone">Phone (E.164)</Field>
          <Input
            id="cf-phone"
            placeholder="+19495551234"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
          />
          {isEdit ? (
            <p className="mt-1 text-xs text-slate-500">
              Current: {contact?.masked_phone}. Leave blank to keep it; enter a full number to
              replace.
            </p>
          ) : null}
        </div>
        <div>
          <Field htmlFor="cf-tz">Timezone</Field>
          <Input
            id="cf-tz"
            placeholder="America/New_York"
            value={timezone}
            onChange={(e) => setTimezone(e.target.value)}
          />
        </div>
        <div>
          <Field htmlFor="cf-ext">External ID</Field>
          <Input id="cf-ext" value={externalId} onChange={(e) => setExternalId(e.target.value)} />
        </div>
        <div>
          <Field htmlFor="cf-voice">Preferred voice</Field>
          <Input
            id="cf-voice"
            value={preferredVoice}
            onChange={(e) => setPreferredVoice(e.target.value)}
          />
        </div>
        <div>
          <Field htmlFor="cf-meta">Metadata (advanced, JSON object)</Field>
          <Textarea
            id="cf-meta"
            rows={3}
            placeholder="{}"
            value={metadataText}
            onChange={(e) => setMetadataText(e.target.value)}
          />
        </div>
        {localError ? <p className="text-xs font-medium text-red-700">{localError}</p> : null}
        {serverError ? <p className="text-xs font-medium text-red-700">{serverError}</p> : null}
        <div className="mt-5 flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy}>
            {isEdit ? (busy ? "Saving…" : "Save") : busy ? "Creating…" : "Create"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
```

- [ ] **Step 6: Run the `ContactFormDialog` test to verify it passes**

Run: `cd apps/admin-ui && npx vitest run src/test/ContactFormDialog.test.tsx`
Expected: PASS (4 tests).

- [ ] **Step 7: Wire `+ New contact` and detail links into `ContactsPage.tsx`**

In `apps/admin-ui/src/features/contacts/ContactsPage.tsx`:
1. Add imports: `import { Link } from "react-router-dom";` and `import { ContactFormDialog } from "./ContactFormDialog";` (`useState` is already imported).
2. Add `const [createOpen, setCreateOpen] = useState(false);` near the other state.
3. Replace the header block:

```tsx
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="font-display text-2xl text-ink-strong">Contacts</h1>
        <Button onClick={() => setCreateOpen(true)}>+ New contact</Button>
      </div>
```

4. Make the name cell a link:

```tsx
              <Td className="font-medium text-slate-900">
                <Link className="text-accent hover:underline" to={`/contacts/${e.id}`}>
                  {e.name}
                </Link>
              </Td>
```

5. Render the create dialog at the end of the outer `<div className="space-y-4">` (before its closing tag):

```tsx
      {createOpen ? (
        <ContactFormDialog mode="create" onClose={() => setCreateOpen(false)} />
      ) : null}
```

- [ ] **Step 8: Extend `ContactsPage.test.tsx`**

The existing test mocks only `get`/`put`. Add `post` to the api mock: declare `const postMock = vi.fn();`, add `post: (u: string, b?: unknown) => postMock(u, b)` to the `vi.mock("../lib/api", …)` object, and `postMock.mockReset()` in `beforeEach`. Then append:

```tsx
  it("shows the New contact button for admins and opens the create dialog", async () => {
    renderPage();
    const btn = await screen.findByRole("button", { name: "+ New contact" });
    await userEvent.click(btn);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create" })).toBeInTheDocument();
  });

  it("links each contact name to its detail page", async () => {
    renderPage();
    const link = await screen.findByRole("link", { name: "Edna Moore" });
    expect(link).toHaveAttribute("href", "/contacts/11111111-1111-1111-1111-111111111111");
  });
```

(The existing test already wraps the page in `<MemoryRouter>`, so `<Link>` resolves.)

- [ ] **Step 9: Run the contacts tests**

Run: `cd apps/admin-ui && npx vitest run src/test/ContactsPage.test.tsx src/test/ContactFormDialog.test.tsx`
Expected: PASS (all).

- [ ] **Step 10: Lint + commit**

```bash
cd apps/admin-ui && npm run lint
git add apps/admin-ui/src/types/api.ts apps/admin-ui/src/features/contacts/hooks.ts apps/admin-ui/src/features/contacts/ContactFormDialog.tsx apps/admin-ui/src/features/contacts/ContactsPage.tsx apps/admin-ui/src/test/ContactFormDialog.test.tsx apps/admin-ui/src/test/ContactsPage.test.tsx
git commit -m "feat(admin-ui): contact create/edit dialog + New-contact wiring"
```

---

## Task 3: Contact detail page + delete

**Files:**
- Modify: `apps/admin-ui/src/features/contacts/hooks.ts` (add `useDeleteContact`)
- Create: `apps/admin-ui/src/features/contacts/ContactDetailPage.tsx`
- Modify: `apps/admin-ui/src/routes.tsx` (add `/contacts/:id` under `RequireAdmin`)
- Test: `apps/admin-ui/src/test/ContactDetailPage.test.tsx`

**Interfaces:**
- Consumes: `useContact` / `ContactFormDialog` (Task 2), `ConfirmDialog`, `useDeleteContact`.
- Produces (used by Tasks 4, 6): the `/contacts/:id` route renders `ContactDetailPage`, which leaves two explicit extension-point comments — `CALL_NOW_BUTTON` (Task 4) and `SCHEDULES_SECTION` (Task 6).

- [ ] **Step 1: Add `useDeleteContact` to `features/contacts/hooks.ts`**

```typescript
export function useDeleteContact() {
  const qc = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (id) => api.del<void>(`/v1/admin/contacts/${id}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: CONTACTS_KEY });
    },
    // A delete blocked by a dependent record (FK) surfaces its server detail as a toast.
    onError: (err) => pushToast(err.detail),
  });
}
```

- [ ] **Step 2: Write the failing `ContactDetailPage` test**

Create `apps/admin-ui/src/test/ContactDetailPage.test.tsx`:

```tsx
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { ContactDetail } from "../types/api";
import { meFixture } from "./meFixture";

const getMock = vi.fn();
const delMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    get: (u: string) => getMock(u),
    post: vi.fn(),
    patch: vi.fn(),
    del: (u: string) => delMock(u),
  },
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      public detail: string,
    ) {
      super(detail);
    }
  },
}));
vi.mock("../components/ui/toast", () => ({ pushToast: vi.fn() }));

import { ContactDetailPage } from "../features/contacts/ContactDetailPage";

const detail: ContactDetail = {
  id: "11111111-1111-1111-1111-111111111111",
  name: "Edna Moore",
  masked_phone: "***4567",
  timezone: "America/New_York",
  agent_profile_id: null,
  agent_profile_name: null,
  external_id: "EHR-9",
  preferred_voice: null,
  metadata: {},
  created_at: "2026-06-20T09:00:00Z",
  updated_at: "2026-06-20T09:00:00Z",
};

function routeGet(url: string): Promise<unknown> {
  if (url === "/v1/auth/me") return Promise.resolve(meFixture("admin"));
  if (url === `/v1/admin/contacts/${detail.id}`) return Promise.resolve(detail);
  if (url.startsWith("/v1/admin/schedules")) return Promise.resolve([]);
  if (url.startsWith("/v1/admin/profiles")) return Promise.resolve([]);
  return Promise.reject(new Error(`unexpected GET ${url}`));
}

function renderAt(id: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/contacts/${id}`]}>
        <Routes>
          <Route path="/contacts/:id" element={<ContactDetailPage />} />
          <Route path="/contacts" element={<div>contacts list</div>} />
          <Route path="/calls" element={<div>calls page</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getMock.mockReset();
  delMock.mockReset();
  getMock.mockImplementation(routeGet);
});
afterEach(() => vi.clearAllMocks());

describe("ContactDetailPage", () => {
  it("renders the contact header with masked phone", async () => {
    renderAt(detail.id);
    expect(await screen.findByText("Edna Moore")).toBeInTheDocument();
    expect(screen.getByText("***4567")).toBeInTheDocument();
  });

  it("delete asks for confirmation, then DELETEs", async () => {
    const user = userEvent.setup();
    delMock.mockResolvedValue(undefined);
    renderAt(detail.id);
    await screen.findByText("Edna Moore");
    await user.click(screen.getByRole("button", { name: "Delete" }));
    expect(delMock).not.toHaveBeenCalled(); // confirm first
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(delMock).toHaveBeenCalledWith(`/v1/admin/contacts/${detail.id}`));
  });

  it("shows a not-found state for a missing contact", async () => {
    const { ApiError } = await import("../lib/api");
    getMock.mockImplementation((url: string) => {
      if (url === "/v1/auth/me") return Promise.resolve(meFixture("admin"));
      if (url.startsWith("/v1/admin/contacts/"))
        return Promise.reject(new ApiError(404, "not found"));
      if (url.startsWith("/v1/admin/schedules")) return Promise.resolve([]);
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    renderAt("00000000-0000-0000-0000-000000000000");
    expect(await screen.findByText(/Contact not found/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 3: Run it to verify it fails**

Run: `cd apps/admin-ui && npx vitest run src/test/ContactDetailPage.test.tsx`
Expected: FAIL (cannot resolve `ContactDetailPage`).

- [ ] **Step 4: Implement `ContactDetailPage.tsx`**

Create `apps/admin-ui/src/features/contacts/ContactDetailPage.tsx`:

```tsx
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Button } from "../../components/ui/button";
import { Spinner } from "../../components/ui/spinner";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { useIsAdmin } from "../../auth/useSession";
import { ContactFormDialog } from "./ContactFormDialog";
import { useContact, useDeleteContact } from "./hooks";

// Per-contact home: header + edit/delete + call-now + schedules. Call-now (Task 4)
// and the schedules section (Task 6) mount at the marked extension points.
export function ContactDetailPage() {
  const isAdmin = useIsAdmin();
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const contact = useContact(id);
  const del = useDeleteContact();
  const [editOpen, setEditOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  if (!isAdmin) return <p className="text-sm text-muted">Admins only.</p>;
  if (contact.isLoading) {
    return (
      <div className="flex items-center gap-2 text-muted">
        <Spinner /> Loading contact…
      </div>
    );
  }
  if (contact.isError || !contact.data) {
    return (
      <div className="space-y-2">
        <p className="text-sm text-red-700">Contact not found.</p>
        <Link className="text-sm text-accent hover:underline" to="/contacts">
          ← Back to contacts
        </Link>
      </div>
    );
  }

  const c = contact.data;
  return (
    <div className="space-y-5">
      <Link className="text-sm text-accent hover:underline" to="/contacts">
        ← Contacts
      </Link>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="font-display text-2xl text-ink-strong">{c.name}</h1>
          <p className="font-mono text-sm text-faint">{c.masked_phone}</p>
          <p className="text-sm text-muted">
            {c.timezone}
            {c.agent_profile_name ? ` · ${c.agent_profile_name}` : ""}
            {c.external_id ? ` · ${c.external_id}` : ""}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {/* CALL_NOW_BUTTON — Task 4 inserts the "Call now" button here. */}
          <Button variant="secondary" onClick={() => setEditOpen(true)}>
            Edit
          </Button>
          <Button variant="danger" onClick={() => setConfirmDelete(true)}>
            Delete
          </Button>
        </div>
      </div>

      {/* SCHEDULES_SECTION — Task 6 mounts <ScheduleSection contactId={c.id} /> here. */}

      <div>
        <Link className="text-sm text-accent hover:underline" to={`/calls?contact_id=${c.id}`}>
          Recent calls →
        </Link>
      </div>

      {editOpen ? (
        <ContactFormDialog mode="edit" contact={c} onClose={() => setEditOpen(false)} />
      ) : null}
      <ConfirmDialog
        open={confirmDelete}
        title="Delete contact?"
        body={
          <>
            Delete <strong>{c.name}</strong>? Their schedules are removed and past calls are kept
            but no longer linked to a name. This cannot be undone.
          </>
        }
        confirmLabel="Delete"
        busy={del.isPending}
        onCancel={() => setConfirmDelete(false)}
        onConfirm={() =>
          del.mutate(c.id, {
            onSuccess: () => {
              setConfirmDelete(false);
              navigate("/contacts");
            },
          })
        }
      />
    </div>
  );
}
```

- [ ] **Step 5: Add the route**

In `apps/admin-ui/src/routes.tsx`: add `import { ContactDetailPage } from "./features/contacts/ContactDetailPage";` and, immediately after the existing `contacts` route object, add:

```tsx
          {
            path: "contacts/:id",
            element: (
              <RequireAdmin>
                <ContactDetailPage />
              </RequireAdmin>
            ),
          },
```

- [ ] **Step 6: Run the detail test to verify it passes**

Run: `cd apps/admin-ui && npx vitest run src/test/ContactDetailPage.test.tsx`
Expected: PASS (3 tests).

- [ ] **Step 7: Lint + commit**

```bash
cd apps/admin-ui && npm run lint
git add apps/admin-ui/src/features/contacts/hooks.ts apps/admin-ui/src/features/contacts/ContactDetailPage.tsx apps/admin-ui/src/routes.tsx apps/admin-ui/src/test/ContactDetailPage.test.tsx
git commit -m "feat(admin-ui): contact detail page with edit + delete"
```

---

## Task 4: Call-now dialog

**Files:**
- Modify: `apps/admin-ui/src/types/api.ts` (add `AdminCreateCallRequest`, `CallResponse`)
- Modify: `apps/admin-ui/src/features/contacts/hooks.ts` (add `useCallNow`)
- Create: `apps/admin-ui/src/features/contacts/CallNowDialog.tsx`
- Modify: `apps/admin-ui/src/features/contacts/ContactDetailPage.tsx` (insert at `CALL_NOW_BUTTON`)
- Test: `apps/admin-ui/src/test/CallNowDialog.test.tsx`

**Interfaces:**
- Consumes: `KeyValueEditor` / `rowsToRecord` + `dynamicVarsByteSize` / `DYNAMIC_VARS_MAX_BYTES` (Task 1); `useProfiles` (existing); `ContactDetail`.
- Produces: `useCallNow()`, `<CallNowDialog contact onClose />`.

- [ ] **Step 1: Add call types to `types/api.ts`**

```typescript
// Admin ad-hoc outbound (admin_calls.py: AdminCreateCallRequest). idempotency_key
// is minted server-side, so it is NOT sent.
export interface AdminCreateCallRequest {
  contact_id: string;
  dynamic_vars?: Record<string, string>;
  profile_override?: string | null;
}

// POST /v1/admin/calls returns the full CallResponse; the UI reads this subset.
// status is "queued" on a normal enqueue and "dnc_blocked" when the number is on
// the DNC list (HTTP 200, not an error).
export interface CallResponse {
  id: string;
  contact_id: string | null;
  direction: string;
  status: string;
  created_at: string;
}
```

- [ ] **Step 2: Write the failing `CallNowDialog` test**

Create `apps/admin-ui/src/test/CallNowDialog.test.tsx`:

```tsx
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ContactDetail } from "../types/api";

const postMock = vi.fn();
const getMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    get: (u: string) => getMock(u),
    post: (u: string, b?: unknown) => postMock(u, b),
  },
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      public detail: string,
    ) {
      super(detail);
    }
  },
}));
const pushToastMock = vi.fn();
vi.mock("../components/ui/toast", () => ({
  pushToast: (m: string, t?: string) => pushToastMock(m, t),
}));

import { CallNowDialog } from "../features/contacts/CallNowDialog";

const contact: ContactDetail = {
  id: "11111111-1111-1111-1111-111111111111",
  name: "Edna Moore",
  masked_phone: "***4567",
  timezone: "America/New_York",
  agent_profile_id: null,
  agent_profile_name: null,
  external_id: null,
  preferred_voice: null,
  metadata: {},
  created_at: "2026-06-20T09:00:00Z",
  updated_at: "2026-06-20T09:00:00Z",
};

function renderDialog() {
  getMock.mockImplementation((u: string) =>
    u.startsWith("/v1/admin/profiles") ? Promise.resolve([]) : Promise.reject(new Error(u)),
  );
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <CallNowDialog contact={contact} onClose={vi.fn()} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  postMock.mockReset();
  getMock.mockReset();
  pushToastMock.mockReset();
});
afterEach(() => vi.clearAllMocks());

describe("CallNowDialog", () => {
  it("disables Call until the out-of-window ack is checked", async () => {
    const user = userEvent.setup();
    postMock.mockResolvedValue({
      id: "c1",
      contact_id: contact.id,
      direction: "outbound",
      status: "queued",
      created_at: "",
    });
    renderDialog();
    const callBtn = await screen.findByRole("button", { name: /^Call/ });
    expect(callBtn).toBeDisabled();
    await user.click(screen.getByLabelText(/outside their normal window/i));
    expect(callBtn).toBeEnabled();
    await user.click(callBtn);
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/v1/admin/calls", { contact_id: contact.id }),
    );
  });

  it("surfaces a DNC-blocked result inline (not as an error)", async () => {
    const user = userEvent.setup();
    postMock.mockResolvedValue({
      id: "c2",
      contact_id: contact.id,
      direction: "outbound",
      status: "dnc_blocked",
      created_at: "",
    });
    renderDialog();
    await user.click(await screen.findByLabelText(/outside their normal window/i));
    await user.click(screen.getByRole("button", { name: /^Call/ }));
    expect(await screen.findByText(/Do-Not-Call list/)).toBeInTheDocument();
  });

  it("surfaces a 503 (telephony unavailable) inline", async () => {
    const user = userEvent.setup();
    const { ApiError } = await import("../lib/api");
    postMock.mockRejectedValue(new ApiError(503, "outbound calling is not available"));
    renderDialog();
    await user.click(await screen.findByLabelText(/outside their normal window/i));
    await user.click(screen.getByRole("button", { name: /^Call/ }));
    expect(await screen.findByText("outbound calling is not available")).toBeInTheDocument();
  });
});
```

- [ ] **Step 3: Run it to verify it fails**

Run: `cd apps/admin-ui && npx vitest run src/test/CallNowDialog.test.tsx`
Expected: FAIL (cannot resolve `CallNowDialog`).

- [ ] **Step 4: Add `useCallNow` to `features/contacts/hooks.ts`**

Extend the type import to include `AdminCreateCallRequest, CallResponse`, then add:

```typescript
// Ad-hoc call. Returns the CallResponse (status may be "dnc_blocked"). Errors
// (404/422/503) are surfaced by the dialog, so no toast here.
export function useCallNow() {
  return useMutation<CallResponse, ApiError, AdminCreateCallRequest>({
    mutationFn: (body) => api.post<CallResponse>("/v1/admin/calls", body),
  });
}
```

- [ ] **Step 5: Implement `CallNowDialog.tsx`**

Create `apps/admin-ui/src/features/contacts/CallNowDialog.tsx`:

```tsx
import { useState } from "react";
import { Dialog } from "../../components/ui/dialog";
import { Select } from "../../components/ui/select";
import { Button } from "../../components/ui/button";
import { KeyValueEditor, rowsToRecord, type KvRow } from "../../components/ui/KeyValueEditor";
import { DYNAMIC_VARS_MAX_BYTES, dynamicVarsByteSize } from "../../lib/dynamicVars";
import { pushToast } from "../../components/ui/toast";
import type { ApiError } from "../../lib/api";
import type { AdminCreateCallRequest, ContactDetail } from "../../types/api";
import { useProfiles } from "../profiles/hooks";
import { useCallNow } from "./hooks";

interface CallNowDialogProps {
  contact: ContactDetail;
  onClose: () => void;
}

// Ad-hoc outbound call. The required ack is a deliberate speed bump: the server
// does NOT enforce quiet-hours, so the operator confirms they mean to call outside
// the contact's window. DNC is a hard server block surfaced inline (not an error).
export function CallNowDialog({ contact, onClose }: CallNowDialogProps) {
  const [ack, setAck] = useState(false);
  const [override, setOverride] = useState("");
  const [rows, setRows] = useState<KvRow[]>([]);
  const [localError, setLocalError] = useState<string | null>(null);
  const [blocked, setBlocked] = useState(false);
  const profiles = useProfiles();
  const callNow = useCallNow();
  const serverError = (callNow.error as ApiError | null)?.detail ?? null;

  const live = (profiles.data ?? []).filter((p) => p.status === "active");

  function submit() {
    setLocalError(null);
    setBlocked(false);
    const vars = rowsToRecord(rows);
    if (dynamicVarsByteSize(vars) > DYNAMIC_VARS_MAX_BYTES) {
      setLocalError(`Variables exceed the ${DYNAMIC_VARS_MAX_BYTES}-byte limit.`);
      return;
    }
    const body: AdminCreateCallRequest = { contact_id: contact.id };
    if (Object.keys(vars).length > 0) body.dynamic_vars = vars;
    if (override) body.profile_override = override;
    callNow.mutate(body, {
      onSuccess: (call) => {
        if (call.status === "dnc_blocked") {
          setBlocked(true);
          return;
        }
        pushToast("Call queued.", "info");
        onClose();
      },
    });
  }

  return (
    <Dialog open onClose={onClose} title={`Call ${contact.name}`}>
      <div className="space-y-3">
        <p className="text-sm text-muted">
          Calling <span className="font-mono">{contact.masked_phone}</span> now.
        </p>
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="cn-override">
            Profile override (optional)
          </label>
          <Select id="cn-override" value={override} onChange={(e) => setOverride(e.target.value)}>
            <option value="">— use assigned/default —</option>
            {live.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </Select>
        </div>
        <KeyValueEditor rows={rows} onChange={setRows} label="Call variables" />
        <label className="flex items-start gap-2 text-sm text-slate-700">
          <input
            type="checkbox"
            checked={ack}
            onChange={(e) => setAck(e.target.checked)}
            aria-label="acknowledge outside their normal window"
          />
          I understand this calls the contact <strong>outside their normal window</strong>.
        </label>
        {blocked ? (
          <p className="text-sm font-medium text-red-700">
            This number is on the Do-Not-Call list; the call was blocked.
          </p>
        ) : null}
        {localError ? <p className="text-xs font-medium text-red-700">{localError}</p> : null}
        {serverError ? <p className="text-xs font-medium text-red-700">{serverError}</p> : null}
        <div className="mt-5 flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={callNow.isPending}>
            Cancel
          </Button>
          <Button type="button" onClick={submit} disabled={!ack || callNow.isPending}>
            {callNow.isPending ? "Calling…" : "Call now"}
          </Button>
        </div>
      </div>
    </Dialog>
  );
}
```

- [ ] **Step 6: Insert the Call-now button into `ContactDetailPage.tsx`**

Add `import { CallNowDialog } from "./CallNowDialog";` and `const [callOpen, setCallOpen] = useState(false);`. Replace the `{/* CALL_NOW_BUTTON … */}` comment with:

```tsx
          <Button onClick={() => setCallOpen(true)}>📞 Call now</Button>
```

and render the dialog near the others:

```tsx
      {callOpen ? <CallNowDialog contact={c} onClose={() => setCallOpen(false)} /> : null}
```

- [ ] **Step 7: Run the call-now test to verify it passes**

Run: `cd apps/admin-ui && npx vitest run src/test/CallNowDialog.test.tsx`
Expected: PASS (3 tests).

- [ ] **Step 8: Lint + commit**

```bash
cd apps/admin-ui && npm run lint
git add apps/admin-ui/src/types/api.ts apps/admin-ui/src/features/contacts/hooks.ts apps/admin-ui/src/features/contacts/CallNowDialog.tsx apps/admin-ui/src/features/contacts/ContactDetailPage.tsx apps/admin-ui/src/test/CallNowDialog.test.tsx
git commit -m "feat(admin-ui): call-now dialog with out-of-window ack + DNC/503 surfacing"
```

---

## Task 5: Schedule form dialog + schedules hooks/types

**Files:**
- Modify: `apps/admin-ui/src/types/api.ts` (add `ScheduleSlot`, `ScheduleResponse`, `CreateScheduleRequest`, `UpdateScheduleRequest`)
- Create: `apps/admin-ui/src/features/schedules/hooks.ts`
- Create: `apps/admin-ui/src/features/schedules/ScheduleFormDialog.tsx`
- Test: `apps/admin-ui/src/test/ScheduleFormDialog.test.tsx`

**Interfaces:**
- Consumes: `KeyValueEditor` / `rowsToRecord` / `recordToRows` + `DaysOfWeekPicker` (Task 1); `Weekday` (Task 1); `useProfiles`.
- Produces (used by Tasks 6, 7):
  - types above
  - `SCHEDULES_KEY`, `ScheduleFilters`, `useSchedules(filters, limit?, offset?)`, `useContactSchedules(contactId)`, `useCreateSchedule()`, `useUpdateSchedule()`, `useDeleteSchedule()`
  - `<ScheduleFormDialog mode contactId schedule? existingSlots onClose />`

- [ ] **Step 1: Add schedule types to `types/api.ts`**

Append (note `Weekday` already added in Task 1):

```typescript
export type ScheduleSlot = "morning" | "evening";

export interface ScheduleResponse {
  id: string;
  contact_id: string;
  slot: ScheduleSlot;
  enabled: boolean;
  window_start_local: string; // "HH:MM:SS"
  window_end_local: string; // "HH:MM:SS"
  days_of_week: Weekday[];
  dynamic_vars: Record<string, unknown>;
  profile_override: string | null;
  next_run_at: string;
  last_materialized_date: string | null;
  last_result: string | null;
  last_result_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface CreateScheduleRequest {
  contact_id: string;
  slot?: ScheduleSlot;
  window_start_local: string; // "HH:MM" accepted by the server
  window_end_local: string;
  days_of_week?: Weekday[];
  enabled?: boolean;
  dynamic_vars?: Record<string, string>;
  profile_override?: string | null;
}

export interface UpdateScheduleRequest {
  enabled?: boolean;
  window_start_local?: string;
  window_end_local?: string;
  days_of_week?: Weekday[];
  dynamic_vars?: Record<string, string>;
  profile_override?: string | null;
}
```

- [ ] **Step 2: Implement `features/schedules/hooks.ts`** (exercised via Tasks 6/7 component tests)

Create `apps/admin-ui/src/features/schedules/hooks.ts`:

```typescript
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { ApiError } from "../../lib/api";
import { pushToast } from "../../components/ui/toast";
import type {
  CreateScheduleRequest,
  ScheduleResponse,
  UpdateScheduleRequest,
} from "../../types/api";

export const SCHEDULES_KEY = ["schedules"] as const;

export interface ScheduleFilters {
  contactId?: string;
  slot?: string;
  lastResult?: string;
}

export function useSchedules(filters: ScheduleFilters, limit = 100, offset = 0) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (filters.contactId) params.set("contact_id", filters.contactId);
  if (filters.slot) params.set("slot", filters.slot);
  if (filters.lastResult) params.set("last_result", filters.lastResult);
  return useQuery<ScheduleResponse[]>({
    queryKey: [...SCHEDULES_KEY, filters, limit, offset],
    queryFn: () => api.get<ScheduleResponse[]>(`/v1/admin/schedules?${params.toString()}`),
  });
}

export function useContactSchedules(contactId: string) {
  return useSchedules({ contactId });
}

export function useCreateSchedule() {
  const qc = useQueryClient();
  return useMutation<ScheduleResponse, ApiError, CreateScheduleRequest>({
    mutationFn: (body) => api.post<ScheduleResponse>("/v1/admin/schedules", body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: SCHEDULES_KEY }),
  });
}

export function useUpdateSchedule() {
  const qc = useQueryClient();
  return useMutation<ScheduleResponse, ApiError, { id: string; body: UpdateScheduleRequest }>({
    mutationFn: ({ id, body }) => api.patch<ScheduleResponse>(`/v1/admin/schedules/${id}`, body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: SCHEDULES_KEY }),
  });
}

export function useDeleteSchedule() {
  const qc = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (id) => api.del<void>(`/v1/admin/schedules/${id}`),
    onSuccess: () => void qc.invalidateQueries({ queryKey: SCHEDULES_KEY }),
    onError: (err) => pushToast(err.detail),
  });
}
```

- [ ] **Step 3: Write the failing `ScheduleFormDialog` test**

Create `apps/admin-ui/src/test/ScheduleFormDialog.test.tsx`:

```tsx
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import type { ScheduleResponse } from "../types/api";

const postMock = vi.fn();
const patchMock = vi.fn();
const getMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    get: (u: string) => getMock(u),
    post: (u: string, b?: unknown) => postMock(u, b),
    patch: (u: string, b?: unknown) => patchMock(u, b),
  },
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      public detail: string,
    ) {
      super(detail);
    }
  },
}));
vi.mock("../components/ui/toast", () => ({ pushToast: vi.fn() }));

import { ScheduleFormDialog } from "../features/schedules/ScheduleFormDialog";

const sched: ScheduleResponse = {
  id: "s1",
  contact_id: "c1",
  slot: "morning",
  enabled: true,
  window_start_local: "09:00:00",
  window_end_local: "11:00:00",
  days_of_week: ["monday", "tuesday"],
  dynamic_vars: {},
  profile_override: null,
  next_run_at: "2026-06-24T13:00:00Z",
  last_materialized_date: null,
  last_result: null,
  last_result_at: null,
  created_at: "",
  updated_at: "",
};

function renderDialog(node: ReactNode) {
  getMock.mockImplementation((u: string) =>
    u.startsWith("/v1/admin/profiles") ? Promise.resolve([]) : Promise.reject(new Error(u)),
  );
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>);
}

beforeEach(() => {
  postMock.mockReset();
  patchMock.mockReset();
  getMock.mockReset();
});
afterEach(() => vi.clearAllMocks());

describe("ScheduleFormDialog — create", () => {
  it("blocks an inverted window", async () => {
    const user = userEvent.setup();
    renderDialog(
      <ScheduleFormDialog mode="create" contactId="c1" existingSlots={[]} onClose={vi.fn()} />,
    );
    await user.clear(screen.getByLabelText("Window start"));
    await user.type(screen.getByLabelText("Window start"), "11:00");
    await user.clear(screen.getByLabelText("Window end"));
    await user.type(screen.getByLabelText("Window end"), "09:00");
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(postMock).not.toHaveBeenCalled();
    expect(screen.getByText(/start.*before.*end/i)).toBeInTheDocument();
  });

  it("requires at least one day", async () => {
    const user = userEvent.setup();
    renderDialog(
      <ScheduleFormDialog mode="create" contactId="c1" existingSlots={[]} onClose={vi.fn()} />,
    );
    await user.type(screen.getByLabelText("Window start"), "09:00");
    await user.type(screen.getByLabelText("Window end"), "11:00");
    for (const d of [
      "monday",
      "tuesday",
      "wednesday",
      "thursday",
      "friday",
      "saturday",
      "sunday",
    ]) {
      const cb = screen.getByLabelText(d) as HTMLInputElement;
      if (cb.checked) await user.click(cb);
    }
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(postMock).not.toHaveBeenCalled();
    expect(screen.getByText(/at least one day/i)).toBeInTheDocument();
  });

  it("posts a valid morning schedule", async () => {
    const user = userEvent.setup();
    postMock.mockResolvedValue(sched);
    const onClose = vi.fn();
    renderDialog(
      <ScheduleFormDialog mode="create" contactId="c1" existingSlots={[]} onClose={onClose} />,
    );
    await user.type(screen.getByLabelText("Window start"), "09:00");
    await user.type(screen.getByLabelText("Window end"), "11:00");
    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(postMock).toHaveBeenCalledTimes(1));
    const [, body] = postMock.mock.calls[0];
    expect(body).toMatchObject({
      contact_id: "c1",
      slot: "morning",
      window_start_local: "09:00",
      window_end_local: "11:00",
    });
    expect(body.days_of_week.length).toBe(7);
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("only offers the free slot when one slot is taken", async () => {
    renderDialog(
      <ScheduleFormDialog
        mode="create"
        contactId="c1"
        existingSlots={["morning"]}
        onClose={vi.fn()}
      />,
    );
    const slot = screen.getByLabelText("Slot") as HTMLSelectElement;
    const values = Array.from(slot.options).map((o) => o.value);
    expect(values).toEqual(["evening"]);
  });
});

describe("ScheduleFormDialog — edit", () => {
  it("renders the slot read-only and PATCHes window+days together", async () => {
    const user = userEvent.setup();
    patchMock.mockResolvedValue(sched);
    renderDialog(
      <ScheduleFormDialog
        mode="edit"
        contactId="c1"
        schedule={sched}
        existingSlots={["morning"]}
        onClose={vi.fn()}
      />,
    );
    expect(screen.queryByLabelText("Slot")).not.toBeInTheDocument();
    expect(screen.getByText(/morning/i)).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Window end"));
    await user.type(screen.getByLabelText("Window end"), "12:00");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(1));
    const [, body] = patchMock.mock.calls[0];
    expect(body.window_start_local).toBe("09:00");
    expect(body.window_end_local).toBe("12:00");
    expect(body.slot).toBeUndefined();
  });
});
```

- [ ] **Step 4: Run it to verify it fails**

Run: `cd apps/admin-ui && npx vitest run src/test/ScheduleFormDialog.test.tsx`
Expected: FAIL (cannot resolve `ScheduleFormDialog`).

- [ ] **Step 5: Implement `ScheduleFormDialog.tsx`**

Create `apps/admin-ui/src/features/schedules/ScheduleFormDialog.tsx`:

```tsx
import { useState } from "react";
import { Dialog } from "../../components/ui/dialog";
import { Input } from "../../components/ui/input";
import { Select } from "../../components/ui/select";
import { Button } from "../../components/ui/button";
import { DaysOfWeekPicker } from "../../components/ui/DaysOfWeekPicker";
import { KeyValueEditor, recordToRows, rowsToRecord, type KvRow } from "../../components/ui/KeyValueEditor";
import { DYNAMIC_VARS_MAX_BYTES, dynamicVarsByteSize } from "../../lib/dynamicVars";
import type { ApiError } from "../../lib/api";
import type {
  CreateScheduleRequest,
  ScheduleResponse,
  ScheduleSlot,
  UpdateScheduleRequest,
  Weekday,
} from "../../types/api";
import { useProfiles } from "../profiles/hooks";
import { useCreateSchedule, useUpdateSchedule } from "./hooks";

const ALL_DAYS: Weekday[] = [
  "monday",
  "tuesday",
  "wednesday",
  "thursday",
  "friday",
  "saturday",
  "sunday",
];
const ALL_SLOTS: ScheduleSlot[] = ["morning", "evening"];

// "HH:MM" or "HH:MM:SS" -> minutes since midnight, for client-side window checks.
function toMinutes(t: string): number | null {
  const m = /^(\d{2}):(\d{2})(?::\d{2})?$/.exec(t.trim());
  if (!m) return null;
  return Number(m[1]) * 60 + Number(m[2]);
}
const QUIET_START = 9 * 60; // 09:00
const QUIET_END = 21 * 60; // 21:00 (exclusive)

interface ScheduleFormDialogProps {
  mode: "create" | "edit";
  contactId: string;
  schedule?: ScheduleResponse; // required for edit
  existingSlots: ScheduleSlot[]; // slots already taken by this contact
  onClose: () => void;
}

export function ScheduleFormDialog({
  mode,
  contactId,
  schedule,
  existingSlots,
  onClose,
}: ScheduleFormDialogProps) {
  const isEdit = mode === "edit";
  const freeSlots = ALL_SLOTS.filter((s) => !existingSlots.includes(s));
  const [slot, setSlot] = useState<ScheduleSlot>(schedule?.slot ?? freeSlots[0] ?? "morning");
  const [start, setStart] = useState((schedule?.window_start_local ?? "").slice(0, 5));
  const [end, setEnd] = useState((schedule?.window_end_local ?? "").slice(0, 5));
  const [days, setDays] = useState<Weekday[]>(schedule?.days_of_week ?? ALL_DAYS);
  const [enabled, setEnabled] = useState(schedule?.enabled ?? true);
  const [override, setOverride] = useState(schedule?.profile_override ?? "");
  const [rows, setRows] = useState<KvRow[]>(recordToRows(schedule?.dynamic_vars ?? {}));
  const [localError, setLocalError] = useState<string | null>(null);

  const profiles = useProfiles();
  const create = useCreateSchedule();
  const update = useUpdateSchedule();
  const busy = create.isPending || update.isPending;
  const serverError =
    (create.error as ApiError | null)?.detail ?? (update.error as ApiError | null)?.detail;
  const live = (profiles.data ?? []).filter((p) => p.status === "active");

  function validate(): string | null {
    const s = toMinutes(start);
    const e = toMinutes(end);
    if (s === null || e === null) return "Enter a window start and end (HH:MM).";
    if (s >= e) return "Window start must be before the end.";
    if (e <= QUIET_START || s >= QUIET_END) return "Window must fall within 09:00–21:00.";
    if (days.length === 0) return "Pick at least one day.";
    if (dynamicVarsByteSize(rowsToRecord(rows)) > DYNAMIC_VARS_MAX_BYTES) {
      return `Variables exceed the ${DYNAMIC_VARS_MAX_BYTES}-byte limit.`;
    }
    return null;
  }

  function handleSubmit() {
    const err = validate();
    setLocalError(err);
    if (err) return;
    const vars = rowsToRecord(rows);

    if (isEdit && schedule) {
      const body: UpdateScheduleRequest = {
        enabled,
        window_start_local: start,
        window_end_local: end,
        days_of_week: days,
        dynamic_vars: vars,
        profile_override: override || null,
      };
      update.mutate({ id: schedule.id, body }, { onSuccess: onClose });
      return;
    }
    const body: CreateScheduleRequest = {
      contact_id: contactId,
      slot,
      window_start_local: start,
      window_end_local: end,
      days_of_week: days,
      enabled,
      dynamic_vars: vars,
      profile_override: override || null,
    };
    create.mutate(body, { onSuccess: onClose });
  }

  return (
    <Dialog open onClose={onClose} title={isEdit ? "Edit schedule" : "New schedule"}>
      <div className="space-y-3">
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="sf-slot">
            Slot
          </label>
          {isEdit ? (
            <p className="text-sm text-ink">
              {slot} (slot is fixed; delete and recreate to change)
            </p>
          ) : (
            <Select
              id="sf-slot"
              aria-label="Slot"
              value={slot}
              onChange={(e) => setSlot(e.target.value as ScheduleSlot)}
            >
              {freeSlots.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </Select>
          )}
        </div>
        <div className="flex gap-3">
          <div className="flex-1">
            <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="sf-start">
              Window start
            </label>
            <Input
              id="sf-start"
              type="time"
              aria-label="Window start"
              value={start}
              onChange={(e) => setStart(e.target.value)}
            />
          </div>
          <div className="flex-1">
            <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="sf-end">
              Window end
            </label>
            <Input
              id="sf-end"
              type="time"
              aria-label="Window end"
              value={end}
              onChange={(e) => setEnd(e.target.value)}
            />
          </div>
        </div>
        <div>
          <div className="mb-1 block text-xs font-medium text-slate-600">Days</div>
          <DaysOfWeekPicker value={days} onChange={setDays} />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="sf-override">
            Profile override (optional)
          </label>
          <Select id="sf-override" value={override} onChange={(e) => setOverride(e.target.value)}>
            <option value="">— use assigned/default —</option>
            {live.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </Select>
        </div>
        <KeyValueEditor rows={rows} onChange={setRows} label="Schedule variables" />
        <label className="flex items-center gap-2 text-sm text-slate-700">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
            aria-label="enabled"
          />
          Enabled
        </label>
        {localError ? <p className="text-xs font-medium text-red-700">{localError}</p> : null}
        {serverError ? <p className="text-xs font-medium text-red-700">{serverError}</p> : null}
        <div className="mt-5 flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="button" onClick={handleSubmit} disabled={busy}>
            {isEdit ? (busy ? "Saving…" : "Save") : busy ? "Creating…" : "Create"}
          </Button>
        </div>
      </div>
    </Dialog>
  );
}
```

- [ ] **Step 6: Run the schedule form test to verify it passes**

Run: `cd apps/admin-ui && npx vitest run src/test/ScheduleFormDialog.test.tsx`
Expected: PASS (5 tests).

- [ ] **Step 7: Lint + commit**

```bash
cd apps/admin-ui && npm run lint
git add apps/admin-ui/src/types/api.ts apps/admin-ui/src/features/schedules/hooks.ts apps/admin-ui/src/features/schedules/ScheduleFormDialog.tsx apps/admin-ui/src/test/ScheduleFormDialog.test.tsx
git commit -m "feat(admin-ui): schedule create/edit dialog + schedules hooks"
```

---

## Task 6: Per-contact schedule section on the detail page

**Files:**
- Create: `apps/admin-ui/src/features/schedules/ScheduleSection.tsx`
- Modify: `apps/admin-ui/src/features/contacts/ContactDetailPage.tsx` (mount at `SCHEDULES_SECTION`)
- Test: `apps/admin-ui/src/test/ScheduleSection.test.tsx`

**Interfaces:**
- Consumes: `useContactSchedules`, `useUpdateSchedule`, `useDeleteSchedule`, `ScheduleFormDialog` (Task 5); `ConfirmDialog`.
- Produces: `<ScheduleSection contactId />`.

- [ ] **Step 1: Write the failing `ScheduleSection` test**

Create `apps/admin-ui/src/test/ScheduleSection.test.tsx`:

```tsx
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ScheduleResponse } from "../types/api";

const getMock = vi.fn();
const patchMock = vi.fn();
const delMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    get: (u: string) => getMock(u),
    post: vi.fn(),
    patch: (u: string, b?: unknown) => patchMock(u, b),
    del: (u: string) => delMock(u),
  },
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      public detail: string,
    ) {
      super(detail);
    }
  },
}));
vi.mock("../components/ui/toast", () => ({ pushToast: vi.fn() }));

import { ScheduleSection } from "../features/schedules/ScheduleSection";

function sched(over: Partial<ScheduleResponse> = {}): ScheduleResponse {
  return {
    id: "s1",
    contact_id: "c1",
    slot: "morning",
    enabled: true,
    window_start_local: "09:00:00",
    window_end_local: "11:00:00",
    days_of_week: ["monday", "tuesday"],
    dynamic_vars: {},
    profile_override: null,
    next_run_at: "2026-06-24T13:00:00Z",
    last_materialized_date: null,
    last_result: null,
    last_result_at: null,
    created_at: "",
    updated_at: "",
    ...over,
  };
}

let schedules: ScheduleResponse[] = [];
function renderSection() {
  getMock.mockImplementation((u: string) => {
    if (u.startsWith("/v1/admin/schedules")) return Promise.resolve(schedules);
    if (u.startsWith("/v1/admin/profiles")) return Promise.resolve([]);
    return Promise.reject(new Error(u));
  });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ScheduleSection contactId="c1" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getMock.mockReset();
  patchMock.mockReset();
  delMock.mockReset();
  schedules = [sched()];
});
afterEach(() => vi.clearAllMocks());

describe("ScheduleSection", () => {
  it("lists existing schedules and offers Add for the free slot", async () => {
    renderSection();
    expect(await screen.findByText(/morning/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Add schedule/i })).toBeInTheDocument();
  });

  it("toggles enabled via PATCH", async () => {
    const user = userEvent.setup();
    patchMock.mockResolvedValue(sched({ enabled: false }));
    renderSection();
    await screen.findByText(/morning/i);
    await user.click(screen.getByRole("button", { name: /Disable/i }));
    await waitFor(() =>
      expect(patchMock).toHaveBeenCalledWith("/v1/admin/schedules/s1", { enabled: false }),
    );
  });

  it("deletes a schedule after confirmation", async () => {
    const user = userEvent.setup();
    delMock.mockResolvedValue(undefined);
    renderSection();
    await screen.findByText(/morning/i);
    await user.click(screen.getByRole("button", { name: "Delete" }));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(delMock).toHaveBeenCalledWith("/v1/admin/schedules/s1"));
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/admin-ui && npx vitest run src/test/ScheduleSection.test.tsx`
Expected: FAIL (cannot resolve `ScheduleSection`).

- [ ] **Step 3: Implement `ScheduleSection.tsx`**

Create `apps/admin-ui/src/features/schedules/ScheduleSection.tsx`:

```tsx
import { useState } from "react";
import { Button } from "../../components/ui/button";
import { Badge } from "../../components/ui/badge";
import { Spinner } from "../../components/ui/spinner";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import type { ScheduleResponse, ScheduleSlot } from "../../types/api";
import { ScheduleFormDialog } from "./ScheduleFormDialog";
import { useContactSchedules, useDeleteSchedule, useUpdateSchedule } from "./hooks";

function fmtWindow(s: ScheduleResponse): string {
  return `${s.window_start_local.slice(0, 5)}–${s.window_end_local.slice(0, 5)}`;
}

export function ScheduleSection({ contactId }: { contactId: string }) {
  const list = useContactSchedules(contactId);
  const update = useUpdateSchedule();
  const del = useDeleteSchedule();
  const [createOpen, setCreateOpen] = useState(false);
  const [editing, setEditing] = useState<ScheduleResponse | null>(null);
  const [toDelete, setToDelete] = useState<ScheduleResponse | null>(null);

  const schedules = list.data ?? [];
  const takenSlots = schedules.map((s) => s.slot) as ScheduleSlot[];
  const hasFreeSlot = takenSlots.length < 2;

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="font-display text-lg text-ink-strong">Schedules</h2>
        {hasFreeSlot ? (
          <Button variant="secondary" onClick={() => setCreateOpen(true)}>
            + Add schedule
          </Button>
        ) : null}
      </div>

      {list.isLoading ? (
        <div className="flex items-center gap-2 text-muted">
          <Spinner /> Loading…
        </div>
      ) : schedules.length === 0 ? (
        <p className="text-sm text-faint">No schedules yet.</p>
      ) : (
        <ul className="space-y-2">
          {schedules.map((s) => (
            <li
              key={s.id}
              className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-line px-3 py-2"
            >
              <div className="text-sm">
                <span className="font-medium capitalize">{s.slot}</span>{" "}
                <span className="text-muted">
                  {fmtWindow(s)} · {s.days_of_week.length} days
                </span>{" "}
                {s.enabled ? <Badge tone="green">on</Badge> : <Badge>off</Badge>}
              </div>
              <div className="flex gap-2">
                <Button
                  variant="ghost"
                  disabled={update.isPending}
                  onClick={() => update.mutate({ id: s.id, body: { enabled: !s.enabled } })}
                >
                  {s.enabled ? "Disable" : "Enable"}
                </Button>
                <Button variant="secondary" onClick={() => setEditing(s)}>
                  Edit
                </Button>
                <Button variant="danger" onClick={() => setToDelete(s)}>
                  Delete
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {createOpen ? (
        <ScheduleFormDialog
          mode="create"
          contactId={contactId}
          existingSlots={takenSlots}
          onClose={() => setCreateOpen(false)}
        />
      ) : null}
      {editing ? (
        <ScheduleFormDialog
          mode="edit"
          contactId={contactId}
          schedule={editing}
          existingSlots={takenSlots}
          onClose={() => setEditing(null)}
        />
      ) : null}
      <ConfirmDialog
        open={toDelete !== null}
        title="Delete schedule?"
        body={
          <>
            Delete the <strong>{toDelete?.slot}</strong> schedule? Future automatic calls for this
            slot stop. This cannot be undone.
          </>
        }
        confirmLabel="Delete"
        busy={del.isPending}
        onCancel={() => setToDelete(null)}
        onConfirm={() => {
          if (!toDelete) return;
          del.mutate(toDelete.id, { onSuccess: () => setToDelete(null) });
        }}
      />
    </section>
  );
}
```

- [ ] **Step 4: Mount the section in `ContactDetailPage.tsx`**

Add `import { ScheduleSection } from "../schedules/ScheduleSection";` and replace the `{/* SCHEDULES_SECTION … */}` comment with:

```tsx
      <ScheduleSection contactId={c.id} />
```

- [ ] **Step 5: Run the section + detail tests**

Run: `cd apps/admin-ui && npx vitest run src/test/ScheduleSection.test.tsx src/test/ContactDetailPage.test.tsx`
Expected: PASS.

- [ ] **Step 6: Lint + commit**

```bash
cd apps/admin-ui && npm run lint
git add apps/admin-ui/src/features/schedules/ScheduleSection.tsx apps/admin-ui/src/features/contacts/ContactDetailPage.tsx apps/admin-ui/src/test/ScheduleSection.test.tsx
git commit -m "feat(admin-ui): per-contact schedule section on the detail page"
```

---

## Task 7: Global Schedules page + route + nav

**Files:**
- Modify: `apps/admin-ui/src/components/nav-icons.tsx` (add `SchedulesIcon`)
- Modify: `apps/admin-ui/src/components/NavSidebar.tsx` (add Schedules nav item)
- Modify: `apps/admin-ui/src/routes.tsx` (add `/schedules` under `RequireAdmin`)
- Create: `apps/admin-ui/src/features/schedules/SchedulesPage.tsx`
- Test: `apps/admin-ui/src/test/SchedulesPage.test.tsx`

**Interfaces:**
- Consumes: `useSchedules` (Task 5).

- [ ] **Step 1: Add `SchedulesIcon` to `nav-icons.tsx`**

Append (uses the file-local `Icon`):

```tsx
// Config → Schedules (calendar with a clock).
export function SchedulesIcon() {
  return (
    <Icon>
      <rect width="18" height="18" x="3" y="4" rx="2" />
      <path d="M3 10h18" />
      <path d="M8 2v4" />
      <path d="M16 2v4" />
      <path d="M12 14v2l1.5 1" />
    </Icon>
  );
}
```

- [ ] **Step 2: Write the failing `SchedulesPage` test**

Create `apps/admin-ui/src/test/SchedulesPage.test.tsx`:

```tsx
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ScheduleResponse } from "../types/api";
import { meFixture } from "./meFixture";

const getMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: { get: (u: string) => getMock(u), patch: vi.fn(), del: vi.fn() },
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      public detail: string,
    ) {
      super(detail);
    }
  },
}));
vi.mock("../components/ui/toast", () => ({ pushToast: vi.fn() }));

import { SchedulesPage } from "../features/schedules/SchedulesPage";

let lastUrl = "";
const row: ScheduleResponse = {
  id: "s1",
  contact_id: "c1",
  slot: "morning",
  enabled: true,
  window_start_local: "09:00:00",
  window_end_local: "11:00:00",
  days_of_week: ["monday"],
  dynamic_vars: {},
  profile_override: null,
  next_run_at: "2026-06-24T13:00:00Z",
  last_materialized_date: null,
  last_result: "skipped_window",
  last_result_at: null,
  created_at: "",
  updated_at: "",
};

function renderPage() {
  getMock.mockImplementation((u: string) => {
    if (u === "/v1/auth/me") return Promise.resolve(meFixture("admin"));
    if (u.startsWith("/v1/admin/schedules")) {
      lastUrl = u;
      return Promise.resolve([row]);
    }
    if (u.startsWith("/v1/admin/profiles")) return Promise.resolve([]);
    return Promise.reject(new Error(u));
  });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <SchedulesPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getMock.mockReset();
  lastUrl = "";
});
afterEach(() => vi.clearAllMocks());

describe("SchedulesPage", () => {
  it("links each row to the contact and shows last_result", async () => {
    renderPage();
    const link = await screen.findByRole("link", { name: /c1/ });
    expect(link).toHaveAttribute("href", "/contacts/c1");
    expect(screen.getByText("skipped_window")).toBeInTheDocument();
  });

  it("applies the 'who missed' filter as last_result=skipped_window", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("skipped_window");
    await user.click(screen.getByRole("button", { name: /Who missed/i }));
    await waitFor(() => expect(lastUrl).toContain("last_result=skipped_window"));
  });
});
```

- [ ] **Step 3: Run it to verify it fails**

Run: `cd apps/admin-ui && npx vitest run src/test/SchedulesPage.test.tsx`
Expected: FAIL (cannot resolve `SchedulesPage`).

- [ ] **Step 4: Implement `SchedulesPage.tsx`**

Create `apps/admin-ui/src/features/schedules/SchedulesPage.tsx`:

```tsx
import { useState } from "react";
import { Link } from "react-router-dom";
import { Table, Tbody, Td, Th, Thead, Tr } from "../../components/ui/table";
import { Select } from "../../components/ui/select";
import { Button } from "../../components/ui/button";
import { Badge } from "../../components/ui/badge";
import { Spinner } from "../../components/ui/spinner";
import { fmtDate } from "../../lib/format";
import { useIsAdmin } from "../../auth/useSession";
import { useSchedules, type ScheduleFilters } from "./hooks";

const PAGE_SIZE = 100;

export function SchedulesPage() {
  const isAdmin = useIsAdmin();
  const [slot, setSlot] = useState("");
  const [lastResult, setLastResult] = useState("");
  const [offset, setOffset] = useState(0);

  const setFilter = (set: (v: string) => void) => (value: string) => {
    set(value);
    setOffset(0);
  };

  const filters: ScheduleFilters = {
    slot: slot || undefined,
    lastResult: lastResult || undefined,
  };
  const schedules = useSchedules(filters, PAGE_SIZE, offset);

  if (!isAdmin) return <p className="text-sm text-muted">Admins only.</p>;

  const list = schedules.data ?? [];
  const hasNext = list.length === PAGE_SIZE;
  const hasPrev = offset > 0;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="font-display text-2xl text-ink-strong">Schedules</h1>
        <Button
          variant={lastResult === "skipped_window" ? "primary" : "secondary"}
          onClick={() =>
            setFilter(setLastResult)(lastResult === "skipped_window" ? "" : "skipped_window")
          }
        >
          Who missed (skipped window)
        </Button>
      </div>

      <div className="flex flex-wrap items-end gap-3">
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="sch-slot">
            Slot
          </label>
          <Select
            id="sch-slot"
            className="w-36"
            value={slot}
            onChange={(e) => setFilter(setSlot)(e.target.value)}
          >
            <option value="">All</option>
            <option value="morning">morning</option>
            <option value="evening">evening</option>
          </Select>
        </div>
      </div>

      {schedules.isLoading ? (
        <div className="flex items-center gap-2 text-muted">
          <Spinner /> Loading schedules…
        </div>
      ) : schedules.isError ? (
        <p className="text-sm text-red-700">
          Failed to load schedules: {(schedules.error as Error)?.message}
        </p>
      ) : (
        <>
          <Table>
            <Thead>
              <Tr>
                <Th>Contact</Th>
                <Th>Slot</Th>
                <Th>Window</Th>
                <Th>Days</Th>
                <Th>Enabled</Th>
                <Th>Next run</Th>
                <Th>Last result</Th>
              </Tr>
            </Thead>
            <Tbody>
              {list.length === 0 ? (
                <Tr>
                  <Td className="text-faint" colSpan={7}>
                    No schedules match these filters.
                  </Td>
                </Tr>
              ) : null}
              {list.map((s) => (
                <Tr key={s.id}>
                  <Td className="font-medium">
                    <Link className="text-accent hover:underline" to={`/contacts/${s.contact_id}`}>
                      {s.contact_id}
                    </Link>
                  </Td>
                  <Td className="capitalize">{s.slot}</Td>
                  <Td className="font-mono text-xs">
                    {s.window_start_local.slice(0, 5)}–{s.window_end_local.slice(0, 5)}
                  </Td>
                  <Td className="text-xs">{s.days_of_week.length}</Td>
                  <Td>{s.enabled ? <Badge tone="green">on</Badge> : <Badge>off</Badge>}</Td>
                  <Td className="whitespace-nowrap text-xs">{fmtDate(s.next_run_at)}</Td>
                  <Td className="text-xs">{s.last_result ?? "—"}</Td>
                </Tr>
              ))}
            </Tbody>
          </Table>

          <div className="flex items-center gap-3 text-sm text-muted">
            <Button
              variant="secondary"
              disabled={!hasPrev}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            >
              Previous
            </Button>
            <Button
              variant="secondary"
              disabled={!hasNext}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              Next
            </Button>
          </div>
        </>
      )}
    </div>
  );
}
```

> The global page is read + jump-to-contact (per spec §6.3; create/edit live on the contact detail).

- [ ] **Step 5: Add the route + nav item**

In `routes.tsx`: import `SchedulesPage` and add after the `contacts/:id` route:

```tsx
          {
            path: "schedules",
            element: (
              <RequireAdmin>
                <SchedulesPage />
              </RequireAdmin>
            ),
          },
```

In `NavSidebar.tsx`: import `SchedulesIcon` (from `./nav-icons`) and add to the **Config** group items (after Contacts):

```tsx
      { to: "/schedules", label: "Schedules", icon: SchedulesIcon, adminOnly: true },
```

- [ ] **Step 6: Run the page test to verify it passes**

Run: `cd apps/admin-ui && npx vitest run src/test/SchedulesPage.test.tsx`
Expected: PASS (2 tests).

- [ ] **Step 7: Lint + commit**

```bash
cd apps/admin-ui && npm run lint
git add apps/admin-ui/src/components/nav-icons.tsx apps/admin-ui/src/components/NavSidebar.tsx apps/admin-ui/src/routes.tsx apps/admin-ui/src/features/schedules/SchedulesPage.tsx apps/admin-ui/src/test/SchedulesPage.test.tsx
git commit -m "feat(admin-ui): global schedules page with who-missed filter + nav"
```

---

## Task 8: DNC page (list / add / remove-by-typed-phone)

**Files:**
- Modify: `apps/admin-ui/src/types/api.ts` (add `AdminDNCResponse`, `DNCCreate`)
- Modify: `apps/admin-ui/src/components/nav-icons.tsx` (add `DncIcon`)
- Modify: `apps/admin-ui/src/components/NavSidebar.tsx` (add DNC nav item)
- Modify: `apps/admin-ui/src/routes.tsx` (add `/dnc` under `RequireAdmin`)
- Create: `apps/admin-ui/src/features/dnc/hooks.ts`, `apps/admin-ui/src/features/dnc/AddDncDialog.tsx`, `apps/admin-ui/src/features/dnc/DncPage.tsx`
- Test: `apps/admin-ui/src/test/DncPage.test.tsx`

**Interfaces:**
- Consumes: `api`, `pushToast`, `Dialog`, `Table`, `Input`, `Button`, `useIsAdmin`.

- [ ] **Step 1: Add DNC types**

Append to `types/api.ts`:

```typescript
// DNC — admin self-service (dnc.py). The list/add response carries ONLY the masked
// phone (raw phone_e164 is never exposed); removal targets the full E.164 by path.
export interface AdminDNCResponse {
  masked_phone: string;
  reason: string | null;
  added_at: string;
}

export interface DNCCreate {
  phone_e164: string;
  reason?: string | null;
}
```

- [ ] **Step 2: Add `DncIcon` to `nav-icons.tsx`**

```tsx
// Config → DNC (a phone with a slash — do not call).
export function DncIcon() {
  return (
    <Icon>
      <path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.13.96.36 1.9.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91" />
      <line x1="2" x2="22" y1="2" y2="22" />
    </Icon>
  );
}
```

- [ ] **Step 3: Implement `features/dnc/hooks.ts`**

Create `apps/admin-ui/src/features/dnc/hooks.ts`:

```typescript
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { ApiError } from "../../lib/api";
import { pushToast } from "../../components/ui/toast";
import type { AdminDNCResponse, DNCCreate } from "../../types/api";

const DNC_KEY = ["dnc"] as const;

export function useDnc(limit = 200, offset = 0) {
  return useQuery<AdminDNCResponse[]>({
    queryKey: [...DNC_KEY, limit, offset],
    queryFn: () => api.get<AdminDNCResponse[]>(`/v1/admin/dnc?limit=${limit}&offset=${offset}`),
  });
}

export function useAddDnc() {
  const qc = useQueryClient();
  return useMutation<AdminDNCResponse, ApiError, DNCCreate>({
    mutationFn: (body) => api.post<AdminDNCResponse>("/v1/admin/dnc", body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: DNC_KEY }),
  });
}

export function useRemoveDnc() {
  const qc = useQueryClient();
  return useMutation<void, ApiError, string>({
    // The path carries the full E.164; the operator re-enters it (the list only has
    // the masked form — spec D5). encodeURIComponent guards the leading '+'.
    mutationFn: (phone) => api.del<void>(`/v1/admin/dnc/${encodeURIComponent(phone)}`),
    onSuccess: () => void qc.invalidateQueries({ queryKey: DNC_KEY }),
    onError: (err) => pushToast(err.detail),
  });
}
```

- [ ] **Step 4: Write the failing `DncPage` test**

Create `apps/admin-ui/src/test/DncPage.test.tsx`:

```tsx
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { AdminDNCResponse } from "../types/api";
import { meFixture } from "./meFixture";

const getMock = vi.fn();
const postMock = vi.fn();
const delMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    get: (u: string) => getMock(u),
    post: (u: string, b?: unknown) => postMock(u, b),
    del: (u: string) => delMock(u),
  },
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      public detail: string,
    ) {
      super(detail);
    }
  },
}));
vi.mock("../components/ui/toast", () => ({ pushToast: vi.fn() }));

import { DncPage } from "../features/dnc/DncPage";

let entries: AdminDNCResponse[] = [];
function renderPage(role: "admin" | "viewer" = "admin") {
  getMock.mockImplementation((u: string) => {
    if (u === "/v1/auth/me") return Promise.resolve(meFixture(role));
    if (u.startsWith("/v1/admin/dnc")) return Promise.resolve(entries);
    return Promise.reject(new Error(u));
  });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <DncPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getMock.mockReset();
  postMock.mockReset();
  delMock.mockReset();
  entries = [
    { masked_phone: "***4567", reason: "patient request", added_at: "2026-06-20T09:00:00Z" },
  ];
});
afterEach(() => vi.clearAllMocks());

describe("DncPage", () => {
  it("lists masked entries", async () => {
    renderPage();
    expect(await screen.findByText("***4567")).toBeInTheDocument();
    expect(screen.getByText("patient request")).toBeInTheDocument();
  });

  it("adds a number via the dialog", async () => {
    const user = userEvent.setup();
    postMock.mockResolvedValue({ masked_phone: "***9999", reason: null, added_at: "" });
    renderPage();
    await user.click(await screen.findByRole("button", { name: "+ Add to DNC" }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText(/Phone/), "+19495559999");
    await user.click(within(dialog).getByRole("button", { name: "Add" }));
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/v1/admin/dnc", { phone_e164: "+19495559999", reason: null }),
    );
  });

  it("requires the full E.164 typed to remove", async () => {
    const user = userEvent.setup();
    delMock.mockResolvedValue(undefined);
    renderPage();
    await screen.findByText("***4567");
    await user.click(screen.getByRole("button", { name: "Remove" }));
    const dialog = screen.getByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "Remove" });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/full number/i), "+19495554567");
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    await waitFor(() => expect(delMock).toHaveBeenCalledWith("/v1/admin/dnc/%2B19495554567"));
  });

  it("renders Admins-only for a viewer", async () => {
    renderPage("viewer");
    expect(await screen.findByText("Admins only.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "+ Add to DNC" })).not.toBeInTheDocument();
  });
});
```

- [ ] **Step 5: Run it to verify it fails**

Run: `cd apps/admin-ui && npx vitest run src/test/DncPage.test.tsx`
Expected: FAIL (cannot resolve `DncPage`).

- [ ] **Step 6: Implement `AddDncDialog.tsx`**

Create `apps/admin-ui/src/features/dnc/AddDncDialog.tsx`:

```tsx
import { useState, type FormEvent } from "react";
import { Dialog } from "../../components/ui/dialog";
import { Input } from "../../components/ui/input";
import { Button } from "../../components/ui/button";
import type { ApiError } from "../../lib/api";
import { useAddDnc } from "./hooks";

const E164 = /^\+[1-9]\d{7,14}$/;

export function AddDncDialog({ onClose }: { onClose: () => void }) {
  const [phone, setPhone] = useState("");
  const [reason, setReason] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const add = useAddDnc();
  const serverError = (add.error as ApiError | null)?.detail ?? null;

  function handleSubmit(e: FormEvent): void {
    e.preventDefault();
    setLocalError(null);
    if (!E164.test(phone.trim())) {
      setLocalError("Phone must be E.164 format, e.g. +19495551234.");
      return;
    }
    add.mutate({ phone_e164: phone.trim(), reason: reason.trim() || null }, { onSuccess: onClose });
  }

  return (
    <Dialog open onClose={onClose} title="Add to Do-Not-Call list">
      <form onSubmit={handleSubmit} className="space-y-3">
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="dnc-phone">
            Phone (E.164)
          </label>
          <Input
            id="dnc-phone"
            placeholder="+19495551234"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
          />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="dnc-reason">
            Reason (optional)
          </label>
          <Input id="dnc-reason" value={reason} onChange={(e) => setReason(e.target.value)} />
        </div>
        {localError ? <p className="text-xs font-medium text-red-700">{localError}</p> : null}
        {serverError ? <p className="text-xs font-medium text-red-700">{serverError}</p> : null}
        <div className="mt-5 flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={add.isPending}>
            Cancel
          </Button>
          <Button type="submit" disabled={add.isPending}>
            {add.isPending ? "Adding…" : "Add"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
```

- [ ] **Step 7: Implement `DncPage.tsx`**

Create `apps/admin-ui/src/features/dnc/DncPage.tsx`:

```tsx
import { useState } from "react";
import { Table, Tbody, Td, Th, Thead, Tr } from "../../components/ui/table";
import { Input } from "../../components/ui/input";
import { Button } from "../../components/ui/button";
import { Spinner } from "../../components/ui/spinner";
import { Dialog } from "../../components/ui/dialog";
import { fmtDate } from "../../lib/format";
import { useIsAdmin } from "../../auth/useSession";
import { AddDncDialog } from "./AddDncDialog";
import { useDnc, useRemoveDnc } from "./hooks";

const E164 = /^\+[1-9]\d{7,14}$/;

export function DncPage() {
  const isAdmin = useIsAdmin();
  const dnc = useDnc();
  const remove = useRemoveDnc();
  const [addOpen, setAddOpen] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [removePhone, setRemovePhone] = useState("");

  if (!isAdmin) return <p className="text-sm text-muted">Admins only.</p>;

  const entries = dnc.data ?? [];
  const canRemove = E164.test(removePhone.trim());

  function closeRemove() {
    setRemoving(false);
    setRemovePhone("");
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="font-display text-2xl text-ink-strong">Do-Not-Call list</h1>
        <Button onClick={() => setAddOpen(true)}>+ Add to DNC</Button>
      </div>

      {dnc.isLoading ? (
        <div className="flex items-center gap-2 text-muted">
          <Spinner /> Loading…
        </div>
      ) : dnc.isError ? (
        <p className="text-sm text-red-700">Failed to load: {(dnc.error as Error)?.message}</p>
      ) : (
        <Table>
          <Thead>
            <Tr>
              <Th>Phone</Th>
              <Th>Reason</Th>
              <Th>Added</Th>
              <Th />
            </Tr>
          </Thead>
          <Tbody>
            {entries.length === 0 ? (
              <Tr>
                <Td className="text-faint" colSpan={4}>
                  The Do-Not-Call list is empty.
                </Td>
              </Tr>
            ) : null}
            {entries.map((e) => (
              <Tr key={e.masked_phone + e.added_at}>
                <Td className="font-mono text-xs">{e.masked_phone}</Td>
                <Td>{e.reason ?? "—"}</Td>
                <Td className="whitespace-nowrap text-xs">{fmtDate(e.added_at)}</Td>
                <Td>
                  <Button variant="danger" onClick={() => setRemoving(true)}>
                    Remove
                  </Button>
                </Td>
              </Tr>
            ))}
          </Tbody>
        </Table>
      )}

      {addOpen ? <AddDncDialog onClose={() => setAddOpen(false)} /> : null}

      <Dialog open={removing} onClose={closeRemove} title="Remove from Do-Not-Call list">
        <div className="space-y-3">
          <p className="text-sm text-slate-700">
            Entries show only a masked number, so re-enter the full E.164 to remove it. This
            re-enables calling that number.
          </p>
          <div>
            <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="dnc-remove">
              Full number (E.164)
            </label>
            <Input
              id="dnc-remove"
              placeholder="+19495554567"
              value={removePhone}
              onChange={(e) => setRemovePhone(e.target.value)}
            />
          </div>
          <div className="mt-5 flex justify-end gap-2">
            <Button
              type="button"
              variant="secondary"
              onClick={closeRemove}
              disabled={remove.isPending}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="danger"
              disabled={!canRemove || remove.isPending}
              onClick={() => remove.mutate(removePhone.trim(), { onSuccess: closeRemove })}
            >
              Remove
            </Button>
          </div>
        </div>
      </Dialog>
    </div>
  );
}
```

- [ ] **Step 8: Add the route + nav item**

In `routes.tsx`: import `DncPage` and add after the `schedules` route:

```tsx
          {
            path: "dnc",
            element: (
              <RequireAdmin>
                <DncPage />
              </RequireAdmin>
            ),
          },
```

In `NavSidebar.tsx`: import `DncIcon` (from `./nav-icons`) and add to the **Config** group (after Schedules):

```tsx
      { to: "/dnc", label: "DNC", icon: DncIcon, adminOnly: true },
```

- [ ] **Step 9: Run the DNC test to verify it passes**

Run: `cd apps/admin-ui && npx vitest run src/test/DncPage.test.tsx`
Expected: PASS (4 tests).

- [ ] **Step 10: Full suite + lint, then commit**

```bash
cd apps/admin-ui && npm run lint && npx vitest run
git add apps/admin-ui/src/types/api.ts apps/admin-ui/src/components/nav-icons.tsx apps/admin-ui/src/components/NavSidebar.tsx apps/admin-ui/src/routes.tsx apps/admin-ui/src/features/dnc/ apps/admin-ui/src/test/DncPage.test.tsx
git commit -m "feat(admin-ui): DNC list/add/remove page with typed-phone removal + nav"
```

---

## Self-Review (completed by plan author)

**Spec coverage** (spec §6 surfaces → tasks):
- §6.1 Contacts list (+New, row links) → Task 2. ✅
- §6.2 Contact detail (header, edit, delete, recent-calls) → Task 3; call-now button → Task 4; schedules section → Task 6. ✅
- §6.3 Global Schedules (filters, who-missed) → Task 7. ✅
- §6.4 DNC (list/add/remove-by-typed-phone) → Task 8. ✅
- §6.5 Call-now (ack gating, 202/200-dnc/503/422) → Task 4. ✅
- §5 KV editor for call vars; metadata JSON → Tasks 1 (editor) + 2 (metadata textarea) + 4/5 (vars). ✅
- §4 routes + nav → Tasks 3/7/8. ✅
- §8 testing (per-page vitest, masked-phone-omit, slot-immutable, remove-by-typed-phone) → covered in the corresponding tasks. ✅

**Type consistency:** `ContactDetail` extends `ContactSummary` (existing); `Weekday` declared in Task 1 and reused by `DaysOfWeekPicker`/schedule types; `KvRow`/`rowsToRecord`/`recordToRows` names stable across Tasks 1/4/5; `api.del`/`api.patch`/`api.post` match `lib/api.ts`; hook return types match the PR A schemas. ✅

**Placeholder scan:** every code step contains complete code; tests contain real assertions; commands have expected output. The two cross-task extension points in `ContactDetailPage` (`CALL_NOW_BUTTON`, `SCHEDULES_SECTION`) are explicit comments filled by Tasks 4 and 6 — not vague TODOs. ✅

**Role-gating consistency:** all three new/extended pages (`ContactsPage` already, `ContactDetailPage`, `SchedulesPage`, `DncPage`) use the same page-level gate `if (!isAdmin) return "Admins only."`, and every new route is `RequireAdmin`-wrapped. (Viewer-readable pages like `CustomVariables` use a different per-control pattern, but PR B's surfaces are admin-only per spec D4.) ✅
