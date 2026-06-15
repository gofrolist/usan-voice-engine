// apps/admin-ui/src/test/PromptEditor.test.tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactElement } from "react";
import { describe, expect, it } from "vitest";
import { PromptEditor } from "../features/editor/sections/PromptEditor";
import type { VariableSpec } from "../config/variableCatalog";

// PromptEditor uses useCreateCustomVariable (inline declaration), which needs a
// QueryClient in scope — wrap every render.
function renderWithClient(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const VARS: VariableSpec[] = [
  {
    name: "first_name",
    tier: "builtin",
    description: "The contact's first name.",
    default: "there",
    example: "Margaret",
    phi: false,
  },
  {
    name: "today_meds",
    tier: "builtin",
    description: "Medications scheduled today.",
    default: "",
    example: "Lisinopril",
    phi: true,
  },
];

const PHI_NAMES: ReadonlySet<string> = new Set(["today_meds"]);

function Harness() {
  const [value, setValue] = useState("Hello ");
  return (
    <>
      <PromptEditor
        id="prompts.greeting"
        value={value}
        onChange={setValue}
        variables={VARS}
        knownNames={new Set(["first_name"])}
      />
      <output data-testid="val">{value}</output>
    </>
  );
}

describe("PromptEditor PHI warnings", () => {
  it("(a) shows PHI warning for a PHI var in a sensitive field", () => {
    renderWithClient(
      <PromptEditor
        id="prompts.voicemail_message"
        fieldKey="voicemail_message"
        value="Your meds today: {{today_meds}}"
        onChange={() => {}}
        variables={VARS}
        knownNames={new Set(["first_name", "today_meds"])}
        phiNames={PHI_NAMES}
      />,
    );
    expect(screen.getByText(/today_meds.*health information/i)).toBeInTheDocument();
  });

  it("(b) does NOT show PHI warning for a non-PHI var in a sensitive field", () => {
    renderWithClient(
      <PromptEditor
        id="prompts.voicemail_message"
        fieldKey="voicemail_message"
        value="Hello {{first_name}}"
        onChange={() => {}}
        variables={VARS}
        knownNames={new Set(["first_name", "today_meds"])}
        phiNames={PHI_NAMES}
      />,
    );
    expect(screen.queryByText(/health information/i)).not.toBeInTheDocument();
  });

  it("(c) does NOT show PHI warning for a PHI var in a non-sensitive field", () => {
    renderWithClient(
      <PromptEditor
        id="prompts.system_prompt"
        fieldKey="system_prompt"
        value="Meds: {{today_meds}}"
        onChange={() => {}}
        variables={VARS}
        knownNames={new Set(["first_name", "today_meds"])}
        phiNames={PHI_NAMES}
      />,
    );
    expect(screen.queryByText(/health information/i)).not.toBeInTheDocument();
  });

  it("(d) PHI warning is non-blocking — component renders without error", () => {
    // A warning must never throw or prevent rendering (saving remains possible).
    const { container } = renderWithClient(
      <PromptEditor
        id="prompts.greeting"
        fieldKey="greeting"
        value="{{today_meds}}"
        onChange={() => {}}
        variables={VARS}
        knownNames={new Set(["first_name", "today_meds"])}
        phiNames={PHI_NAMES}
      />,
    );
    expect(container).toBeTruthy();
    expect(screen.getByText(/health information/i)).toBeInTheDocument();
    // No Zod error text — purely informational notice.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("PromptEditor variable palette", () => {
  it("renders the insert-variable button alongside the editor", () => {
    renderWithClient(<Harness />);
    expect(screen.getByRole("button", { name: /insert variable/i })).toBeInTheDocument();
  });

  it("inserts {{first_name}} into the value when picked from the palette", async () => {
    const user = userEvent.setup();
    renderWithClient(<Harness />);

    await user.click(screen.getByRole("button", { name: /insert variable/i }));
    await user.click(screen.getByRole("button", { name: /first_name/ }));

    // Monaco is not mounted under jsdom, so the insert appends to the current value.
    expect(screen.getByTestId("val").textContent).toBe("Hello {{first_name}}");
  });

  it("shows a non-blocking unknown-variable notice for unknown tokens", () => {
    function UnknownHarness() {
      return (
        <PromptEditor
          id="prompts.greeting"
          value="Hi {{first_name}} and {{made_up}}"
          onChange={() => {}}
          variables={VARS}
          knownNames={new Set(["first_name"])}
        />
      );
    }
    renderWithClient(<UnknownHarness />);
    expect(screen.getByText(/unknown variable: made_up/i)).toBeInTheDocument();
  });
});
