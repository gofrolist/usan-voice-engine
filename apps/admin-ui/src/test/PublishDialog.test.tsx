import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement } from "react";
import type { AgentConfig, VersionDetail } from "../types/api";

// Mock the api module so the dialog's useVersion (GET) returns a live config and
// usePublish (POST) is observable.
const getMock = vi.fn();
const postMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    get: (u: string) => getMock(u),
    post: (u: string, b?: unknown) => postMock(u, b),
    put: vi.fn(),
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

import { PublishDialog } from "../features/editor/PublishDialog";

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

function renderWithProviders(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("PublishDialog", () => {
  beforeEach(() => {
    getMock.mockReset();
    postMock.mockReset();
  });
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("renders the diff between the live version and the edited draft", async () => {
    const live: VersionDetail = {
      version: 3,
      note: "previous",
      published_by: "ops@example.com",
      published_at: "2026-06-01T00:00:00Z",
      config: baseConfig(),
    };
    getMock.mockResolvedValue(live);

    const draft = baseConfig();
    draft.prompts.greeting = "Good morning";
    draft.timing.max_call_duration_s = 3600;

    renderWithProviders(
      <PublishDialog
        open
        onClose={() => {}}
        profileId="p1"
        draftConfig={draft}
        publishedVersion={3}
        onPublished={() => {}}
      />,
    );

    // The live version is fetched for the diff.
    await waitFor(() => expect(getMock).toHaveBeenCalledWith("/v1/admin/profiles/p1/versions/3"));

    // Exactly the two changed fields show up as diff rows.
    const rows = await screen.findAllByTestId("diff-row");
    expect(rows).toHaveLength(2);
    expect(screen.getByText("Good morning")).toBeInTheDocument();
    expect(screen.getByText("3600")).toBeInTheDocument();
  });

  it("calls the publish mutation with the typed note on confirm", async () => {
    const live: VersionDetail = {
      version: 3,
      note: null,
      published_by: "ops@example.com",
      published_at: "2026-06-01T00:00:00Z",
      config: baseConfig(),
    };
    getMock.mockResolvedValue(live);
    postMock.mockResolvedValue({
      version: 4,
      note: "tweak greeting",
      published_by: "me@example.com",
      published_at: "2026-06-08T00:00:00Z",
    });

    const draft = baseConfig();
    draft.prompts.greeting = "Good morning";
    const onPublished = vi.fn();

    renderWithProviders(
      <PublishDialog
        open
        onClose={() => {}}
        profileId="p1"
        draftConfig={draft}
        publishedVersion={3}
        onPublished={onPublished}
      />,
    );

    const user = userEvent.setup();
    await screen.findAllByTestId("diff-row");

    await user.type(screen.getByLabelText(/Note/i), "tweak greeting");
    await user.click(screen.getByRole("button", { name: /^Publish$/ }));

    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/v1/admin/profiles/p1/publish", {
        note: "tweak greeting",
      }),
    );
    await waitFor(() => expect(onPublished).toHaveBeenCalled());
  });
});
