import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import {
  KeyValueEditor,
  rowsToRecord,
  recordToRows,
  type KvRow,
} from "../components/ui/KeyValueEditor";
import { dynamicVarsByteSize, DYNAMIC_VARS_MAX_BYTES } from "../lib/dynamicVars";

function Harness({ initial = [] as KvRow[] }) {
  const [rows, setRows] = useState<KvRow[]>(initial);
  return (
    <>
      <KeyValueEditor rows={rows} onChange={setRows} label="Variables" />
      <output data-testid="record">{JSON.stringify(rowsToRecord(rows))}</output>
    </>
  );
}

describe("dynamicVars helpers", () => {
  it("byte size counts JSON bytes and the cap is 8192", () => {
    expect(DYNAMIC_VARS_MAX_BYTES).toBe(8192);
    expect(dynamicVarsByteSize({ a: "b" })).toBe(JSON.stringify({ a: "b" }).length);
  });

  it("rowsToRecord drops empty keys; last duplicate wins", () => {
    expect(
      rowsToRecord([
        { id: "1", key: "first_name", value: "Jane" },
        { id: "2", key: "  ", value: "ignored" },
        { id: "3", key: "first_name", value: "Janet" },
      ]),
    ).toEqual({ first_name: "Janet" });
  });

  it("recordToRows round-trips a record into editable rows", () => {
    expect(recordToRows({ a: "1", b: "2" }).map((r) => [r.key, r.value])).toEqual([
      ["a", "1"],
      ["b", "2"],
    ]);
  });
});

describe("KeyValueEditor", () => {
  it("adds, edits and removes rows, emitting the record", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Add variable" }));
    await user.type(screen.getByLabelText("Variables key"), "first_name");
    await user.type(screen.getByLabelText("Variables value"), "Jane");
    expect(screen.getByTestId("record")).toHaveTextContent('{"first_name":"Jane"}');
    await user.click(screen.getByRole("button", { name: /Remove/ }));
    expect(screen.getByTestId("record")).toHaveTextContent("{}");
  });
});
