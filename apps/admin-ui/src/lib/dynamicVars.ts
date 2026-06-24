// Shared helpers for the dynamic_vars / call-personalization editor. The server
// caps a schedule's / call's dynamic_vars at 8192 bytes of serialized JSON and
// rejects non-scalar values; we mirror the byte cap client-side for instant
// feedback (the server 422 stays authoritative).
export const DYNAMIC_VARS_MAX_BYTES = 8192;

export function dynamicVarsByteSize(vars: Record<string, string>): number {
  return new TextEncoder().encode(JSON.stringify(vars)).length;
}
