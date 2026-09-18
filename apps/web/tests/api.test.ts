import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const getToken = vi.fn<() => Promise<string | null>>();

vi.mock("@/lib/session", () => ({
  getToken: () => getToken(),
}));

// lib/api.ts reads BASE_URL from process.env at module-load time, so it must
// be set before the module is imported.
process.env.QAGENT_API_URL = "http://api.test";

const { api, apiBaseUrl, ApiError, isConfigured, settle } = await import("@/lib/api");

describe("api.get (via api.dashboard)", () => {
  beforeEach(() => {
    getToken.mockReset();
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("throws a 401 ApiError without ever calling fetch when there is no token", async () => {
    getToken.mockResolvedValue(null);

    await expect(api.dashboard()).rejects.toMatchObject({ status: 401 });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("sends the bearer token and never caches, since a stale pass rate is worse than a slow one", async () => {
    getToken.mockResolvedValue("secret-token");
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ projects: 1 }), { status: 200 }),
    );

    await api.dashboard();

    expect(fetch).toHaveBeenCalledWith(
      "http://api.test/api/v1/dashboard",
      expect.objectContaining({
        headers: { Authorization: "Bearer secret-token" },
        cache: "no-store",
      }),
    );
  });

  it("returns the parsed JSON body on success", async () => {
    getToken.mockResolvedValue("t");
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ projects: 3 }), { status: 200 }),
    );

    await expect(api.dashboard()).resolves.toEqual({ projects: 3 });
  });

  it("wraps a network failure as a 503 ApiError, matching SetupNotice's contract", async () => {
    getToken.mockResolvedValue("t");
    vi.mocked(fetch).mockRejectedValue(new TypeError("fetch failed"));

    await expect(api.dashboard()).rejects.toMatchObject({ status: 503 });
  });

  it("wraps a non-ok response as an ApiError carrying its status", async () => {
    getToken.mockResolvedValue("t");
    vi.mocked(fetch).mockResolvedValue(new Response("", { status: 500, statusText: "Server Error" }));

    await expect(api.dashboard()).rejects.toMatchObject({ status: 500 });
  });
});

describe("settle", () => {
  it("resolves passing results without throwing", async () => {
    await expect(settle(Promise.resolve(42))).resolves.toEqual({ data: 42, error: null });
  });

  it("captures an ApiError instead of letting it reject, so one dead panel can't blank the page", async () => {
    const error = new ApiError("boom", 503);
    await expect(settle(Promise.reject(error))).resolves.toEqual({ data: null, error });
  });

  it("wraps a non-ApiError rejection into an ApiError too", async () => {
    const result = await settle(Promise.reject(new Error("unexpected")));
    expect(result.data).toBeNull();
    expect(result.error).toBeInstanceOf(ApiError);
  });
});

describe("isConfigured / apiBaseUrl", () => {
  it("is configured exactly when a token exists", async () => {
    getToken.mockResolvedValue("t");
    await expect(isConfigured()).resolves.toBe(true);

    getToken.mockResolvedValue(null);
    await expect(isConfigured()).resolves.toBe(false);
  });

  it("exposes the configured base URL for the setup notice to display", () => {
    expect(apiBaseUrl()).toBe("http://api.test");
  });
});
