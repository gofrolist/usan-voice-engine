import type {
  TestAudioRequest,
  TestAudioResponse,
  TestLlmRequest,
  TestLlmResponse,
} from "../types/api";

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
  ) {
    super(detail);
    this.name = "ApiError";
  }
}

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    credentials: "include",
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) {
    window.location.assign("/v1/auth/login");
    throw new ApiError(401, "redirecting to login");
  }
  if (!res.ok) {
    const detail = await res
      .json()
      .then((b) => b?.detail ?? res.statusText)
      .catch(() => res.statusText);
    throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  get: <T>(u: string) => request<T>("GET", u),
  post: <T>(u: string, b?: unknown) => request<T>("POST", u, b),
  put: <T>(u: string, b?: unknown) => request<T>("PUT", u, b),
  patch: <T>(u: string, b?: unknown) => request<T>("PATCH", u, b),
  del: <T>(u: string) => request<T>("DELETE", u),
};

// --- Pre-publish agent test endpoints (US5) ---------------------------------
// Sandboxed: test/llm runs the draft against Vertex with stub tools (no DB writes);
// test/audio mints a join-only browser token + dispatches a session_kind="test"
// agent. Both take admin-supplied SYNTHETIC sample_vars only — no real PHI.

export function testProfileLlm(
  profileId: string,
  body: TestLlmRequest,
): Promise<TestLlmResponse> {
  return api.post<TestLlmResponse>(`/v1/admin/profiles/${profileId}/test/llm`, body);
}

export function testProfileAudio(
  profileId: string,
  body: TestAudioRequest,
): Promise<TestAudioResponse> {
  return api.post<TestAudioResponse>(`/v1/admin/profiles/${profileId}/test/audio`, body);
}
