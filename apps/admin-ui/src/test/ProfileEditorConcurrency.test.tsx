// apps/admin-ui/src/test/ProfileEditorConcurrency.test.tsx
// Optimistic-concurrency reload UX (FR-032 / SC-011): a 409 from saveDraft shows a
// reload banner — never a silent local clobber — and the editor sends the loaded
// draft_revision as expected_revision.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { AgentConfig, Me, ProfileDetail } from "../types/api";

const getMock = vi.fn();
const putMock = vi.fn();
const postMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    get: (u: string) => getMock(u),
    put: (u: string, b?: unknown) => putMock(u, b),
    post: (u: string, b?: unknown) => postMock(u, b),
    del: vi.fn(),
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
  pushToast: (message: string, tone?: string) => pushToastMock(message, tone),
}));

import { ProfileEditorPage } from "../features/editor/ProfileEditorPage";

const CONFLICT_DETAIL =
  "This draft was changed by someone else since you opened it. " +
  "Reload to see the latest, then re-apply your changes.";

function baseConfig(): AgentConfig {
  return {
    prompts: {
      system_prompt: "sys",
      greeting: "Hello there",
      recording_disclosure: "recorded",
      voicemail_message: "vm",
      checkin_flow_instructions: "flow",
      goodbye_message: "bye",
      inbound_opening: "open",
      inbound_personalization_template: "with {contact_name}",
    },
    voice: { cartesia_voice_id: null, tts_model: null, speed: null, language: null },
    llm: { model: "gemini-3.1-flash-lite", temperature: null },
    stt: { model: "ink-whisper", language: null },
    timing: { answer_timeout_s: 50, max_call_duration_s: 1800 },
    tools: { enabled: ["log_wellness", "end_call"] },
    voicemail_detection: { window_s: 3, trigger_phrases: [] },
    speech_advanced: {
      vad_min_silence_s: null,
      vad_activation_threshold: null,
      turn_detection: null,
      min_endpointing_delay_s: null,
      max_endpointing_delay_s: null,
      min_interruption_duration_s: null,
      min_interruption_words: null,
    },
  };
}

function profile(revision = 1): ProfileDetail {
  return {
    id: "p1",
    name: "Profile One",
    description: null,
    status: "active",
    is_default_inbound: false,
    is_default_outbound: false,
    published_version: 1,
    draft_config: baseConfig(),
    draft_revision: revision,
    created_by: null,
    updated_by: null,
    created_at: "2026-06-01T00:00:00Z",
    updated_at: "2026-06-01T00:00:00Z",
  };
}

function routeGet(url: string): Promise<unknown> {
  if (url === "/v1/auth/me") {
    return Promise.resolve({ email: "me@example.com", role: "admin" } satisfies Me);
  }
  if (url === "/v1/admin/profiles/p1") return Promise.resolve(profile());
  return Promise.reject(new Error(`unexpected GET ${url}`));
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/profiles/p1"]}>
        <Routes>
          <Route path="/profiles/:id" element={<ProfileEditorPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function makeDirty(user: ReturnType<typeof userEvent.setup>) {
  // Voicemail is the only section with a plain <textarea>; editing it marks dirty.
  await user.click(screen.getByRole("tab", { name: "Voicemail" }));
  await user.type(await screen.findByRole("textbox"), "x");
}

describe("ProfileEditorPage optimistic concurrency", () => {
  beforeEach(() => {
    getMock.mockReset();
    putMock.mockReset();
    postMock.mockReset();
    getMock.mockImplementation(routeGet);
  });
  afterEach(() => vi.clearAllMocks());

  it("sends the loaded draft_revision as expected_revision on save", async () => {
    putMock.mockResolvedValue(profile(2));
    renderPage();
    const user = userEvent.setup();
    await screen.findByRole("button", { name: "Save draft" });
    await makeDirty(user);
    await user.click(screen.getByRole("button", { name: "Save draft" }));

    await waitFor(() =>
      expect(putMock).toHaveBeenCalledWith(
        "/v1/admin/profiles/p1/draft",
        expect.objectContaining({ expected_revision: 1 }),
      ),
    );
  });

  it("shows a reload banner on 409 instead of silently clobbering", async () => {
    const { ApiError } = await import("../lib/api");
    putMock.mockRejectedValue(new ApiError(409, CONFLICT_DETAIL));
    renderPage();
    const user = userEvent.setup();
    await screen.findByRole("button", { name: "Save draft" });
    await makeDirty(user);
    await user.click(screen.getByRole("button", { name: "Save draft" }));

    // The conflict surfaces as a visible banner with a Reload action (no silent loss).
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/changed since you opened it/i);
    expect(screen.getByRole("button", { name: "Reload" })).toBeInTheDocument();
    // The generic, PHI-free message is toasted; never another actor's identity.
    // (The toast wrapper forwards an undefined `tone` second arg.)
    expect(pushToastMock).toHaveBeenCalledWith(CONFLICT_DETAIL, undefined);
  });

  it("Reload re-fetches the latest draft and clears the banner", async () => {
    const { ApiError } = await import("../lib/api");
    putMock.mockRejectedValue(new ApiError(409, CONFLICT_DETAIL));
    // Auto-confirm the discard prompt so the reload proceeds.
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    renderPage();
    const user = userEvent.setup();
    await screen.findByRole("button", { name: "Save draft" });
    await makeDirty(user);
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    await screen.findByRole("alert");

    const getCallsBefore = getMock.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "Reload" }));

    // Re-fetches the profile (latest revision) and the banner goes away.
    await waitFor(() => expect(getMock.mock.calls.length).toBeGreaterThan(getCallsBefore));
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    confirmSpy.mockRestore();
  });

  it("Reload discards dirty edits even when the refetched draft is byte-identical", async () => {
    // Regression guard: a revision-only 409 refetches a STRUCTURALLY-IDENTICAL draft, so
    // React Query's structuralSharing keeps the same `profile` reference and the load
    // effect never re-runs. The discard must still happen (explicit reset in
    // handleReloadDraft) — otherwise the operator's discarded edits silently survive.
    const { ApiError } = await import("../lib/api");
    putMock.mockRejectedValue(new ApiError(409, CONFLICT_DETAIL));
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    renderPage();
    const user = userEvent.setup();
    await screen.findByRole("button", { name: "Save draft" });

    // Dirty the voicemail textarea; capture its server value first, then confirm it changed.
    await user.click(screen.getByRole("tab", { name: "Voicemail" }));
    const textarea = (await screen.findByRole("textbox")) as HTMLTextAreaElement;
    const original = textarea.value;
    await user.type(textarea, "DIRTY");
    expect(textarea.value).not.toBe(original);

    // 409, then Reload (refetch returns the identical revision-1 draft → same ref).
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    await screen.findByRole("alert");
    await user.click(screen.getByRole("button", { name: "Reload" }));

    // The dirty edit is dropped: the field is back to the server's value, not kept.
    await waitFor(() =>
      expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe(original),
    );
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    confirmSpy.mockRestore();
  });
});
