import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { AgentConfig, Me, ProfileDetail, VersionDetail } from "../types/api";

// Mock the api module: getMock serves the page/session/version reads, putMock is the
// saveDraft (the behavior under test), postMock would be the publish confirm.
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

import { ProfileEditorPage } from "../features/editor/ProfileEditorPage";

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
      inbound_personalization_template: "with {elder_name}",
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

function profile(): ProfileDetail {
  return {
    id: "p1",
    name: "Profile One",
    description: null,
    status: "active",
    is_default_inbound: false,
    is_default_outbound: false,
    published_version: 1,
    draft_config: baseConfig(),
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
  if (url === "/v1/admin/profiles/p1/versions/1") {
    return Promise.resolve({
      version: 1,
      note: null,
      published_by: "ops@example.com",
      published_at: "2026-06-01T00:00:00Z",
      config: baseConfig(),
    } satisfies VersionDetail);
  }
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
  // Voicemail is the only section with a plain <textarea> (role textbox); editing it
  // marks the form dirty deterministically (no Monaco needed).
  await user.click(screen.getByRole("tab", { name: "Voicemail" }));
  await user.type(await screen.findByRole("textbox"), "leave a message");
}

describe("ProfileEditorPage publish flow", () => {
  beforeEach(() => {
    getMock.mockReset();
    putMock.mockReset();
    postMock.mockReset();
    getMock.mockImplementation(routeGet);
  });
  afterEach(() => vi.clearAllMocks());

  it("persists a dirty draft BEFORE opening the publish dialog", async () => {
    putMock.mockResolvedValue(profile());
    renderPage();
    const user = userEvent.setup();
    await screen.findByRole("button", { name: "Publish" }); // admin + profile loaded
    await makeDirty(user);

    await user.click(screen.getByRole("button", { name: "Publish" }));

    // The saved draft is persisted first (so the server freezes what the diff shows)...
    await waitFor(() =>
      expect(putMock).toHaveBeenCalledWith(
        "/v1/admin/profiles/p1/draft",
        expect.objectContaining({ config: expect.anything() }),
      ),
    );
    // ...then the dialog opens and fetches the live version for the diff.
    await waitFor(() =>
      expect(getMock).toHaveBeenCalledWith("/v1/admin/profiles/p1/versions/1"),
    );
  });

  it("skips the redundant save when the form is not dirty", async () => {
    renderPage();
    const user = userEvent.setup();
    await screen.findByRole("button", { name: "Publish" });

    await user.click(screen.getByRole("button", { name: "Publish" }));

    await waitFor(() =>
      expect(getMock).toHaveBeenCalledWith("/v1/admin/profiles/p1/versions/1"),
    );
    expect(putMock).not.toHaveBeenCalled();
  });

  it("keeps the dialog closed when persisting the dirty draft fails (422)", async () => {
    const { ApiError } = await import("../lib/api");
    putMock.mockRejectedValue(
      new ApiError(
        422,
        JSON.stringify([
          { loc: ["body", "config", "voicemail_detection", "window_s"], msg: "bad" },
        ]),
      ),
    );
    renderPage();
    const user = userEvent.setup();
    await screen.findByRole("button", { name: "Publish" });
    await makeDirty(user);

    await user.click(screen.getByRole("button", { name: "Publish" }));

    await waitFor(() => expect(putMock).toHaveBeenCalled());
    // The 422 short-circuits onPublishClick: the dialog never opens, so the live
    // version is never fetched.
    expect(getMock).not.toHaveBeenCalledWith("/v1/admin/profiles/p1/versions/1");
  });
});
