import { describe, expect, it } from "vitest";
import { matchPromptTokens } from "../features/editor/sections/promptTokens";

describe("matchPromptTokens", () => {
  it("matches {{variable}} and {slot} tokens with positions", () => {
    const toks = matchPromptTokens("Hi {{first_name}} in {state}.");
    expect(toks.map((t) => t.text)).toEqual(["{{first_name}}", "{state}"]);
    expect(toks[0]).toMatchObject({ start: 3, end: 17 });
  });

  it("returns nothing for text without braces", () => {
    expect(matchPromptTokens("plain text, no braces")).toEqual([]);
  });

  it("does not let a {{...}} token span an inner '{' (backtrack-free property)", () => {
    // With the old /\{\{[^}]+\}\}/ arm this whole string matched as one token and the
    // engine could backtrack quadratically. The [^{}] arms make tokens non-spanning,
    // so the inner {b} is the only match.
    expect(matchPromptTokens("{{a{b}}").map((t) => t.text)).toEqual(["{b}"]);
  });

  it("matches nothing in a long run of unmatched '{' (no hang)", () => {
    expect(matchPromptTokens("{".repeat(24000))).toEqual([]);
  });

  it("is stateless across calls (fresh regex, no shared lastIndex)", () => {
    expect(matchPromptTokens("{{a}}")).toEqual(matchPromptTokens("{{a}}"));
  });
});
