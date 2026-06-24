import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { DaysOfWeekPicker } from "../components/ui/DaysOfWeekPicker";
import type { Weekday } from "../types/api";

function Harness({ initial = [] as Weekday[] }) {
  const [days, setDays] = useState<Weekday[]>(initial);
  return (
    <>
      <DaysOfWeekPicker value={days} onChange={setDays} />
      <output data-testid="days">{days.join(",")}</output>
    </>
  );
}

describe("DaysOfWeekPicker", () => {
  it("toggles days and always emits Mon-first order", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByLabelText("friday"));
    await user.click(screen.getByLabelText("monday"));
    expect(screen.getByTestId("days")).toHaveTextContent("monday,friday");
    await user.click(screen.getByLabelText("monday"));
    expect(screen.getByTestId("days")).toHaveTextContent("friday");
  });
});
