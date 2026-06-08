/** Tiny classnames joiner — falsy values are dropped. No external dep. */
export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}
