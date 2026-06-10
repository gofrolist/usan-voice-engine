import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../lib/api";

function mockFetch(res: Partial<Response> & { status: number }) {
  const full = {
    ok: res.status >= 200 && res.status < 300,
    statusText: "",
    json: async () => ({}),
    ...res,
  } as Response;
  return vi.fn().mockResolvedValue(full);
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("api wrapper", () => {
  it("redirects to login on 401", async () => {
    vi.stubGlobal("fetch", mockFetch({ status: 401, json: async () => ({}) }));
    const assign = vi.fn();
    vi.stubGlobal("window", { ...window, location: { assign } });

    await expect(api.get("/v1/auth/me")).rejects.toBeInstanceOf(ApiError);
    expect(assign).toHaveBeenCalledWith("/v1/auth/login");
  });

  it("throws ApiError with parsed detail on 409", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch({ status: 409, json: async () => ({ detail: "conflict: stale draft" }) }),
    );

    let caught: unknown;
    try {
      await api.post("/v1/admin/profiles/x/publish", { note: "n" });
    } catch (e) {
      caught = e;
    }
    expect(caught).toBeInstanceOf(ApiError);
    expect((caught as ApiError).status).toBe(409);
    expect((caught as ApiError).detail).toBe("conflict: stale draft");
  });

  it("returns undefined on 204 No Content", async () => {
    const json = vi.fn();
    vi.stubGlobal("fetch", mockFetch({ status: 204, json }));

    const result = await api.del("/v1/admin/profiles/x");
    expect(result).toBeUndefined();
    expect(json).not.toHaveBeenCalled();
  });

  it("sends PATCH with a JSON body", async () => {
    const fetch = mockFetch({ status: 200, json: async () => ({}) });
    vi.stubGlobal("fetch", fetch);

    await api.patch("/v1/admin/follow-up-flags/1", { status: "acknowledged" });

    expect(fetch).toHaveBeenCalledWith("/v1/admin/follow-up-flags/1", {
      method: "PATCH",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: "acknowledged" }),
    });
  });
});
