# Admin UI Phase 1 — Unblock + Retell Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the editor's overlapping top bar, raise the prompt size limits so a real ~12k-char agent prompt (with `{{variables}}`) can be saved, and restyle the admin console to look and feel like Retell.

**Architecture:** Frontend-heavy change in `apps/admin-ui` (React + Vite + TS + Tailwind) plus a small, safe backend change in `apps/api` (raise `system_prompt`/`checkin_flow_instructions` caps; stop rejecting `{`/`}` on those two free-form fields only — they are never passed to `str.format`, unlike `inbound_personalization_template`). The agent (`services/agent`) needs **no change**: its config copy has no length caps or brace validators. The editor frame is restructured from a fragile negative-margin sticky header into a non-scrolling app frame whose panes scroll internally, so the toolbar can never overlap content.

**Tech Stack:** React 18, react-hook-form + Zod, TanStack Query, react-router, Tailwind 3, Monaco; Pydantic v2 (api); Vitest + Testing Library; pytest.

**Out of scope (later phases):** real `{{variable}}` substitution + variable catalog (Phase 2); data-driven tool registry / custom function editing / executing new tools (Phase 3). Phase 1 only *unblocks pasting* and *restyles*.

---

## Invariants the redesign MUST preserve (tests + e2e depend on them)

- Section navigation items remain real tabs: `role="tab"` with the **exact** labels `Prompts`, `Voice`, `LLM`, `STT`, `Speech (Advanced)`, `Timing`, `Tools`, `Voicemail`. (`src/test/ProfileEditorPage.test.tsx` clicks `getByRole("tab", { name: "Voicemail" })`; `e2e/smoke.spec.ts` clicks `tab /prompts/i`.) The existing `Tabs` component already renders `role="tab"`.
- Buttons named exactly **`Save draft`** and **`Publish`** stay on the editor (tests use `{ name: "Publish" }` and `/save draft/i`).
- Form-control ids stay equal to the dotted config path, e.g. `id="prompts.greeting"` (`e2e` fills `#prompts\.greeting`).
- The **Voicemail** section keeps a single plain `<textarea>` (role `textbox`) so `makeDirty()` can type into it (`ProfileEditorPage.test.tsx:109-114`).
- The publish flow logic in `ProfileEditorPage` (save-dirty-before-publish, 422 short-circuit) is unchanged.

## File map

**Backend (`apps/api`)**
- Modify: `src/usan_api/schemas/agent_config.py` — caps + brace-validator field list.
- Modify: `tests/test_agent_config_schema.py` — add cap/brace coverage.

**Frontend schema (`apps/admin-ui`)**
- Modify: `src/config/agentConfigSchema.ts` — caps + per-field brace toggle.
- Modify: `src/config/fieldMeta.ts` — help text.
- Modify: `src/test/agentConfigSchema.test.ts` — add cap/brace coverage.

**Frontend design system**
- Modify: `tailwind.config.js` — fonts, shadow.
- Modify: `src/index.css` — base layer (bg, font smoothing, `.prompt-var-token`).
- Modify primitives: `src/components/ui/{button,badge,tabs,input,textarea,select,table,spinner}.tsx`.

**Frontend shell + editor**
- Modify: `src/components/AppLayout.tsx` — non-scrolling frame.
- Create: `src/components/PageBody.tsx` — scroll+max-width wrapper for simple pages.
- Modify: `src/components/NavSidebar.tsx` — grouped nav (Build / Config / System), brand.
- Modify: `src/features/editor/ProfileEditorPage.tsx` — flex-column frame, toolbar, 2-pane.
- Create: `src/features/editor/EditorToolbar.tsx` — name/status + model·voice·lang chips + actions.
- Create: `src/features/editor/SectionRail.tsx` — right-hand section list (tabs) with value summaries.
- Modify: `src/features/editor/sections/PromptEditor.tsx` — taller hero editor + `{{token}}` highlight.
- Modify: `src/features/editor/sections/PromptsSection.tsx` — system prompt hero row height.
- Modify: `src/features/editor/sections/ToolsSection.tsx` — Retell-style "Functions" toggle list.
- Modify: simple pages for shell consistency: `ProfilesListPage`, `EldersPage`, `DefaultsPage`, `AuditPage`, `AdminUsersPage`, `VersionHistoryPage` (wrap in `PageBody`, heading style).

## Design tokens (Retell-adjacent)

- Surfaces: app `bg-slate-50`; cards/panels `bg-white border border-slate-200 rounded-xl shadow-card`.
- Text: `text-slate-900` body, `text-slate-500` muted, `text-slate-400` faint.
- Accent (links, active nav, focus ring, selected tab): **indigo-600**.
- Primary CTA (`Publish`): dark **slate-900** (matches Retell's dark Publish); `Save draft` = subtle `bg-white border`.
- Radii: controls `rounded-lg`; cards `rounded-xl`. Font: Inter → system stack.

---

## Task 1: Backend — raise caps, relax braces on free-form fields

**Files:**
- Modify: `apps/api/src/usan_api/schemas/agent_config.py:35-55`
- Test: `apps/api/tests/test_agent_config_schema.py`

- [ ] **Step 1: Write failing tests** — append to `apps/api/tests/test_agent_config_schema.py`:

```python
def test_system_prompt_accepts_long_text_with_braces():
    # A real migrated agent prompt (~12k chars, full of {{vars}}) must save.
    # system_prompt is passed straight to the LLM (pipeline.py:113), never
    # str.format-ed, so braces are safe there.
    cfg = DEFAULT_AGENT_CONFIG.prompts.model_dump()
    cfg["system_prompt"] = ("You are Clara. Greet {{first_name}} in {{state}}.\n" * 300)[:12000]
    parsed = PromptsConfig.model_validate(cfg)
    assert "{{first_name}}" in parsed.system_prompt


def test_checkin_flow_accepts_braces():
    cfg = DEFAULT_AGENT_CONFIG.prompts.model_dump()
    cfg["checkin_flow_instructions"] = "Ask about {{med_name}} at {{time}}."
    assert PromptsConfig.model_validate(cfg)


def test_system_prompt_rejects_over_cap():
    cfg = DEFAULT_AGENT_CONFIG.prompts.model_dump()
    cfg["system_prompt"] = "x" * 24001
    with pytest.raises(ValidationError):
        PromptsConfig.model_validate(cfg)
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/api && uv run pytest tests/test_agent_config_schema.py -q -k "long_text_with_braces or checkin_flow_accepts or over_cap"`
Expected: the two "accepts braces" tests FAIL with `ValidationError` ("must not contain '{' or '}'").

- [ ] **Step 3: Implement** — in `agent_config.py`, change the two field caps and remove `system_prompt`/`checkin_flow_instructions` from the brace validator:

```python
class PromptsConfig(BaseModel):
    # system_prompt and checkin_flow_instructions are large free-form behavior fields
    # that the agent passes verbatim to the LLM (never to str.format), so they allow
    # braces and a generous cap. Only inbound_personalization_template is str.format-ed.
    system_prompt: str = Field(min_length=1, max_length=24000)
    greeting: str = Field(min_length=1, max_length=1000)
    recording_disclosure: str = Field(min_length=1, max_length=1000)
    voicemail_message: str = Field(min_length=1, max_length=1000)
    checkin_flow_instructions: str = Field(min_length=1, max_length=24000)
    goodbye_message: str = Field(min_length=1, max_length=1000)
    inbound_opening: str = Field(min_length=1, max_length=1000)
    inbound_personalization_template: str = Field(min_length=1, max_length=6000)

    # Brace rejection applies ONLY to the short, literal fields. system_prompt and
    # checkin_flow_instructions are intentionally excluded (they hold {{variable}}
    # tokens for migrated prompts and are never str.format-ed). DO NOT pass these two
    # fields to str.format anywhere — that would reintroduce the injection vector.
    @field_validator(
        "greeting",
        "recording_disclosure",
        "voicemail_message",
        "goodbye_message",
        "inbound_opening",
    )
    @classmethod
    def _no_braces(cls, v: str) -> str:
        return _reject_braces(v)
```

(Leave `_only_allowed_slots` on `inbound_personalization_template` untouched.)

- [ ] **Step 4: Run the full schema suite**

Run: `cd apps/api && uv run pytest tests/test_agent_config_schema.py -q`
Expected: PASS (existing `test_prompt_field_rejects_braces` still passes — it uses `greeting`, still strict).

- [ ] **Step 5: Lint + types + commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
cd ../.. && git add apps/api/src/usan_api/schemas/agent_config.py apps/api/tests/test_agent_config_schema.py
git commit -m "feat(api): raise system_prompt/checkin_flow caps to 24k; allow braces in free-form prompt fields"
```

---

## Task 2: Frontend schema — mirror caps/braces + help text

**Files:**
- Modify: `apps/admin-ui/src/config/agentConfigSchema.ts:27-72`
- Modify: `apps/admin-ui/src/config/fieldMeta.ts:34-53`
- Test: `apps/admin-ui/src/test/agentConfigSchema.test.ts`

- [ ] **Step 1: Write failing tests** — append to `agentConfigSchema.test.ts` (inside the `describe`):

```ts
  it("accepts a long system_prompt containing {{braces}}", () => {
    const cfg = validConfig();
    cfg.prompts.system_prompt = "You are Clara. Greet {{first_name}}.\n".repeat(300).slice(0, 12000);
    expect(agentConfigSchema.safeParse(cfg).success).toBe(true);
  });

  it("accepts braces in checkin_flow_instructions", () => {
    const cfg = validConfig();
    cfg.prompts.checkin_flow_instructions = "Ask about {{med_name}} at {{time}}.";
    expect(agentConfigSchema.safeParse(cfg).success).toBe(true);
  });

  it("rejects a system_prompt over 24000 chars", () => {
    const cfg = validConfig();
    cfg.prompts.system_prompt = "x".repeat(24001);
    expect(agentConfigSchema.safeParse(cfg).success).toBe(false);
  });
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/admin-ui && npx vitest run src/test/agentConfigSchema.test.ts`
Expected: the two "accepts braces" tests FAIL (brace refinement rejects them).

- [ ] **Step 3: Implement** — make `promptField` take an `allowBraces` flag and bump the two caps:

```ts
function promptField(maxLength: number, label: string, allowBraces = false) {
  const base = z
    .string()
    .min(1, `${label} is required`)
    .max(maxLength, `${label} must be at most ${maxLength} characters`);
  return allowBraces ? base : base.superRefine(noBraces(label));
}

export const promptsSchema = z.object({
  // Large free-form behavior fields: braces allowed (hold {{variable}} tokens; never
  // str.format-ed). Mirrors apps/api PromptsConfig.
  system_prompt: promptField(24000, "System prompt", true),
  greeting: promptField(1000, "Greeting"),
  recording_disclosure: promptField(1000, "Recording disclosure"),
  voicemail_message: promptField(1000, "Voicemail message"),
  checkin_flow_instructions: promptField(24000, "Check-in flow instructions", true),
  goodbye_message: promptField(1000, "Goodbye message"),
  inbound_opening: promptField(1000, "Inbound opening"),
  inbound_personalization_template: personalizationTemplate,
});
```

- [ ] **Step 4: Update help text** — in `fieldMeta.ts`:

```ts
  "prompts.system_prompt": {
    label: "System prompt",
    help: "Base persona/instructions. Supports {{variables}}. Up to 24,000 chars.",
  },
```
and
```ts
  "prompts.checkin_flow_instructions": {
    label: "Check-in flow instructions",
    help: "Step-by-step check-in script. Supports {{variables}}. Up to 24,000 chars.",
  },
```

- [ ] **Step 5: Run tests + typecheck**

Run: `cd apps/admin-ui && npx vitest run src/test/agentConfigSchema.test.ts && npm run typecheck`
Expected: PASS (the existing "rejects a brace in the greeting" stays green — greeting unchanged).

- [ ] **Step 6: Commit**

```bash
git add apps/admin-ui/src/config/agentConfigSchema.ts apps/admin-ui/src/config/fieldMeta.ts apps/admin-ui/src/test/agentConfigSchema.test.ts
git commit -m "feat(admin-ui): mirror 24k prompt caps + allow braces in free-form prompt fields"
```

---

## Task 3: Design tokens + UI primitive polish

**Files:** `tailwind.config.js`, `src/index.css`, `src/components/ui/{button,badge,tabs,input,textarea,select,table,spinner}.tsx`

- [ ] **Step 1: tailwind.config.js** — extend theme:

```js
/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'ui-sans-serif', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'Helvetica Neue', 'Arial', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', 'monospace'],
      },
      boxShadow: {
        card: '0 1px 2px 0 rgb(15 23 42 / 0.04), 0 1px 3px 0 rgb(15 23 42 / 0.06)',
      },
    },
  },
  plugins: [],
};
```

- [ ] **Step 2: src/index.css** — base layer + token style:

```css
@tailwind base;
@tailwind components;
@tailwind utilities;

@layer base {
  html {
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
    text-rendering: optimizeLegibility;
  }
  body {
    @apply bg-slate-50 text-slate-900;
  }
}

/* {{variable}} / {slot} token highlight inside Monaco prompt editors. */
.prompt-var-token {
  color: #4f46e5;
  background: #eef2ff;
  border-radius: 3px;
}
```

- [ ] **Step 3: Restyle primitives** (keep prop APIs identical; change only classes):
  - `button.tsx` VARIANTS → `primary: "bg-slate-900 text-white hover:bg-slate-800 disabled:bg-slate-400"`, `secondary: "border border-slate-200 bg-white text-slate-700 hover:bg-slate-50 disabled:opacity-50"`, `danger: "bg-red-600 text-white hover:bg-red-700 disabled:bg-red-300"`, `ghost: "bg-transparent text-slate-600 hover:bg-slate-100 disabled:opacity-50"`; base → `rounded-lg px-3 py-1.5 text-sm font-medium transition-colors disabled:cursor-not-allowed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 focus-visible:ring-offset-1`.
  - `badge.tsx` TONES → `gray: "bg-slate-100 text-slate-600"` (keep tone keys `green/blue/red/amber/gray`); base unchanged (rounded-full).
  - `input.tsx` / `textarea.tsx` / `select.tsx` → border `border-slate-300`, `rounded-lg`, focus `focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500`.
  - `table.tsx` → wrapper `rounded-xl border border-slate-200 bg-white shadow-card`; `Thead` `bg-slate-50 text-slate-500`; `Tbody` `divide-slate-100`; `Tr` hover `hover:bg-slate-50`.
  - `tabs.tsx` → active `bg-indigo-50 font-medium text-indigo-700`, inactive `text-slate-600 hover:bg-slate-100`, `rounded-lg`.
  - `spinner.tsx` → change any `text-gray-*` → `text-slate-*` (verify only).

- [ ] **Step 4: Verify build + existing tests still green**

Run: `cd apps/admin-ui && npm run typecheck && npx vitest run`
Expected: PASS (no API changes to primitives → component tests unaffected).

- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/tailwind.config.js apps/admin-ui/src/index.css apps/admin-ui/src/components/ui
git commit -m "feat(admin-ui): Retell-style design tokens + primitive polish"
```

---

## Task 4: App shell — grouped sidebar + non-scrolling frame

**Files:** `src/components/AppLayout.tsx`, `src/components/NavSidebar.tsx`, create `src/components/PageBody.tsx`; update simple pages to use `PageBody`.

- [ ] **Step 1: AppLayout** — app frame no longer scrolls; panes scroll internally:

```tsx
import { Outlet } from "react-router-dom";
import { NavSidebar } from "./NavSidebar";
import { ErrorToast } from "./ErrorToast";

export function AppLayout() {
  return (
    <div className="flex h-screen overflow-hidden bg-slate-50 text-slate-900">
      <NavSidebar />
      <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <Outlet />
      </main>
      <ErrorToast />
    </div>
  );
}
```

- [ ] **Step 2: PageBody** — create `src/components/PageBody.tsx`:

```tsx
import type { ReactNode } from "react";

// Scroll container + centered max-width for the simple (non-editor) pages. The app
// frame (AppLayout > main) no longer scrolls, so each page owns its own scroll.
export function PageBody({ children }: { children: ReactNode }) {
  return (
    <div className="flex-1 overflow-y-auto">
      <div className="mx-auto w-full max-w-6xl px-8 py-7">{children}</div>
    </div>
  );
}
```

- [ ] **Step 3: NavSidebar** — grouped sections + brand; preserve nav targets/labels. Groups: `Build`→Profiles; `Config`→Elders(admin), Defaults; `System`→Audit, Admin Users(admin). Container `w-60 shrink-0 border-r border-slate-200 bg-white`; group headings `text-[11px] font-semibold uppercase tracking-wider text-slate-400`; active link `bg-indigo-50 text-indigo-700`, inactive `text-slate-600 hover:bg-slate-100`. Brand row: a small `bg-slate-900` rounded square "U" + "USAN Admin". Keep `logout()` and the email/role footer.

- [ ] **Step 4: Wrap simple pages in `PageBody`** — for each of `ProfilesListPage`, `EldersPage`, `DefaultsPage`, `AuditPage`, `AdminUsersPage`, `VersionHistoryPage`: replace the top-level `<div className="space-y-4">` (or equivalent) with `<PageBody><div className="space-y-5">…</div></PageBody>`, and bump the page `<h1>` to `text-xl font-semibold text-slate-900`. (These pages previously relied on `<main>`'s `p-6`, now removed.) Keep all other markup/logic identical.

- [ ] **Step 5: Verify**

Run: `cd apps/admin-ui && npm run typecheck && npx vitest run`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/admin-ui/src/components apps/admin-ui/src/features
git commit -m "feat(admin-ui): grouped sidebar + non-scrolling app frame (PageBody)"
```

---

## Task 5: Editor — fix overlap + Retell toolbar + 2-pane

**Files:** `src/features/editor/ProfileEditorPage.tsx`; create `EditorToolbar.tsx`, `SectionRail.tsx`.

The editor becomes a full-height flex column inside `main`: a non-scrolling toolbar (`shrink-0`) on top, then a 2-pane body (`flex min-h-0 flex-1`): the active section content on the **left** (scrolls), the section rail on the **right**. Because the toolbar is a flex sibling — not a sticky overlay over a padded scroll box — it can never overlap content. This removes the `-mx-6 -mt-6 sticky` hack entirely.

- [ ] **Step 1: EditorToolbar** — create `src/features/editor/EditorToolbar.tsx`:

```tsx
import { Link } from "react-router-dom";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import type { SectionKey } from "../../config/fieldMeta";

function Chip({ label, value, onClick }: { label: string; value: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2.5 py-1 text-xs text-slate-600 hover:bg-slate-50"
    >
      <span className="text-slate-400">{label}</span>
      <span className="font-medium text-slate-800">{value}</span>
    </button>
  );
}

interface EditorToolbarProps {
  name: string;
  status: string;
  publishedVersion: number | null;
  dirty: boolean;
  model: string;
  voice: string;
  language: string;
  isAdmin: boolean;
  saving: boolean;
  profileId: string;
  onJump: (s: SectionKey) => void;
  onSave: () => void;
  onPublish: () => void;
}

export function EditorToolbar(props: EditorToolbarProps) {
  return (
    <div className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-slate-200 bg-white px-8 py-3">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="truncate text-lg font-semibold text-slate-900">{props.name}</h1>
          <Badge tone={props.status === "active" ? "green" : "gray"}>{props.status}</Badge>
          {props.publishedVersion !== null ? (
            <Badge tone="blue">live v{props.publishedVersion}</Badge>
          ) : (
            <Badge tone="gray">unpublished</Badge>
          )}
          {props.dirty ? <Badge tone="amber">unsaved changes</Badge> : null}
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-2">
          <Chip label="Model" value={props.model} onClick={() => props.onJump("llm")} />
          <Chip label="Voice" value={props.voice} onClick={() => props.onJump("voice")} />
          <Chip label="Lang" value={props.language} onClick={() => props.onJump("voice")} />
          <Link to={`/profiles/${props.profileId}/versions`} className="text-xs text-indigo-600 hover:underline">
            Version history
          </Link>
        </div>
      </div>
      {props.isAdmin ? (
        <div className="flex shrink-0 gap-2">
          <Button variant="secondary" onClick={props.onSave} disabled={props.saving}>
            {props.saving ? "Saving…" : "Save draft"}
          </Button>
          <Button onClick={props.onPublish}>Publish</Button>
        </div>
      ) : (
        <span className="text-xs text-slate-500">Read-only (viewer role)</span>
      )}
    </div>
  );
}
```

- [ ] **Step 2: SectionRail** — create `src/features/editor/SectionRail.tsx`. The summary span is `aria-hidden` so the tab's accessible name stays exactly `SECTION_LABELS[key]` (keeps `getByRole("tab", { name: "Voicemail" })` green):

```tsx
import { cn } from "../../lib/cn";
import { SECTION_LABELS, type SectionKey } from "../../config/fieldMeta";

interface SectionRailProps {
  order: SectionKey[];
  active: SectionKey;
  summaries: Partial<Record<SectionKey, string>>;
  onSelect: (s: SectionKey) => void;
}

export function SectionRail({ order, active, summaries, onSelect }: SectionRailProps) {
  return (
    <nav role="tablist" className="flex flex-col gap-1">
      {order.map((key) => (
        <button
          key={key}
          role="tab"
          aria-selected={key === active}
          onClick={() => onSelect(key)}
          className={cn(
            "flex items-center justify-between rounded-lg px-3 py-2 text-left text-sm",
            key === active
              ? "bg-indigo-50 font-medium text-indigo-700"
              : "text-slate-600 hover:bg-slate-100",
          )}
        >
          <span>{SECTION_LABELS[key]}</span>
          {summaries[key] ? (
            <span aria-hidden="true" className="ml-2 truncate text-xs text-slate-400">
              {summaries[key]}
            </span>
          ) : null}
        </button>
      ))}
    </nav>
  );
}
```

- [ ] **Step 3: Rewrite ProfileEditorPage render** — keep ALL hooks/handlers above the return unchanged; replace the returned JSX. Also wrap the loading/error early-returns' content in `<div className="p-8">…</div>` (main has no padding now). New main render:

```tsx
  const draftValues = form.watch();
  const summaries: Partial<Record<SectionKey, string>> = {
    llm: draftValues.llm?.model,
    voice: draftValues.voice?.cartesia_voice_id ?? "default",
    tools: `${draftValues.tools?.enabled?.length ?? 0} on`,
    timing: draftValues.timing ? `${draftValues.timing.answer_timeout_s}s` : undefined,
  };

  return (
    <div className="flex h-full flex-col">
      <EditorToolbar
        name={profile.name}
        status={profile.status}
        publishedVersion={profile.published_version}
        dirty={form.formState.isDirty}
        model={draftValues.llm?.model ?? "—"}
        voice={draftValues.voice?.cartesia_voice_id ?? "default"}
        language={draftValues.voice?.language ?? "default"}
        isAdmin={isAdmin}
        saving={saveDraft.isPending}
        profileId={id}
        onJump={(s) => setSection(s)}
        onSave={onSave}
        onPublish={onPublishClick}
      />
      <div className="flex min-h-0 flex-1">
        <div className="min-w-0 flex-1 overflow-y-auto px-8 py-6">
          <div className="mx-auto max-w-3xl">
            <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-slate-500">
              {SECTION_LABELS[section]}
            </h2>
            <form className="min-w-0" onSubmit={onSave}>
              <fieldset disabled={!isAdmin} className="min-w-0">
                {section === "prompts" ? <PromptsSection form={form} /> : null}
                {section === "voice" ? <VoiceSection form={form} /> : null}
                {section === "llm" ? <LLMSection form={form} /> : null}
                {section === "stt" ? <STTSection form={form} /> : null}
                {section === "speech_advanced" ? <SpeechAdvancedSection form={form} /> : null}
                {section === "timing" ? <TimingSection form={form} /> : null}
                {section === "tools" ? <ToolsSection form={form} /> : null}
                {section === "voicemail_detection" ? <VoicemailSection form={form} /> : null}
              </fieldset>
            </form>
          </div>
        </div>
        <aside className="w-64 shrink-0 overflow-y-auto border-l border-slate-200 bg-white px-3 py-4">
          <SectionRail
            order={SECTION_ORDER}
            active={section}
            summaries={summaries}
            onSelect={(s) => setSection(s)}
          />
        </aside>
      </div>
      <PublishDialog
        open={publishOpen}
        onClose={() => setPublishOpen(false)}
        profileId={id}
        draftConfig={draftValues as AgentConfig}
        publishedVersion={profile.published_version}
        onPublished={() => {
          setPublishOpen(false);
          pushToast("Published.", "info");
        }}
      />
    </div>
  );
```

Update imports: add `EditorToolbar`, `SectionRail`; remove now-unused imports (`Tabs`, and `Badge`/`Button` if no longer referenced in this file) to satisfy `--max-warnings 0`. The `tabItems` variable is no longer needed — delete it.

- [ ] **Step 4: Run the editor test (contract check) + typecheck/lint**

Run: `cd apps/admin-ui && npx vitest run src/test/ProfileEditorPage.test.tsx && npm run typecheck && npm run lint`
Expected: PASS — `getByRole("tab", { name: "Voicemail" })`, `Publish`, `Save draft` all still resolve.

- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/src/features/editor
git commit -m "fix(admin-ui): replace sticky-overlap header with toolbar + 2-pane editor frame"
```

---

## Task 6: Prompt hero editor + Functions-style tools

**Files:** `src/features/editor/sections/PromptEditor.tsx`, `src/features/editor/sections/PromptsSection.tsx`, `src/features/editor/sections/ToolsSection.tsx`

- [ ] **Step 1: Taller hero + `{{token}}` highlight in `PromptEditor.tsx`** — add a Monaco `onMount`/`onChange` decoration pass for `{{...}}` and `{...}` tokens (full code):

```tsx
import { Suspense, lazy, useRef } from "react";
import { ErrorBoundary } from "../../../components/ErrorBoundary";
import { Textarea } from "../../../components/ui/textarea";

const MonacoEditor = lazy(async () => {
  const mod = await import("@monaco-editor/react");
  return { default: mod.default };
});

interface PromptEditorProps {
  id: string;
  value: string;
  onChange: (value: string) => void;
  rows?: number;
}

function Fallback({ id, value, onChange, rows = 6 }: PromptEditorProps) {
  return <Textarea id={id} value={value} rows={rows} onChange={(e) => onChange(e.target.value)} />;
}

// Highlight {{variable}} (and bare {slot}) tokens so migrated Retell prompts read well.
function decorate(editor: any, monaco: any) {
  const model = editor.getModel();
  if (!model) return;
  const text: string = model.getValue();
  const re = /\{\{[^}]+\}\}|\{[^{}]+\}/g;
  const decos: any[] = [];
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    const start = model.getPositionAt(m.index);
    const end = model.getPositionAt(m.index + m[0].length);
    decos.push({
      range: new monaco.Range(start.lineNumber, start.column, end.lineNumber, end.column),
      options: { inlineClassName: "prompt-var-token" },
    });
  }
  editor.__varCollection = editor.deltaDecorations(editor.__varCollection ?? [], decos);
}

export function PromptEditor(props: PromptEditorProps) {
  const { value, onChange, rows = 6 } = props;
  const editorRef = useRef<any>(null);
  const monacoRef = useRef<any>(null);
  return (
    <div className="overflow-hidden rounded-lg border border-slate-300">
      <ErrorBoundary fallback={<Fallback {...props} />}>
        <Suspense fallback={<Fallback {...props} />}>
          <MonacoEditor
            height={`${Math.max(rows, 4) * 22}px`}
            defaultLanguage="markdown"
            value={value}
            onChange={(v) => {
              onChange(v ?? "");
              if (editorRef.current && monacoRef.current) decorate(editorRef.current, monacoRef.current);
            }}
            onMount={(editor, monaco) => {
              editorRef.current = editor;
              monacoRef.current = monaco;
              decorate(editor, monaco);
            }}
            options={{
              minimap: { enabled: false },
              lineNumbers: "off",
              wordWrap: "on",
              fontSize: 13,
              scrollBeyondLastLine: false,
              renderLineHighlight: "none",
              folding: false,
              padding: { top: 10, bottom: 10 },
            }}
          />
        </Suspense>
      </ErrorBoundary>
    </div>
  );
}
```

- [ ] **Step 2: System prompt as hero in `PromptsSection.tsx`** — replace the `LARGE` set with a `rowsFor` helper: `system_prompt` → 18; `checkin_flow_instructions` & `inbound_personalization_template` → 12; everything else → 4. Pass `rows={rowsFor(key)}` to `PromptEditor`.

- [ ] **Step 3: ToolsSection → Functions list** — keep the `Controller`/`toggle`/`TOOL_NAMES` logic; restyle each `<li>` as a card with the mono name + description and a checkbox on the right:

```tsx
<li key={tool} className="flex items-start justify-between gap-3 rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-card">
  <label htmlFor={`tool-${tool}`} className="min-w-0">
    <span className="font-mono text-sm text-slate-900">{tool}</span>
    <span className="mt-0.5 block text-xs text-slate-500">{TOOL_HELP[tool]}</span>
  </label>
  <input id={`tool-${tool}`} type="checkbox" className="mt-1 h-4 w-4 accent-indigo-600"
         checked={enabled.has(tool)} onChange={(e) => toggle(tool, e.target.checked)} />
</li>
```
Header caption: "Functions the agent can call during a call."

- [ ] **Step 4: Verify build + tests + commit**

Run: `cd apps/admin-ui && npm run typecheck && npx vitest run`
Expected: PASS.

```bash
git add apps/admin-ui/src/features/editor apps/admin-ui/src/index.css
git commit -m "feat(admin-ui): hero prompt editor with {{token}} highlight + Functions-style tools"
```

---

## Task 7: Restyle simple pages for shell consistency

**Files:** `ProfilesListPage.tsx`, `EldersPage.tsx`, `DefaultsPage.tsx`, `AuditPage.tsx`, `AdminUsersPage.tsx`, `VersionHistoryPage.tsx`, `NewProfileDialog.tsx`, `ConfirmDialog.tsx`, `PublishDialog.tsx`, `DiffView.tsx`

- [ ] **Step 1:** Confirm each simple page is wrapped in `PageBody` (Task 4 Step 4). Standardize headings to `text-xl font-semibold text-slate-900` and section gaps to `space-y-5`.
- [ ] **Step 2:** Replace remaining `text-gray-*`/`border-gray-*`/`bg-gray-*` with the `slate` equivalents across these files (search: `grep -rn "gray-" src/features src/components`). Dialog panels → `rounded-xl ... shadow-card`.
- [ ] **Step 3:** Verify

Run: `cd apps/admin-ui && npm run typecheck && npm run lint && npx vitest run`
Expected: PASS (including `PublishDialog.test.tsx`, `DiffView.test.tsx`, `VoicemailSection.test.tsx`).

- [ ] **Step 4: Commit**

```bash
git add apps/admin-ui/src
git commit -m "style(admin-ui): unify slate palette + card surfaces across pages/dialogs"
```

---

## Task 8: Full verification + visual check

- [ ] **Step 1: Frontend full gate**

Run: `cd apps/admin-ui && npm run lint && npm run typecheck && npx vitest run && npm run build`
Expected: all PASS; `vite build` produces `dist/`.

- [ ] **Step 2: Backend gate**

Run: `cd apps/api && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q`
Expected: all PASS.

- [ ] **Step 3: Visual smoke (manual)** — `cd apps/admin-ui && npm run dev`, open `http://localhost:5173`. With the API running locally and a seeded session, verify: (a) toolbar no longer overlaps content at any scroll position; (b) sidebar groups render; (c) pasting a ~12k prompt with `{{vars}}` into System prompt saves without a validation error; (d) `{{tokens}}` are highlighted. (Without the API the SPA 401-redirects; rely on Vitest + `build` for non-interactive verification.)

- [ ] **Step 4: Push**

```bash
git push -u origin feat/admin-ui-phase1-retell-redesign
```

---

## Self-review checklist

- **Spec coverage:** overlap fix (Task 5) ✓; raise caps (Tasks 1–2) ✓; allow braces to unblock paste (Tasks 1–2) ✓; Retell look — sidebar/toolbar/cards/2-pane/Functions (Tasks 3–7) ✓; model·voice·lang surfaced (Task 5) ✓.
- **Invariants:** `role="tab"` labels preserved (SectionRail + `aria-hidden` summary), `Publish`/`Save draft` retained, field ids = dotted path, Voicemail textarea intact — all called out in Tasks 5–6.
- **No agent change required:** confirmed — `services/agent` config copy has no caps/validators.
- **Type consistency:** `SectionKey`, `SECTION_LABELS`, `SECTION_ORDER` reused as defined; new component props typed.
