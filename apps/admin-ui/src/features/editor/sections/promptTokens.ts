// Prompt {{variable}} / {slot} token matching, extracted from PromptEditor so it can
// be unit-tested without Monaco.
//
// Both alternatives use [^{}] (never [^}]) so a token can never grow across an inner
// or unmatched brace. That keeps matching linear and backtrack-free: a pathological
// run of "{" with no closer matches nothing instead of scanning quadratically.
export const PROMPT_TOKEN_RE = /\{\{[^{}]+\}\}|\{[^{}]+\}/g;

export interface TokenMatch {
  text: string;
  start: number;
  end: number;
}

export function matchPromptTokens(text: string): TokenMatch[] {
  // Fresh RegExp per call so there is no shared `lastIndex` state across editors/calls.
  const re = new RegExp(PROMPT_TOKEN_RE.source, "g");
  const out: TokenMatch[] = [];
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    out.push({ text: m[0], start: m.index, end: m.index + m[0].length });
  }
  return out;
}
