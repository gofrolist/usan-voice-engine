// apps/admin-ui/src/test/PromptEditor.test.tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { PromptEditor } from "../features/editor/sections/PromptEditor";
import type { VariableSpec } from "../config/variableCatalog";

const VARS: VariableSpec[] = [
  {
    name: "first_name",
    tier: "builtin",
    description: "The elder's first name.",
    default: "there",
    example: "Margaret",
  },
];

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

describe("PromptEditor variable palette", () => {
  it("renders the insert-variable button alongside the editor", () => {
    render(<Harness />);
    expect(screen.getByRole("button", { name: /insert variable/i })).toBeInTheDocument();
  });

  it("inserts {{first_name}} into the value when picked from the palette", async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(screen.getByRole("button", { name: /insert variable/i }));
    await user.click(screen.getByRole("button", { name: /first_name/ }));

    // Monaco is not mounted under jsdom, so the insert appends to the current value.
    expect(screen.getByTestId("val").textContent).toBe("Hello {{first_name}}");
  });

  it("shows a non-blocking unknown-variable notice for unknown tokens", () => {
    const user = userEvent.setup();
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
    void user;
    render(<UnknownHarness />);
    expect(screen.getByText(/unknown variable: made_up/i)).toBeInTheDocument();
  });
});
