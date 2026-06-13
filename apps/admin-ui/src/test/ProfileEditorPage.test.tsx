import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
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

// Observe toasts (the QueuesPage.test.tsx pattern) so the 422-routing tests can
// assert exactly ONE friendly toast and never the raw JSON detail blob.
const pushToastMock = vi.fn();
vi.mock("../components/ui/toast", () => ({
  pushToast: (message: string, tone?: string) => pushToastMock(message, tone),
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
    draft_revision: 1,
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

// Server 422 routing (C9). The custom-PHI SMS violation is the client-pass/server-422
// case: the body references a CUSTOM variable (the static zod superRefine only blocks
// the 5 builtins), so the server is the authoritative gate and its fabricated loc —
// ["body","config","tools","sms","templates",<i>,"body"] — must land on the body input.
const PHI_VIOLATION_MSG =
  "SMS template 'followup' body references protected health information " +
  "({{diagnosis}}); SMS bodies may use non-PHI variables only";
const PHI_DETAIL = JSON.stringify([
  {
    loc: ["body", "config", "tools", "sms", "templates", 0, "body"],
    msg: PHI_VIOLATION_MSG,
    type: "value_error.custom_phi_sms",
  },
]);

// Profile whose draft has one SMS template referencing a custom variable. The form
// schema types tools.sms; the server AgentConfig type does not (yet), so cast.
function profileWithSmsTemplate(): ProfileDetail {
  const p = profile();
  return {
    ...p,
    draft_config: {
      ...p.draft_config,
      tools: {
        enabled: ["log_wellness", "send_sms", "end_call"],
        sms: { templates: [{ key: "followup", label: "Follow up", body: "Hi {{diagnosis}}" }] },
      },
    } as unknown as AgentConfig,
  };
}

function routeGetWithSms(url: string): Promise<unknown> {
  if (url === "/v1/auth/me") {
    return Promise.resolve({ email: "me@example.com", role: "admin" } satisfies Me);
  }
  if (url === "/v1/admin/profiles/p1") return Promise.resolve(profileWithSmsTemplate());
  if (url === "/v1/admin/profiles/p1/versions/1") {
    return Promise.resolve({
      version: 1,
      note: null,
      published_by: "ops@example.com",
      published_at: "2026-06-01T00:00:00Z",
      config: baseConfig(),
    } satisfies VersionDetail);
  }
  // The Tools section fetches both catalogs; empty responses keep the templates
  // editor rendering without any catalog-driven notices.
  if (url === "/v1/admin/tool-catalog") return Promise.resolve({ tools: [] });
  if (url === "/v1/admin/variable-catalog") return Promise.resolve({ variables: [] });
  return Promise.reject(new Error(`unexpected GET ${url}`));
}

describe("ProfileEditorPage server 422 routing (mapServerErrors)", () => {
  beforeEach(() => {
    getMock.mockReset();
    putMock.mockReset();
    postMock.mockReset();
    pushToastMock.mockReset();
    getMock.mockImplementation(routeGetWithSms);
  });
  afterEach(() => vi.clearAllMocks());

  it("save 422 with a templates loc lands the error on the body field", async () => {
    const { ApiError } = await import("../lib/api");
    putMock.mockRejectedValue(new ApiError(422, PHI_DETAIL));
    renderPage();
    const user = userEvent.setup();
    await screen.findByRole("button", { name: "Publish" });
    await user.click(screen.getByRole("tab", { name: "Tools" }));
    await screen.findByText("SMS templates");

    await user.click(screen.getByRole("button", { name: "Save draft" }));

    await waitFor(() => expect(putMock).toHaveBeenCalled());
    // The error must render on tools.sms.templates.0.body (the body input), not be
    // eaten by a value filter that drops the trailing field literally named "body".
    expect(await screen.findByText(PHI_VIOLATION_MSG)).toBeInTheDocument();
  });

  it("publish 422 routes through mapServerErrors with exactly one friendly toast", async () => {
    const { ApiError } = await import("../lib/api");
    postMock.mockRejectedValue(new ApiError(422, PHI_DETAIL));
    renderPage();
    const user = userEvent.setup();
    await screen.findByRole("button", { name: "Publish" });
    await user.click(screen.getByRole("tab", { name: "Tools" }));
    await screen.findByText("SMS templates");

    // The form is pristine, so Publish opens the dialog without a save round-trip.
    await user.click(screen.getByRole("button", { name: "Publish" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /^Publish$/ }));

    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/v1/admin/profiles/p1/publish", { note: null }),
    );
    // The violation lands on the body field like a save 422 would...
    expect(await screen.findByText(PHI_VIOLATION_MSG)).toBeInTheDocument();
    // ...and EXACTLY one friendly toast fires — never the raw JSON detail. (react-query
    // v5 runs a per-mutate onError in addition to the hook-level one: a second handler
    // would double-toast, so the handler MOVES from usePublish into the dialog confirm.)
    await waitFor(() => expect(pushToastMock).toHaveBeenCalledTimes(1));
    expect(pushToastMock.mock.calls[0]?.[0]).toBe(
      "Some fields were rejected by the server — see the highlighted errors.",
    );
  });
});

// Profile whose draft carries a set quiet-hours start, so clearing the time input
// is what dirties the form — the exact publish-while-dirty payload under test.
function profileWithPolicy(): ProfileDetail {
  const p = profile();
  return {
    ...p,
    draft_config: {
      ...p.draft_config,
      policy: {
        quiet_hours_start_local: "10:00",
        quiet_hours_end_local: null,
        retry_delay_multiplier: null,
        retry_max_attempts: null,
      },
    },
  };
}

function routeGetWithPolicy(url: string): Promise<unknown> {
  if (url === "/v1/admin/profiles/p1") return Promise.resolve(profileWithPolicy());
  return routeGet(url);
}

describe("ProfileEditorPage publish-while-dirty payload normalization", () => {
  beforeEach(() => {
    getMock.mockReset();
    putMock.mockReset();
    postMock.mockReset();
    pushToastMock.mockReset();
    getMock.mockImplementation(routeGetWithPolicy);
  });
  afterEach(() => vi.clearAllMocks());

  it("a cleared quiet-hours time input persists as null, never ''", async () => {
    // form.getValues() returns RAW input values: a cleared <input type="time">
    // is "" and the zod ""→null transform only runs inside the resolver, so the
    // dirty-save before publish must normalize through agentConfigSchema.parse
    // — the server's HH:MM regex 422s on "".
    putMock.mockResolvedValue(profileWithPolicy());
    renderPage();
    const user = userEvent.setup();
    await screen.findByRole("button", { name: "Publish" });

    await user.click(screen.getByRole("tab", { name: "Policy" }));
    const start = await screen.findByLabelText("Quiet hours start (local)");
    expect(start).toHaveValue("10:00");
    await user.clear(start); // "" — and the form is now dirty
    await user.tab();

    await user.click(screen.getByRole("button", { name: "Publish" }));

    await waitFor(() => expect(putMock).toHaveBeenCalled());
    const body = putMock.mock.calls[0]?.[1] as { config: AgentConfig };
    expect(body.config.policy?.quiet_hours_start_local).toBeNull();
  });
});

describe("ProfileEditorPage policy section (D11)", () => {
  beforeEach(() => {
    getMock.mockReset();
    putMock.mockReset();
    postMock.mockReset();
    pushToastMock.mockReset();
    getMock.mockImplementation(routeGet);
  });
  afterEach(() => vi.clearAllMocks());

  it("Policy section renders in order (last rail tab, after the existing sections)", async () => {
    renderPage();
    const user = userEvent.setup();
    await screen.findByRole("button", { name: "Publish" });

    const tabs = screen.getAllByRole("tab");
    expect(tabs).toHaveLength(9);
    expect(tabs[8]).toHaveAccessibleName("Policy");

    await user.click(screen.getByRole("tab", { name: "Policy" }));
    expect(screen.getByRole("heading", { name: "Policy" })).toBeInTheDocument();
  });

  it("older draft without policy loads without errors", async () => {
    // baseConfig() deliberately lacks the policy key (an older draft); form.reset
    // must accept it and the untouched form must still pass the zod resolver on save.
    putMock.mockResolvedValue(profile());
    renderPage();
    const user = userEvent.setup();
    await screen.findByRole("button", { name: "Publish" });

    await user.click(screen.getByRole("tab", { name: "Policy" }));
    const start = await screen.findByLabelText("Quiet hours start (local)");
    expect(start).toHaveValue(""); // placeholders only — never values

    await user.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() =>
      expect(putMock).toHaveBeenCalledWith(
        "/v1/admin/profiles/p1/draft",
        expect.objectContaining({ config: expect.anything() }),
      ),
    );
    expect(pushToastMock).toHaveBeenCalledWith("Draft saved.", "info");
  });
});
