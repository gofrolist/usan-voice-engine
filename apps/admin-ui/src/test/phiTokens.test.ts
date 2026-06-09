// apps/admin-ui/src/test/phiTokens.test.ts
import { describe, expect, it } from "vitest";
import { phiTokenNames } from "../features/editor/sections/phiTokens";

const PHI_NAMES: ReadonlySet<string> = new Set([
  "last_check_in",
  "last_check_in_line",
  "last_mood",
  "last_pain",
  "today_meds",
]);

describe("phiTokenNames", () => {
  it("returns phi token names present in text, in first-seen order", () => {
    expect(
      phiTokenNames("Your mood was {{last_mood}} and pain {{last_pain}}.", PHI_NAMES),
    ).toEqual(["last_mood", "last_pain"]);
  });

  it("ignores non-phi tokens", () => {
    expect(
      phiTokenNames("Hello {{first_name}}, your meds are {{today_meds}}.", PHI_NAMES),
    ).toEqual(["today_meds"]);
  });

  it("de-duplicates — returns each name only once, first-seen order", () => {
    expect(
      phiTokenNames("{{today_meds}} and {{today_meds}} again.", PHI_NAMES),
    ).toEqual(["today_meds"]);
  });

  it("returns [] when no phi tokens are present", () => {
    expect(phiTokenNames("Hello {{first_name}}!", PHI_NAMES)).toEqual([]);
  });

  it("returns [] for plain text with no tokens", () => {
    expect(phiTokenNames("No variables here.", PHI_NAMES)).toEqual([]);
  });

  it("ignores stray braces", () => {
    expect(phiTokenNames("{ bad } and {{ also_bad", PHI_NAMES)).toEqual([]);
  });

  it("handles inner whitespace in token syntax", () => {
    expect(phiTokenNames("{{ last_mood }}", PHI_NAMES)).toEqual(["last_mood"]);
  });
});
