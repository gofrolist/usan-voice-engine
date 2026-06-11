import { describe, expect, it } from "vitest";
import { fmtDate, fmtDuration } from "../lib/format";

describe("fmtDate", () => {
  it("formats a valid ISO string via toLocaleString", () => {
    const iso = "2026-06-10T12:34:56Z";
    expect(fmtDate(iso)).toBe(new Date(iso).toLocaleString());
  });

  it("returns the input verbatim on junk", () => {
    expect(fmtDate("not-a-date")).toBe("not-a-date");
  });
});

describe("fmtDuration", () => {
  it("renders an em dash for null", () => {
    expect(fmtDuration(null)).toBe("—");
  });

  it("renders 0 seconds as 0:00", () => {
    expect(fmtDuration(0)).toBe("0:00");
  });

  it("zero-pads seconds", () => {
    expect(fmtDuration(61)).toBe("1:01");
  });

  it("keeps minutes unbounded past an hour", () => {
    expect(fmtDuration(3725)).toBe("62:05");
  });
});
