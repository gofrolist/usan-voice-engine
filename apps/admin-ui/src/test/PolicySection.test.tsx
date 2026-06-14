import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useForm, type Resolver } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { describe, expect, it } from "vitest";
import { PolicySection } from "../features/editor/sections/PolicySection";
import { agentConfigSchema, type AgentConfigForm } from "../config/agentConfigSchema";

// A fully valid config so whole-schema validation (zodResolver validates everything
// on every run) can reach isValid=true — only the policy slice varies per test.
function validConfig(policy: AgentConfigForm["policy"]): AgentConfigForm {
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
    policy,
  };
}

function Harness({ policy = null }: { policy?: AgentConfigForm["policy"] }) {
  const form = useForm<AgentConfigForm>({
    // zod 4 + resolvers v5 split input/output types (see ProfileEditorPage); the
    // harness works on the output shape, so assert the resolver. Types-only.
    resolver: zodResolver(agentConfigSchema) as Resolver<AgentConfigForm>,
    mode: "onBlur",
    defaultValues: validConfig(policy),
  });
  return (
    <>
      <PolicySection form={form} />
      <output data-testid="valid">{String(form.formState.isValid)}</output>
    </>
  );
}

describe("PolicySection", () => {
  it("renders two time inputs, multiplier, and four attempt inputs with effective-default placeholders", () => {
    render(<Harness />);

    // Unset state (spec §6.2): effective defaults appear as PLACEHOLDERS, never as
    // values — the UI must not write statutory/builtin defaults into the config.
    const start = screen.getByLabelText("Quiet hours start (local)");
    const end = screen.getByLabelText("Quiet hours end (local)");
    expect(start).toHaveAttribute("type", "time");
    expect(end).toHaveAttribute("type", "time");
    expect(start).toHaveAttribute("placeholder", "09:00");
    expect(end).toHaveAttribute("placeholder", "21:00");
    expect(start).toHaveValue("");
    expect(end).toHaveValue("");

    const multiplier = screen.getByLabelText("Retry delay multiplier");
    expect(multiplier).toHaveAttribute("placeholder", "1.0");
    expect((multiplier as HTMLInputElement).value).toBe("");

    // Builtin per-status attempt defaults as placeholders (no_answer 2; others 1).
    const attempts: Array<[string, string]> = [
      ["No answer", "2"],
      ["Voicemail left", "1"],
      ["Busy", "1"],
      ["Failed", "1"],
    ];
    for (const [label, placeholder] of attempts) {
      const input = screen.getByLabelText(label);
      expect(input).toHaveAttribute("placeholder", placeholder);
      expect((input as HTMLInputElement).value).toBe("");
    }
  });

  it("widening start shows validation error", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const start = screen.getByLabelText("Quiet hours start (local)");

    await user.type(start, "08:00");
    await user.tab();

    expect(
      await screen.findByText("Quiet hours start must be at or after the statutory 09:00"),
    ).toBeInTheDocument();
  });

  it("clearing a time input keeps the form valid", async () => {
    const user = userEvent.setup();
    render(
      <Harness
        policy={{
          quiet_hours_start_local: "10:00",
          quiet_hours_end_local: null,
          retry_delay_multiplier: null,
          retry_max_attempts: null,
        }}
      />,
    );
    const start = screen.getByLabelText("Quiet hours start (local)");
    expect(start).toHaveValue("10:00");

    // A cleared <input type="time"> yields "" (not null); the zod ""→null transform
    // must make the pristine-looking form validate end-to-end (spec §6.2).
    await user.clear(start);
    await user.tab();

    await waitFor(() => expect(screen.getByTestId("valid").textContent).toBe("true"));
    // The regex error a raw "" would produce must never render (transform applied).
    expect(screen.queryByText(/must be "HH:MM"/)).not.toBeInTheDocument();
  });
});
