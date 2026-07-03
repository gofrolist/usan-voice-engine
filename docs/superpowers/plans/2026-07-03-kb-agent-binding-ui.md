# KB ↔ agent binding editor UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an org admin bind/unbind the org's knowledge bases to an agent from a new "Knowledge Base" section in the agent editor, applied on Publish.

**Architecture:** Binding is the existing draft field `AgentConfig.llm.knowledge_base_ids` (encoded `knowledge_base_<hex>` tokens), edited in a new editor section and persisted through the existing draft/publish flow — no new endpoint, no auto-publish. One backend change exposes the encoded token on the native KB list (`KbSummary.agent_ref`) so the UI can map config tokens to KBs. The frontend adds the field to the zod form schema (which also fixes a silent-strip data-loss bug) plus a `KnowledgeBaseSection`.

**Tech Stack:** apps/api — FastAPI / Pydantic v2 / pytest (py3.14, uv). apps/admin-ui — React / Vite / TypeScript / react-hook-form + zod / @tanstack/react-query v5 / vitest + Testing Library.

## Global Constraints

- **Bind/unbind only** — no per-agent retrieval tuning, no KB-instruction (deferred).
- **Draft edit, applied on Publish** — no dedicated bind endpoint, no auto-publish. Reuses `PUT /v1/admin/profiles/{id}/draft` + `POST .../publish`.
- **Config stores encoded tokens** `knowledge_base_<hex>` (what the runtime decodes); the native KB list returns raw UUIDs, so the UI needs the encoded token exposed as `agent_ref`.
- **Never silently drop a binding.** A bound token with no matching KB in the list (deleted or compat-created KB) must be shown and retained on save, not dropped. The zod schema must round-trip `knowledge_base_ids` (it is currently stripped — this is a real bug to fix).
- **No migration, no new env keys.** The `AgentConfig.llm.knowledge_base_ids` field already exists in the backend.
- **Python:** type hints, line-length 100, `ruff` + `mypy` clean (`cd apps/api && uv run …`).
- **TypeScript:** no `any`; run `npm run typecheck` (tsc) before done — CI runs it, local `npm run lint` does not.
- **Commit scope:** `api` for backend, `admin-ui` for frontend.

---

### Task 1: Backend — expose the encoded token on `KbSummary`

Add `agent_ref` (the encoded `knowledge_base_<hex>` token) to the native KB list response so the UI can reconcile the config's tokens with KBs.

**Files:**
- Modify: `apps/api/src/usan_api/schemas/admin_knowledge_bases.py` (`KbSummary`)
- Modify: `apps/api/src/usan_api/routers/admin_knowledge_bases.py` (`list_knowledge_bases`)
- Test: `apps/api/tests/test_admin_knowledge_bases_api.py` (add)

**Interfaces:**
- Consumes (existing): `usan_api.compat.ids.encode_kb_id(uuid.UUID) -> str` and `decode_kb_id(str) -> uuid.UUID` (`apps/api/src/usan_api/compat/ids.py:70-75`).
- Produces: `KbSummary.agent_ref: str` on every `GET /v1/admin/knowledge-bases` row, where `agent_ref == encode_kb_id(id)`.

- [ ] **Step 1: Write the failing test**

Add to `apps/api/tests/test_admin_knowledge_bases_api.py`:

```python
def test_list_includes_roundtrippable_agent_ref(client, admin_session):
    import uuid as _uuid

    from usan_api.compat import ids

    kb_id = client.post("/v1/admin/knowledge-bases", json={"name": "Refable"}).json()["id"]
    rows = client.get("/v1/admin/knowledge-bases").json()
    row = next(k for k in rows if k["id"] == kb_id)
    assert row["agent_ref"] == ids.encode_kb_id(_uuid.UUID(kb_id))
    # round-trips back to the raw id (this is the token the runtime decodes)
    assert str(ids.decode_kb_id(row["agent_ref"])) == kb_id
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/api && uv run pytest -n0 tests/test_admin_knowledge_bases_api.py::test_list_includes_roundtrippable_agent_ref -q`
Expected: FAIL — `KeyError: 'agent_ref'` (field not in the response).

- [ ] **Step 3: Add the schema field**

In `apps/api/src/usan_api/schemas/admin_knowledge_bases.py`, add `agent_ref` to `KbSummary`:

```python
class KbSummary(BaseModel):
    id: uuid.UUID
    agent_ref: str  # encoded knowledge_base_<hex> token — the id the agent config stores/binds
    name: str
    status: str
    source_count: int
    updated_at: datetime
```

- [ ] **Step 4: Populate it in the router**

In `apps/api/src/usan_api/routers/admin_knowledge_bases.py`, add the import near the other `usan_api.compat` import:

```python
from usan_api.compat import ids
```

Then in `list_knowledge_bases`, set `agent_ref` for each row:

```python
@router.get("", response_model=list[KbSummary])
async def list_knowledge_bases(db: AsyncSession = Depends(get_tenant_db)) -> list[KbSummary]:
    kbs = await repo.list_kbs(db)
    by_kb = await repo.get_sources_for_kbs(db, [k.id for k in kbs])
    return [
        KbSummary(
            id=k.id,
            agent_ref=ids.encode_kb_id(k.id),
            name=k.name,
            status=k.status,
            source_count=len(by_kb.get(k.id, [])),
            updated_at=k.updated_at,
        )
        for k in kbs
    ]
```

- [ ] **Step 5: Run the KB api tests + lint + types**

Run:
```bash
cd apps/api && uv run pytest -n0 tests/test_admin_knowledge_bases_api.py -q
uv run ruff check src/usan_api/routers/admin_knowledge_bases.py src/usan_api/schemas/admin_knowledge_bases.py tests/test_admin_knowledge_bases_api.py
uv run ruff format src/usan_api/routers/admin_knowledge_bases.py src/usan_api/schemas/admin_knowledge_bases.py tests/test_admin_knowledge_bases_api.py
uv run mypy src/usan_api/routers/admin_knowledge_bases.py src/usan_api/schemas/admin_knowledge_bases.py
```
Expected: all pass (existing KB tests unaffected — `agent_ref` is additive), ruff clean, mypy clean.

- [ ] **Step 6: Commit**

```bash
git add apps/api/src/usan_api/schemas/admin_knowledge_bases.py apps/api/src/usan_api/routers/admin_knowledge_bases.py apps/api/tests/test_admin_knowledge_bases_api.py
git commit -m "feat(api): expose encoded agent_ref token on KB list for binding UI"
```

---

### Task 2: Frontend — schema + types (enable the field, fix the strip bug)

Add `knowledge_base_ids` to the zod form schema and TS types so it round-trips through load → validate → save (today it is silently stripped, which would wipe a compat-set binding on the next publish). Add `agent_ref` to the TS `KbSummary`.

**Files:**
- Modify: `apps/admin-ui/src/config/agentConfigSchema.ts` (`llmSchema`)
- Modify: `apps/admin-ui/src/types/api.ts` (`LLMConfig`, `KbSummary`)
- Test: `apps/admin-ui/src/test/agentConfigSchema.test.ts` (add)

**Interfaces:**
- Consumes (Task 1): `KbSummary.agent_ref` shape from the API.
- Produces (Task 3 consumes): the form field path `llm.knowledge_base_ids` typed `string[]` on `AgentConfigForm`; `LLMConfig.knowledge_base_ids: string[] | null`; `KbSummary.agent_ref: string`.

- [ ] **Step 1: Write the failing schema test**

Add to `apps/admin-ui/src/test/agentConfigSchema.test.ts` (import `llmSchema` alongside whatever it already imports from `../config/agentConfigSchema`):

```typescript
import { llmSchema } from "../config/agentConfigSchema";

describe("llmSchema.knowledge_base_ids", () => {
  it("defaults to [] when the key is absent (strip-regression guard)", () => {
    const out = llmSchema.parse({ model: "gemini-2.0-flash", temperature: null });
    expect(out.knowledge_base_ids).toEqual([]);
  });

  it("coerces null to []", () => {
    const out = llmSchema.parse({
      model: "gemini-2.0-flash",
      temperature: null,
      knowledge_base_ids: null,
    });
    expect(out.knowledge_base_ids).toEqual([]);
  });

  it("preserves bound tokens through parse (does not strip them)", () => {
    const out = llmSchema.parse({
      model: "gemini-2.0-flash",
      temperature: null,
      knowledge_base_ids: ["knowledge_base_abc123", "knowledge_base_def456"],
    });
    expect(out.knowledge_base_ids).toEqual(["knowledge_base_abc123", "knowledge_base_def456"]);
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/admin-ui && npx vitest run src/test/agentConfigSchema.test.ts`
Expected: FAIL — `knowledge_base_ids` is `undefined` (schema strips it), so all three assertions fail.

- [ ] **Step 3: Add the field to `llmSchema`**

In `apps/admin-ui/src/config/agentConfigSchema.ts`, extend `llmSchema` (the codebase already uses `z.preprocess` for null-coercing fields, e.g. quiet_hours):

```typescript
export const llmSchema = z.object({
  model: z.string().min(1).max(200),
  temperature: z.number().gte(0.0).lte(2.0).nullable(),
  // Bound KB tokens (knowledge_base_<hex>). preprocess coerces null/undefined (older
  // drafts, or a server null) to [] so the field round-trips instead of being stripped.
  knowledge_base_ids: z.preprocess((v) => v ?? [], z.array(z.string())),
});
```

- [ ] **Step 4: Add the TS types**

In `apps/admin-ui/src/types/api.ts`, add to `LLMConfig`:

```typescript
export interface LLMConfig {
  model: string;
  temperature: number | null;
  knowledge_base_ids: string[] | null;
}
```

and add `agent_ref` to `KbSummary`:

```typescript
export interface KbSummary {
  id: string;
  agent_ref: string;
  name: string;
  status: string;
  source_count: number;
  updated_at: string;
}
```

- [ ] **Step 5: Run the schema test + typecheck**

Run:
```bash
cd apps/admin-ui && npx vitest run src/test/agentConfigSchema.test.ts
npm run typecheck
```
Expected: 3 new assertions PASS. If `npm run typecheck` flags existing `KbSummary` object literals for the now-required `agent_ref` (e.g. `src/test/KnowledgeBasesPage.test.tsx` `rows`), add `agent_ref: "knowledge_base_<hex>"` to each such fixture. tsc must be clean.

- [ ] **Step 6: Run the KB list page test to confirm no regression**

Run: `cd apps/admin-ui && npx vitest run src/test/KnowledgeBasesPage.test.tsx`
Expected: PASS (the list page ignores `agent_ref`; only fixtures may need the field to satisfy tsc, which Step 5 handled).

- [ ] **Step 7: Commit**

```bash
git add apps/admin-ui/src/config/agentConfigSchema.ts apps/admin-ui/src/types/api.ts apps/admin-ui/src/test/agentConfigSchema.test.ts
git add -A apps/admin-ui/src/test   # any fixtures updated for agent_ref
git commit -m "feat(admin-ui): add knowledge_base_ids to agent config schema (round-trips, fixes strip)"
```

---

### Task 3: Frontend — `KnowledgeBaseSection` + editor wiring

Add the editor section that binds/unbinds KBs, and wire it into the section rail. Follows the `ToolsSection` pattern (fetch a catalog, `Controller` over an array field, checkbox toggle preserving stable order).

**Files:**
- Create: `apps/admin-ui/src/features/editor/sections/KnowledgeBaseSection.tsx`
- Modify: `apps/admin-ui/src/config/fieldMeta.ts` (`SectionKey`, `SECTION_LABELS`)
- Modify: `apps/admin-ui/src/features/editor/ProfileEditorPage.tsx` (`SECTION_ORDER`, render switch, summary)
- Test: `apps/admin-ui/src/test/KnowledgeBaseSection.test.tsx` (new)

**Interfaces:**
- Consumes (Task 2): form field `llm.knowledge_base_ids: string[]` on `AgentConfigForm`; `KbSummary.agent_ref`. Existing `useKnowledgeBases()` from `../../knowledgeBases/hooks` returning `KbSummary[]`.
- Produces: a `KnowledgeBaseSection({ form }: { form: UseFormReturn<AgentConfigForm> })` component and the `"knowledge_base"` section key.

- [ ] **Step 1: Write the failing section test**

Create `apps/admin-ui/src/test/KnowledgeBaseSection.test.tsx`:

```typescript
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import type { KbSummary } from "../types/api";
import type { AgentConfigForm } from "../config/agentConfigSchema";

const getMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: { get: (u: string) => getMock(u), post: vi.fn(), del: vi.fn() },
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      public detail: string,
    ) {
      super(detail);
    }
  },
}));

import { KnowledgeBaseSection } from "../features/editor/sections/KnowledgeBaseSection";

const KBS: KbSummary[] = [
  { id: "u1", agent_ref: "knowledge_base_aaa", name: "Pricing", status: "complete", source_count: 1, updated_at: "2026-07-03T00:00:00Z" },
  { id: "u2", agent_ref: "knowledge_base_bbb", name: "FAQ", status: "in_progress", source_count: 0, updated_at: "2026-07-03T00:00:00Z" },
];

function Harness({ bound }: { bound: string[] }) {
  const form = useForm<AgentConfigForm>({
    defaultValues: { llm: { model: "m", temperature: null, knowledge_base_ids: bound } } as AgentConfigForm,
  });
  return <KnowledgeBaseSection form={form} />;
}

function renderSection(bound: string[]) {
  getMock.mockImplementation((u: string) =>
    u === "/v1/admin/knowledge-bases" ? Promise.resolve(KBS) : Promise.reject(new Error(u)),
  );
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Harness bound={bound} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("KnowledgeBaseSection", () => {
  it("renders each KB with its bound state", async () => {
    renderSection(["knowledge_base_aaa"]);
    const pricing = (await screen.findByLabelText(/Pricing/)) as HTMLInputElement;
    const faq = screen.getByLabelText(/FAQ/) as HTMLInputElement;
    expect(pricing.checked).toBe(true);
    expect(faq.checked).toBe(false);
  });

  it("binding a KB adds its token to the form value", async () => {
    renderSection([]);
    const faq = (await screen.findByLabelText(/FAQ/)) as HTMLInputElement;
    await userEvent.click(faq);
    expect(faq.checked).toBe(true);
  });

  it("preserves an unknown bound token (not in the list) and shows it", async () => {
    renderSection(["knowledge_base_orphan"]);
    // The orphan token has no matching KB row but must still be surfaced, not dropped.
    expect(await screen.findByText(/knowledge_base_orphan/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/admin-ui && npx vitest run src/test/KnowledgeBaseSection.test.tsx`
Expected: FAIL — cannot import `KnowledgeBaseSection` (module does not exist).

- [ ] **Step 3: Create the section**

Create `apps/admin-ui/src/features/editor/sections/KnowledgeBaseSection.tsx`:

```typescript
import { Controller, type UseFormReturn } from "react-hook-form";
import { Link } from "react-router-dom";
import type { AgentConfigForm } from "../../../config/agentConfigSchema";
import { useKnowledgeBases } from "../../knowledgeBases/hooks";

// Binds knowledge bases to this agent. The config stores encoded knowledge_base_<hex>
// tokens (KbSummary.agent_ref); this section toggles those tokens in
// llm.knowledge_base_ids. A bound token with no matching KB in the org list (deleted or
// compat-created) is surfaced as a disabled "unknown" row and PRESERVED on every edit —
// never silently dropped. Applied on Publish, like every other editor field.
export function KnowledgeBaseSection({ form }: { form: UseFormReturn<AgentConfigForm> }) {
  const { data: kbs, isLoading, isError } = useKnowledgeBases();

  return (
    <div className="space-y-3">
      <p className="text-sm text-slate-500">
        Knowledge bases the agent can retrieve from during a call or chat. Bindings apply when
        you publish.
      </p>
      {isError ? (
        <p className="text-xs font-medium text-red-700">
          Could not load knowledge bases — bound ones below are still shown from this draft.
        </p>
      ) : null}
      <Controller
        control={form.control}
        name="llm.knowledge_base_ids"
        render={({ field }) => {
          const bound = new Set(field.value ?? []);
          const list = kbs ?? [];
          const knownRefs = new Set(list.map((k) => k.agent_ref));
          const orphans = [...bound].filter((ref) => !knownRefs.has(ref));

          function toggle(ref: string, on: boolean): void {
            const next = new Set(bound);
            if (on) next.add(ref);
            else next.delete(ref);
            field.onChange([...next]);
          }

          if (!isLoading && list.length === 0 && orphans.length === 0) {
            return (
              <p className="text-sm text-slate-500">
                No knowledge bases yet —{" "}
                <Link className="text-accent hover:underline" to="/knowledge-bases">
                  create one under Knowledge
                </Link>
                .
              </p>
            );
          }

          return (
            <ul className="space-y-2">
              {list.map((kb) => (
                <li
                  key={kb.agent_ref}
                  className="flex items-start justify-between gap-3 rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-card"
                >
                  <label htmlFor={`kb-${kb.agent_ref}`} className="min-w-0">
                    <span className="text-sm text-slate-900">{kb.name}</span>
                    <span className="mt-0.5 block text-xs text-slate-500">
                      {kb.status === "complete"
                        ? `${kb.source_count} source${kb.source_count === 1 ? "" : "s"}`
                        : kb.status.replace("_", " ")}
                    </span>
                  </label>
                  <input
                    id={`kb-${kb.agent_ref}`}
                    type="checkbox"
                    className="mt-1 h-4 w-4 accent-indigo-600"
                    checked={bound.has(kb.agent_ref)}
                    onChange={(e) => toggle(kb.agent_ref, e.target.checked)}
                  />
                </li>
              ))}
              {orphans.map((ref) => (
                <li
                  key={ref}
                  className="flex items-start justify-between gap-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3"
                >
                  <div className="min-w-0">
                    <span className="text-sm text-slate-900">Unknown knowledge base</span>
                    <span className="mt-0.5 block break-all font-mono text-xs text-slate-500">
                      {ref}
                    </span>
                  </div>
                  <button
                    type="button"
                    className="mt-0.5 text-xs font-medium text-red-700"
                    onClick={() => toggle(ref, false)}
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          );
        }}
      />
      {isLoading ? <p className="text-xs text-slate-400">Loading knowledge bases…</p> : null}
    </div>
  );
}
```

- [ ] **Step 4: Run the section test to verify it passes**

Run: `cd apps/admin-ui && npx vitest run src/test/KnowledgeBaseSection.test.tsx`
Expected: 3 tests PASS.

- [ ] **Step 5: Register the section key + label**

In `apps/admin-ui/src/config/fieldMeta.ts`, add `"knowledge_base"` to the `SectionKey` union (after `"llm"`):

```typescript
export type SectionKey =
  | "prompts"
  | "voice"
  | "llm"
  | "knowledge_base"
  | "stt"
  | "timing"
  | "tools"
  | "voicemail_detection"
  | "speech_advanced"
  | "policy";
```

and to `SECTION_LABELS`:

```typescript
export const SECTION_LABELS: Record<SectionKey, string> = {
  prompts: "Prompts",
  voice: "Voice",
  llm: "LLM",
  knowledge_base: "Knowledge Base",
  stt: "STT",
  timing: "Timing",
  tools: "Tools",
  voicemail_detection: "Voicemail",
  speech_advanced: "Speech (Advanced)",
  policy: "Policy",
};
```

- [ ] **Step 6: Wire the section into the editor page**

In `apps/admin-ui/src/features/editor/ProfileEditorPage.tsx`:

Add the import next to the other section imports:

```typescript
import { KnowledgeBaseSection } from "./sections/KnowledgeBaseSection";
```

Add `"knowledge_base"` to `SECTION_ORDER` after `"llm"`:

```typescript
const SECTION_ORDER: SectionKey[] = [
  "prompts",
  "voice",
  "llm",
  "knowledge_base",
  "stt",
  "speech_advanced",
  "timing",
  "tools",
  "voicemail_detection",
  "policy",
];
```

Add the watch + summary. Near the other watches (around line 234):

```typescript
  const kbIds = form.watch("llm.knowledge_base_ids");
```

and in the `summaries` object (around line 243):

```typescript
    knowledge_base: kbIds && kbIds.length ? `${kbIds.length} bound` : "None",
```

Add the render line in the section switch (after the `llm` line, ~line 296):

```typescript
                {section === "knowledge_base" ? <KnowledgeBaseSection form={form} /> : null}
```

- [ ] **Step 7: Typecheck + targeted editor suites**

Run:
```bash
cd apps/admin-ui && npm run typecheck
npx vitest run src/test/KnowledgeBaseSection.test.tsx src/test/agentConfigSchema.test.ts src/test/ProfileEditorPage.test.tsx
```
Expected: tsc clean; the three suites PASS. `SectionKey` is a closed `Record`, so tsc will flag `SECTION_LABELS` (Step 5) if the key was missed — that's the intended guard.

- [ ] **Step 8: Full admin-ui suite + build**

Run:
```bash
cd apps/admin-ui && npx vitest run
npm run build
```
Expected: green (re-run any load-flaky UNRELATED test in isolation per the known admin-ui flakiness), production build succeeds.

- [ ] **Step 9: Commit**

```bash
git add apps/admin-ui/src/features/editor/sections/KnowledgeBaseSection.tsx apps/admin-ui/src/config/fieldMeta.ts apps/admin-ui/src/features/editor/ProfileEditorPage.tsx apps/admin-ui/src/test/KnowledgeBaseSection.test.tsx
git commit -m "feat(admin-ui): Knowledge Base section in agent editor (bind/unbind KBs)"
```

---

## Post-implementation

- [ ] Open a squash PR (`feat: KB↔agent binding editor UI`) to `main`, branch `feat/kb-agent-binding-ui`; summary + test plan.
- [ ] Deploys on the next `v*` tag (no migration, no new env keys). After deploy, the Clara/Sales binding set via compat today is visible + editable in the editor's Knowledge Base section.

## Self-Review notes (checked against the spec)

- **Spec coverage:** backend `agent_ref` → Task 1; frontend schema/type + strip fix → Task 2; `KnowledgeBaseSection` + wiring → Task 3; unknown-token preservation → Task 3 (orphan rows + test); strip-regression guard → Task 2 (schema test) grounded in the zod `preprocess`. Draft-edit/apply-on-Publish → reuses existing flow (no bind endpoint). No migration/env keys. ✔
- **Type consistency:** `KbSummary.agent_ref` (backend Task 1 / TS Task 2) and the form field `llm.knowledge_base_ids: string[]` (Task 2) are used identically in Task 3. `useKnowledgeBases()` returns `KbSummary[]` with `agent_ref`. ✔
- **No placeholders:** every code step carries the actual code; commands have expected output. ✔
- **Known-unknown flagged:** Task 2 Step 5 warns that making `agent_ref` required on `KbSummary` may force it onto existing `KbSummary` test fixtures — the implementer updates them to keep tsc green. ✔
