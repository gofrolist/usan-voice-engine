// PHI-variable detection for editor warnings. Mirrors unknownTokens.ts — same
// TOKEN_RE, same de-dup/first-seen contract — but filters for names that are
// in the caller-supplied phi set rather than outside the known set.
const TOKEN_RE = /\{\{\s*([a-zA-Z0-9_]+)\s*\}\}/g;

// Names present as {{tokens}} whose name IS in phiNames, de-duped in first-seen
// order. Drives the non-blocking PHI notice (NOT a Zod error).
export function phiTokenNames(text: string, phiNames: ReadonlySet<string>): string[] {
  const re = new RegExp(TOKEN_RE.source, "g");
  const seen = new Set<string>();
  const out: string[] = [];
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    const name = m[1];
    if (name !== undefined && phiNames.has(name) && !seen.has(name)) {
      seen.add(name);
      out.push(name);
    }
  }
  return out;
}

// Prompt field keys that are spoken before caller identity is confirmed or to
// voicemail. PHI variables in these fields trigger the non-blocking editor warning.
export const SENSITIVE_PROMPT_FIELDS: ReadonlySet<string> = new Set([
  "greeting",
  "inbound_opening",
  "recording_disclosure",
  "voicemail_message",
]);
