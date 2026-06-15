// apps/admin-ui/src/test/DefaultsPage.test.tsx
// US3 (T037): the Defaults page states the per-direction current default, explains
// the resolution order in plain language, shows the built-in fallback read-only,
// links to edit the chosen default profile, and surfaces an ineligible-default
// (archived/unpublished) warning + replacement prompt.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { AgentConfig, DefaultsView, ProfileSummary } from "../types/api";

const getMock = vi.fn();
const postMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: { get: (u: string) => getMock(u), post: (u: string, b?: unknown) => postMock(u, b) },
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      public detail: string,
    ) {
      super(detail);
    }
  },
}));

let isAdmin = true;
vi.mock("../auth/useSession", () => ({
  useIsAdmin: () => isAdmin,
}));

import { DefaultsPage } from "../features/defaults/DefaultsPage";

// A minimal but complete AgentConfig stand-in for the read-only fallback panel.
const FALLBACK: AgentConfig = {
  prompts: {
    system_prompt: "You are a warm check-in assistant.",
    greeting: "Hello! This is your daily check-in.",
    recording_disclosure: "This call is recorded.",
    voicemail_message: "Sorry we missed you.",
    checkin_flow_instructions: "Conduct the check-in.",
    goodbye_message: "Goodbye.",
    inbound_opening: "Greet the caller warmly.",
    inbound_personalization_template: "Speak with the caller.",
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
  policy: null,
};

const RESOLUTION_ORDER = [
  "Per-call profile override",
  "Per-contact assignment",
  "Per-direction default profile",
  "Built-in fallback configuration",
];

function defaultsView(over: Partial<DefaultsView> = {}): DefaultsView {
  return {
    directions: [
      { direction: "inbound", default_profile: null, ineligible: false, ineligible_reason: null },
      { direction: "outbound", default_profile: null, ineligible: false, ineligible_reason: null },
    ],
    resolution_order: RESOLUTION_ORDER,
    builtin_fallback: FALLBACK,
    ...over,
  };
}

let seq = 0;
function profile(over: Partial<ProfileSummary> = {}): ProfileSummary {
  seq += 1;
  return {
    id: `00000000-0000-0000-0000-${String(seq).padStart(12, "0")}`,
    name: `Profile ${seq}`,
    description: null,
    status: "active",
    is_default_inbound: false,
    is_default_outbound: false,
    published_version: 1,
    has_unpublished_draft: false,
    assigned_contact_count: 0,
    draft_revision: 1,
    updated_at: "2026-06-13T00:00:00Z",
    ...over,
  };
}

// Route by URL: /v1/admin/defaults -> the view, /v1/admin/profiles -> the list.
function respond(view: DefaultsView, profiles: ProfileSummary[]) {
  getMock.mockImplementation((url: string) => {
    if (url === "/v1/admin/defaults") return Promise.resolve(view);
    if (url === "/v1/admin/profiles") return Promise.resolve(profiles);
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/defaults"]}>
        <Routes>
          <Route path="/defaults" element={<DefaultsPage />} />
          {/* Edit-link target: the profile editor route. */}
          <Route path="/profiles/:id" element={<div>EDITOR</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  isAdmin = true;
  seq = 0;
  getMock.mockReset();
  postMock.mockReset();
  postMock.mockResolvedValue({});
});
afterEach(() => vi.clearAllMocks());

describe("DefaultsPage", () => {
  it("renders the per-direction current default name", async () => {
    const inbound = profile({ name: "Morning Inbound", is_default_inbound: true });
    respond(
      defaultsView({
        directions: [
          {
            direction: "inbound",
            default_profile: {
              id: inbound.id,
              name: "Morning Inbound",
              status: "active",
              published_version: 1,
              eligible: true,
            },
            ineligible: false,
            ineligible_reason: null,
          },
          {
            direction: "outbound",
            default_profile: null,
            ineligible: false,
            ineligible_reason: null,
          },
        ],
      }),
      [inbound],
    );
    renderPage();

    // The current default name renders (badge + the plain-language "Runs…" line).
    expect((await screen.findAllByText("Morning Inbound")).length).toBeGreaterThanOrEqual(1);
    // Outbound has no default — the page states what runs (the built-in fallback).
    expect(screen.getByText(/no default set/i)).toBeInTheDocument();
  });

  it("explains the resolution order in plain language", async () => {
    respond(defaultsView(), []);
    renderPage();

    await screen.findByText(/resolution order/i);
    for (const tier of RESOLUTION_ORDER) {
      expect(screen.getByText(tier)).toBeInTheDocument();
    }
  });

  it("shows the built-in fallback configuration read-only", async () => {
    respond(defaultsView(), []);
    renderPage();

    const panel = await screen.findByTestId("builtin-fallback");
    // Read-only: surfaces the fallback voice/model so an admin knows what runs last.
    expect(within(panel).getByText("gemini-3.1-flash-lite")).toBeInTheDocument();
    expect(within(panel).getByText("ink-whisper")).toBeInTheDocument();
    // No editable controls in the fallback panel.
    expect(within(panel).queryByRole("textbox")).toBeNull();
    expect(within(panel).queryByRole("combobox")).toBeNull();
  });

  it("links to edit the current default profile", async () => {
    const inbound = profile({ name: "Editable Default", is_default_inbound: true });
    respond(
      defaultsView({
        directions: [
          {
            direction: "inbound",
            default_profile: {
              id: inbound.id,
              name: "Editable Default",
              status: "active",
              published_version: 2,
              eligible: true,
            },
            ineligible: false,
            ineligible_reason: null,
          },
          {
            direction: "outbound",
            default_profile: null,
            ineligible: false,
            ineligible_reason: null,
          },
        ],
      }),
      [inbound],
    );
    const user = userEvent.setup();
    renderPage();

    const editLink = await screen.findByRole("link", { name: /edit .*default/i });
    expect(editLink).toHaveAttribute("href", `/profiles/${inbound.id}`);
    await user.click(editLink);
    expect(await screen.findByText("EDITOR")).toBeInTheDocument();
  });

  it("surfaces an ineligible-default warning and prompts for a replacement", async () => {
    const stale = profile({ name: "Stale Default", is_default_outbound: true, status: "archived" });
    const replacement = profile({ name: "Fresh Outbound" });
    respond(
      defaultsView({
        directions: [
          {
            direction: "inbound",
            default_profile: null,
            ineligible: false,
            ineligible_reason: null,
          },
          {
            direction: "outbound",
            default_profile: {
              id: stale.id,
              name: "Stale Default",
              status: "archived",
              published_version: 1,
              eligible: false,
            },
            ineligible: true,
            ineligible_reason: "archived",
          },
        ],
      }),
      [stale, replacement],
    );
    const user = userEvent.setup();
    renderPage();

    // The warning explains the default is no longer effective.
    expect(await screen.findByText(/this default is no longer effective/i)).toBeInTheDocument();
    // And explains WHY (archived).
    expect(screen.getByText(/archived/i)).toBeInTheDocument();

    // A replacement select offering eligible (active+published) profiles only.
    const select = screen.getByLabelText(/default outbound profile/i);
    // The archived stale profile must NOT be selectable as a replacement.
    expect(within(select).queryByText("Stale Default")).toBeNull();
    await user.selectOptions(select, replacement.id);
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith(`/v1/admin/profiles/${replacement.id}/set-default`, {
        direction: "outbound",
      }),
    );
  });

  it("hides default-change controls for viewers", async () => {
    isAdmin = false;
    respond(defaultsView(), [profile()]);
    renderPage();

    await screen.findByText(/resolution order/i);
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(screen.getByText(/viewer/i)).toBeInTheDocument();
  });

  it("uses 'contact' wording, not 'elder'", async () => {
    respond(defaultsView(), []);
    const { container } = renderPage();

    await screen.findByText(/resolution order/i);
    expect(container.textContent?.toLowerCase()).not.toContain("elder");
    expect(container.textContent?.toLowerCase()).toContain("contact");
  });
});
