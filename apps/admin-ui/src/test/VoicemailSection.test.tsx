import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useForm } from "react-hook-form";
import { describe, expect, it } from "vitest";
import { VoicemailSection } from "../features/editor/sections/VoicemailSection";
import type { AgentConfigForm } from "../config/agentConfigSchema";

function Harness({ initial }: { initial: string[] }) {
  const form = useForm<AgentConfigForm>({
    defaultValues: { voicemail_detection: { window_s: 3, trigger_phrases: initial } },
  });
  const phrases = form.watch("voicemail_detection.trigger_phrases");
  return (
    <>
      <VoicemailSection form={form} />
      <output data-testid="phrases">{JSON.stringify(phrases)}</output>
    </>
  );
}

describe("VoicemailSection trigger_phrases", () => {
  it("lets the operator enter multiple phrases (newline survives)", async () => {
    const user = userEvent.setup();
    render(<Harness initial={[]} />);
    const ta = screen.getByRole("textbox") as HTMLTextAreaElement;

    await user.type(ta, "foo{enter}bar");

    expect(ta.value).toBe("foo\nbar");
    expect(screen.getByTestId("phrases").textContent).toBe(JSON.stringify(["foo", "bar"]));
  });

  it("starts from the existing phrases and can append a second", async () => {
    const user = userEvent.setup();
    render(<Harness initial={["alpha"]} />);
    const ta = screen.getByRole("textbox") as HTMLTextAreaElement;
    expect(ta.value).toBe("alpha");

    await user.type(ta, "{enter}beta");

    expect(ta.value).toBe("alpha\nbeta");
    expect(screen.getByTestId("phrases").textContent).toBe(JSON.stringify(["alpha", "beta"]));
  });
});
