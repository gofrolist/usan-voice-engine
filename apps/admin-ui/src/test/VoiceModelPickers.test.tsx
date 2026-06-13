// apps/admin-ui/src/test/VoiceModelPickers.test.tsx
//
// US2 / FR-009–FR-014. Covers:
// - useVoiceCatalog / useModelCatalog hooks (fetch + 5-min staleTime, mirror toolCatalog).
// - VoiceSection: searchable curated picker + per-voice play control (hits the sample
//   endpoint) + deprecation handling for a withdrawn selected voice.
// - LLMSection / STTSection: curated kind-filtered <select>s + a deprecation marker on a
//   withdrawn selected value.
// - The Zod schema stays permissive (model/voice are plain str), so a withdrawn value
//   still parses (forward-compat invariant).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useForm, type UseFormReturn } from "react-hook-form";
import type { ReactNode } from "react";
import { useVoiceCatalog, type VoiceSpec } from "../config/voiceCatalog";
import { useModelCatalog, type ModelSpec } from "../config/modelCatalog";
import { VoiceSection } from "../features/editor/sections/VoiceSection";
import { LLMSection } from "../features/editor/sections/LLMSection";
import { STTSection } from "../features/editor/sections/STTSection";
import {
  agentConfigSchema,
  llmSchema,
  voiceSchema,
  type AgentConfigForm,
} from "../config/agentConfigSchema";

const getMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: { get: (u: string) => getMock(u) },
}));

const VOICES: VoiceSpec[] = [
  {
    cartesia_voice_id: "voice-warm-man",
    name: "Barbershop Man",
    language: "en",
    gender: "masculine",
    description: "Warm, friendly American male.",
    tts_model_hint: "sonic-2",
    deprecated: false,
  },
  {
    cartesia_voice_id: "voice-calm-lady",
    name: "Calm Lady",
    language: "en",
    gender: "feminine",
    description: "Soothing American female.",
    tts_model_hint: "sonic-2",
    deprecated: false,
  },
  {
    cartesia_voice_id: "voice-old",
    name: "Retired Voice",
    language: "en",
    gender: null,
    description: "No longer offered.",
    tts_model_hint: null,
    deprecated: true,
  },
];

const MODELS: ModelSpec[] = [
  {
    id: "gemini-3.1-flash-lite",
    label: "Gemini 3.1 Flash Lite",
    description: "Fast Vertex model.",
    kind: "llm",
    provider: "vertex",
    deprecated: false,
    default: true,
  },
  {
    id: "gemini-2.5-pro",
    label: "Gemini 2.5 Pro",
    description: "Most capable Vertex model.",
    kind: "llm",
    provider: "vertex",
    deprecated: false,
    default: false,
  },
  {
    id: "gemini-1.0-legacy",
    label: "Gemini 1.0 (legacy)",
    description: "Withdrawn.",
    kind: "llm",
    provider: "vertex",
    deprecated: true,
    default: false,
  },
  {
    id: "ink-whisper",
    label: "Ink Whisper",
    description: "Cartesia STT.",
    kind: "stt",
    provider: "cartesia",
    deprecated: false,
    default: true,
  },
];

function mockCatalogs(): void {
  getMock.mockImplementation((url: string) => {
    if (url === "/v1/admin/voice-catalog") return Promise.resolve({ voices: VOICES });
    if (url === "/v1/admin/model-catalog") return Promise.resolve({ models: MODELS });
    return Promise.reject(new Error(`unexpected url ${url}`));
  });
}

function wrap(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function hookWrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

let formRef: UseFormReturn<AgentConfigForm> | null = null;

function VoiceHarness({ voiceId }: { voiceId: string | null }) {
  const form = useForm<AgentConfigForm>({
    defaultValues: {
      voice: { cartesia_voice_id: voiceId, tts_model: null, speed: null, language: null },
    } as AgentConfigForm,
  });
  formRef = form;
  return <VoiceSection form={form} />;
}

function LlmHarness({ model }: { model: string }) {
  const form = useForm<AgentConfigForm>({
    defaultValues: { llm: { model, temperature: null } } as AgentConfigForm,
  });
  formRef = form;
  return <LLMSection form={form} />;
}

function SttHarness({ model }: { model: string }) {
  const form = useForm<AgentConfigForm>({
    defaultValues: { stt: { model, language: null } } as AgentConfigForm,
  });
  formRef = form;
  return <STTSection form={form} />;
}

beforeEach(() => {
  mockCatalogs();
});

afterEach(() => {
  vi.restoreAllMocks();
  getMock.mockReset();
  formRef = null;
});

describe("useVoiceCatalog / useModelCatalog", () => {
  it("useVoiceCatalog fetches the voice catalog", async () => {
    const { result } = renderHook(() => useVoiceCatalog(), { wrapper: hookWrapper() });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(getMock).toHaveBeenCalledWith("/v1/admin/voice-catalog");
    expect(result.current.data).toEqual(VOICES);
  });

  it("useModelCatalog fetches the model catalog", async () => {
    const { result } = renderHook(() => useModelCatalog(), { wrapper: hookWrapper() });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(getMock).toHaveBeenCalledWith("/v1/admin/model-catalog");
    expect(result.current.data).toEqual(MODELS);
  });
});

describe("VoiceSection picker", () => {
  it("lists active voices and hides deprecated ones until searched", async () => {
    render(wrap(<VoiceHarness voiceId={null} />));
    // Active voices render once the catalog loads.
    await screen.findByText("Barbershop Man");
    expect(screen.getByText("Calm Lady")).toBeInTheDocument();
    // The deprecated voice is not offered as a new choice.
    expect(screen.queryByText("Retired Voice")).not.toBeInTheDocument();
  });

  it("filters the voice list by a search term", async () => {
    const user = userEvent.setup();
    render(wrap(<VoiceHarness voiceId={null} />));
    await screen.findByText("Barbershop Man");
    const search = screen.getByPlaceholderText(/search voices/i);
    await user.type(search, "calm");
    expect(screen.getByText("Calm Lady")).toBeInTheDocument();
    expect(screen.queryByText("Barbershop Man")).not.toBeInTheDocument();
  });

  it("selecting a voice writes its cartesia_voice_id into the form", async () => {
    const user = userEvent.setup();
    render(wrap(<VoiceHarness voiceId={null} />));
    await screen.findByText("Calm Lady");
    await user.click(screen.getByText("Calm Lady"));
    await waitFor(() =>
      expect(formRef!.getValues("voice.cartesia_voice_id")).toBe("voice-calm-lady"),
    );
  });

  it("a per-voice play button hits the sample endpoint", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      blob: async () => new Blob(["x"], { type: "audio/mpeg" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn(() => "blob:fake"),
      revokeObjectURL: vi.fn(),
    });
    const playMock = vi.fn().mockResolvedValue(undefined);
    vi.spyOn(window.HTMLMediaElement.prototype, "play").mockImplementation(playMock);

    render(wrap(<VoiceHarness voiceId={null} />));
    await screen.findByText("Barbershop Man");
    const playButtons = screen.getAllByRole("button", { name: /play sample/i });
    await user.click(playButtons[0]!);

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/admin/voice-catalog/voice-warm-man/sample",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("shows a deprecation marker when the selected voice is withdrawn", async () => {
    render(wrap(<VoiceHarness voiceId="voice-old" />));
    // The selected (withdrawn) voice is still shown with a deprecation marker so the
    // operator understands a published config references it (FR-010 deprecation UX).
    expect(await screen.findByText(/deprecated|no longer offered/i)).toBeInTheDocument();
  });
});

describe("LLMSection / STTSection curated selects", () => {
  it("LLMSection renders a select of llm-kind models only", async () => {
    render(wrap(<LlmHarness model="gemini-3.1-flash-lite" />));
    const select = (await screen.findByLabelText(/LLM model/i)) as HTMLSelectElement;
    // Wait for the catalog fetch to populate the options.
    await screen.findByRole("option", { name: /Gemini 2.5 Pro/i });
    const optionValues = Array.from(select.options).map((o) => o.value);
    // llm models present; the stt model (ink-whisper) is filtered out.
    expect(optionValues).toContain("gemini-3.1-flash-lite");
    expect(optionValues).toContain("gemini-2.5-pro");
    expect(optionValues).not.toContain("ink-whisper");
    // Deprecated llm model is hidden from the active options (it is not selected here).
    expect(optionValues).not.toContain("gemini-1.0-legacy");
  });

  it("STTSection renders a select of stt-kind models only", async () => {
    render(wrap(<SttHarness model="ink-whisper" />));
    const select = (await screen.findByLabelText(/STT model/i)) as HTMLSelectElement;
    await screen.findByRole("option", { name: /Ink Whisper/i });
    const optionValues = Array.from(select.options).map((o) => o.value);
    expect(optionValues).toContain("ink-whisper");
    expect(optionValues).not.toContain("gemini-2.5-pro");
  });

  it("changing the LLM select updates the form value", async () => {
    const user = userEvent.setup();
    render(wrap(<LlmHarness model="gemini-3.1-flash-lite" />));
    const select = (await screen.findByLabelText(/LLM model/i)) as HTMLSelectElement;
    await screen.findByRole("option", { name: /Gemini 2.5 Pro/i });
    await user.selectOptions(select, "gemini-2.5-pro");
    await waitFor(() => expect(formRef!.getValues("llm.model")).toBe("gemini-2.5-pro"));
  });

  it("a withdrawn selected model still appears (with a deprecation marker)", async () => {
    render(wrap(<LlmHarness model="gemini-1.0-legacy" />));
    const select = (await screen.findByLabelText(/LLM model/i)) as HTMLSelectElement;
    // The currently-selected (deprecated) value must remain selectable so a published
    // config that uses it still loads without silently switching models.
    await screen.findByRole("option", { name: /Gemini 1.0/i });
    expect(Array.from(select.options).map((o) => o.value)).toContain("gemini-1.0-legacy");
    expect(select.value).toBe("gemini-1.0-legacy");
    // The amber marker sentence (distinct from the "(deprecated)" option suffix).
    expect(screen.getByText(/This model is deprecated/i)).toBeInTheDocument();
  });
});

describe("Zod stays permissive (forward-compat)", () => {
  it("voiceSchema accepts a withdrawn cartesia_voice_id", () => {
    const parsed = voiceSchema.safeParse({
      cartesia_voice_id: "voice-old",
      tts_model: null,
      speed: null,
      language: null,
    });
    expect(parsed.success).toBe(true);
  });

  it("llmSchema accepts a withdrawn model id", () => {
    const parsed = llmSchema.safeParse({ model: "gemini-1.0-legacy", temperature: null });
    expect(parsed.success).toBe(true);
  });

  it("agentConfigSchema is unchanged by the curated UI (voice/model still str)", () => {
    // A draft with an arbitrary (non-catalog) voice/model still parses client-side; the
    // server 422 is the authoritative gate (R2). This proves we did NOT add a Zod enum.
    const base = {
      voice: { cartesia_voice_id: "anything", tts_model: null, speed: null, language: null },
      llm: { model: "anything", temperature: null },
      stt: { model: "anything", language: null },
    };
    expect(voiceSchema.safeParse(base.voice).success).toBe(true);
    expect(llmSchema.safeParse(base.llm).success).toBe(true);
    expect(agentConfigSchema.shape.voice).toBeDefined();
  });
});
