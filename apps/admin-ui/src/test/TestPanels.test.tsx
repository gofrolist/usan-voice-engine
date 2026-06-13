// apps/admin-ui/src/test/TestPanels.test.tsx
// US5: TestLLMPanel (chat over the test/llm endpoint) and TestAudioPanel
// (livekit-client Room.connect + mic publish + subscribed-audio playback). The api
// helpers and livekit-client are mocked so the panels are exercised without a server.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const testLlmMock = vi.fn();
const testAudioMock = vi.fn();
vi.mock("../lib/api", () => ({
  testProfileLlm: (id: string, body: unknown) => testLlmMock(id, body),
  testProfileAudio: (id: string, body: unknown) => testAudioMock(id, body),
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      public detail: string,
    ) {
      super(detail);
    }
  },
}));

const toastMock = vi.fn();
vi.mock("../components/ui/toast", () => ({ pushToast: (m: string) => toastMock(m) }));

// A controllable mock Room. connect/disconnect/setMicrophoneEnabled are spies; the
// TrackSubscribed handler is captured so the test can assert audio playback wiring.
const connectMock = vi.fn().mockResolvedValue(undefined);
const disconnectMock = vi.fn().mockResolvedValue(undefined);
const setMicMock = vi.fn().mockResolvedValue(undefined);
const attachMock = vi.fn();
const handlers: Record<string, (...a: unknown[]) => void> = {};
vi.mock("livekit-client", () => ({
  Room: class {
    localParticipant = { setMicrophoneEnabled: setMicMock };
    on(event: string, cb: (...a: unknown[]) => void) {
      handlers[event] = cb;
      return this;
    }
    connect = connectMock;
    disconnect = disconnectMock;
  },
  RoomEvent: { TrackSubscribed: "trackSubscribed", Disconnected: "disconnected" },
  Track: { Kind: { Audio: "audio", Video: "video" } },
}));

import { TestLLMPanel } from "../features/editor/TestLLMPanel";
import { TestAudioPanel } from "../features/editor/TestAudioPanel";
import type { AgentConfig } from "../types/api";

const FAKE_CONFIG = { prompts: {} } as unknown as AgentConfig;

beforeEach(() => {
  vi.clearAllMocks();
  for (const k of Object.keys(handlers)) delete handlers[k];
});
afterEach(() => {
  vi.restoreAllMocks();
});

describe("TestLLMPanel", () => {
  it("sends a message, posts sample_vars + config, and renders the assistant reply + tool calls", async () => {
    testLlmMock.mockResolvedValue({
      assistant: "Hello there!",
      tool_calls: [{ name: "get_today_meds", args: {} }],
    });
    const user = userEvent.setup();
    render(<TestLLMPanel profileId="p1" getConfig={() => FAKE_CONFIG} />);

    await user.type(screen.getByLabelText("Sample variables"), "first_name=Alex");
    await user.type(screen.getByLabelText("Your message"), "I am okay");
    await user.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(screen.getByText(/Hello there!/)).toBeInTheDocument());
    expect(testLlmMock).toHaveBeenCalledTimes(1);
    const call = testLlmMock.mock.calls[0] ?? [];
    const id = call[0];
    const body = call[1];
    expect(id).toBe("p1");
    expect(body.messages).toEqual([{ role: "user", content: "I am okay" }]);
    expect(body.sample_vars).toEqual({ first_name: "Alex" });
    expect(body.config).toBe(FAKE_CONFIG);
    // The model's tool call is echoed for visibility.
    expect(screen.getByText(/get_today_meds/)).toBeInTheDocument();
  });

  it("surfaces an error via toast and rolls back the failed user turn", async () => {
    const { ApiError } = await import("../lib/api");
    testLlmMock.mockRejectedValue(new ApiError(503, "text test unavailable"));
    const user = userEvent.setup();
    render(<TestLLMPanel profileId="p1" getConfig={() => null} />);

    await user.type(screen.getByLabelText("Your message"), "hi");
    await user.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(toastMock).toHaveBeenCalledWith("text test unavailable"));
    // The optimistic user turn is rolled back so the transcript stays consistent.
    expect(screen.getByText(/No messages yet/)).toBeInTheDocument();
  });
});

describe("TestAudioPanel", () => {
  it("mints a token via the endpoint, connects, publishes the mic, and plays agent audio", async () => {
    testAudioMock.mockResolvedValue({
      url: "wss://lk.example/",
      token: "jwt-token",
      room: "usan-test-abc",
    });
    const user = userEvent.setup();
    render(<TestAudioPanel profileId="p2" getConfig={() => FAKE_CONFIG} />);

    await user.click(screen.getByRole("button", { name: "Start test call" }));

    await waitFor(() => expect(connectMock).toHaveBeenCalledWith("wss://lk.example/", "jwt-token"));
    expect(testAudioMock).toHaveBeenCalledWith("p2", {
      sample_vars: {},
      config: FAKE_CONFIG,
    });
    expect(setMicMock).toHaveBeenCalledWith(true);
    await waitFor(() =>
      expect(screen.getByText(/Connected — speak to the agent/)).toBeInTheDocument(),
    );

    // A subscribed agent audio track is attached to the <audio> element.
    const fakeTrack = { kind: "audio", attach: attachMock };
    const onSubscribed = handlers["trackSubscribed"];
    expect(onSubscribed).toBeDefined();
    onSubscribed?.(fakeTrack, {}, {});
    expect(attachMock).toHaveBeenCalledTimes(1);

    // Ending the call disconnects the room.
    await user.click(screen.getByRole("button", { name: "End call" }));
    await waitFor(() => expect(disconnectMock).toHaveBeenCalled());
  });

  it("toasts and stays idle when minting the token fails", async () => {
    const { ApiError } = await import("../lib/api");
    testAudioMock.mockRejectedValue(new ApiError(403, "admin role required"));
    const user = userEvent.setup();
    render(<TestAudioPanel profileId="p2" getConfig={() => null} />);

    await user.click(screen.getByRole("button", { name: "Start test call" }));

    await waitFor(() => expect(toastMock).toHaveBeenCalledWith("admin role required"));
    expect(connectMock).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Start test call" })).toBeInTheDocument();
  });
});
