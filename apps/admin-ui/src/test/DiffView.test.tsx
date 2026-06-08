import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { DiffView, diffConfigs } from "../components/DiffView";
import type { AgentConfig } from "../types/api";

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

describe("DiffView", () => {
  it("computes exactly two rows for two changed fields", () => {
    const oldCfg = baseConfig();
    const newCfg = baseConfig();
    newCfg.prompts.greeting = "Good morning";
    newCfg.timing.max_call_duration_s = 3600;

    const rows = diffConfigs(oldCfg, newCfg);
    expect(rows).toHaveLength(2);

    const byPath = Object.fromEntries(rows.map((r) => [r.path, r]));
    expect(byPath["prompts.greeting"]).toMatchObject({
      kind: "changed",
      oldValue: "Hello there",
      newValue: "Good morning",
    });
    expect(byPath["timing.max_call_duration_s"]).toMatchObject({
      kind: "changed",
      oldValue: "1800",
      newValue: "3600",
    });
  });

  it("renders one table row per changed field", () => {
    const oldCfg = baseConfig();
    const newCfg = baseConfig();
    newCfg.prompts.greeting = "Good morning";
    newCfg.timing.max_call_duration_s = 3600;

    render(<DiffView oldConfig={oldCfg} newConfig={newCfg} />);
    const rows = screen.getAllByTestId("diff-row");
    expect(rows).toHaveLength(2);
    expect(screen.getByText("Hello there")).toBeInTheDocument();
    expect(screen.getByText("Good morning")).toBeInTheDocument();
    expect(screen.getByText("1800")).toBeInTheDocument();
    expect(screen.getByText("3600")).toBeInTheDocument();
  });
});
