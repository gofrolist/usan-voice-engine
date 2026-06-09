// apps/admin-ui/src/test/unknownTokens.test.ts
import { describe, expect, it } from "vitest";
import { tokenNames, unknownTokenNames } from "../features/editor/sections/unknownTokens";

describe("tokenNames", () => {
  it("extracts {{name}} token names (with inner spaces) in order", () => {
    expect(tokenNames("Hi {{first_name}} and {{ last_mood }}.")).toEqual([
      "first_name",
      "last_mood",
    ]);
  });

  it("ignores single-brace slots and stray braces", () => {
    expect(tokenNames("Hi {elder_name} and {")).toEqual([]);
  });
});

describe("unknownTokenNames", () => {
  const known = new Set(["first_name", "last_mood"]);

  it("returns token names not in the known set, de-duped and ordered", () => {
    expect(
      unknownTokenNames("Hi {{first_name}}, {{made_up}} and {{made_up}} {{other}}.", known),
    ).toEqual(["made_up", "other"]);
  });

  it("returns [] when every token is known", () => {
    expect(unknownTokenNames("Hi {{first_name}} {{last_mood}}.", known)).toEqual([]);
  });

  it("returns [] for text with no tokens", () => {
    expect(unknownTokenNames("plain text", known)).toEqual([]);
  });
});
