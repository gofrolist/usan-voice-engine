// Unknown-{{variable}} detection for editor warnings. Token-scoped on {{name}} only
// (single-brace slots and stray braces are validation's concern, not warnings).
// Mirrors the agent/API TOKEN_RE: {{ name }} with optional inner whitespace.
const TOKEN_RE = /\{\{\s*([a-zA-Z0-9_]+)\s*\}\}/g;

// All {{name}} token names in document order (duplicates kept).
export function tokenNames(text: string): string[] {
  const re = new RegExp(TOKEN_RE.source, "g");
  const out: string[] = [];
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m[1] !== undefined) out.push(m[1]);
  }
  return out;
}

// Names present as {{tokens}} but NOT in the known catalog set, de-duped, in first-seen
// order. Drives the non-blocking "unknown variable: …" notice (NOT a Zod error).
export function unknownTokenNames(text: string, known: ReadonlySet<string>): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const name of tokenNames(text)) {
    if (!known.has(name) && !seen.has(name)) {
      seen.add(name);
      out.push(name);
    }
  }
  return out;
}
