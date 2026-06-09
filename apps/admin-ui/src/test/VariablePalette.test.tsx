// apps/admin-ui/src/test/VariablePalette.test.tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { VariablePalette } from "../features/editor/sections/VariablePalette";
import type { VariableSpec } from "../config/variableCatalog";

const VARS: VariableSpec[] = [
  {
    name: "first_name",
    tier: "builtin",
    description: "The elder's first name.",
    default: "there",
    example: "Margaret",
    phi: false,
  },
  {
    name: "promo_code",
    tier: "custom",
    description: "Operator-supplied promo code.",
    default: "",
    example: "SPRING",
    phi: false,
  },
];

describe("VariablePalette", () => {
  it("opens a grouped list (Built-in / Custom) on click", async () => {
    const user = userEvent.setup();
    render(<VariablePalette variables={VARS} onInsert={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: /insert variable/i }));

    expect(screen.getByText("Built-in")).toBeInTheDocument();
    expect(screen.getByText("Custom")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /first_name/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /promo_code/ })).toBeInTheDocument();
  });

  it("fires onInsert with the {{token}} when a variable is clicked", async () => {
    const user = userEvent.setup();
    const onInsert = vi.fn();
    render(<VariablePalette variables={VARS} onInsert={onInsert} />);

    await user.click(screen.getByRole("button", { name: /insert variable/i }));
    await user.click(screen.getByRole("button", { name: /first_name/ }));

    expect(onInsert).toHaveBeenCalledWith("{{first_name}}");
  });

  it("closes the list after an insert", async () => {
    const user = userEvent.setup();
    render(<VariablePalette variables={VARS} onInsert={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: /insert variable/i }));
    await user.click(screen.getByRole("button", { name: /promo_code/ }));

    expect(screen.queryByText("Built-in")).not.toBeInTheDocument();
  });

  it("omits an empty tier group", () => {
    const builtinOnly = VARS.filter((v) => v.tier === "builtin");
    render(<VariablePalette variables={builtinOnly} onInsert={vi.fn()} />);
    // Only built-in present: no Custom heading should ever render once opened.
    expect(screen.queryByText("Custom")).not.toBeInTheDocument();
  });
});
