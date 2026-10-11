import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const getToken = vi.fn<() => Promise<string | null>>();

vi.mock("@/lib/session", () => ({
  getToken: () => getToken(),
}));

process.env.QAGENT_API_URL = "http://api.test";

const { describeError, proxyPost } = await import("@/lib/proxy");

function request(body: unknown): Request {
  return new Request("http://dashboard.test/api/x", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

describe("proxyPost", () => {
  beforeEach(() => {
    getToken.mockReset();
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("refuses without a session and never reaches the API", async () => {
    getToken.mockResolvedValue(null);

    const response = await proxyPost("/api/v1/projects", request({}), "nope");

    expect(response.status).toBe(401);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("attaches the bearer token server-side and forwards the body and status", async () => {
    getToken.mockResolvedValue("secret-token");
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ id: "p-1" }), { status: 201 }),
    );

    const response = await proxyPost("/api/v1/projects", request({ name: "shop" }), "nope");

    expect(fetch).toHaveBeenCalledWith(
      "http://api.test/api/v1/projects",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ name: "shop" }),
        headers: expect.objectContaining({ Authorization: "Bearer secret-token" }),
      }),
    );
    expect(response.status).toBe(201);
    expect(await response.json()).toEqual({ id: "p-1" });
  });

  it("flattens FastAPI's detail into { error } and keeps the upstream status", async () => {
    getToken.mockResolvedValue("t");
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ detail: "owner role required" }), { status: 403 }),
    );

    const response = await proxyPost("/api/v1/x", request({}), "fallback");

    expect(response.status).toBe(403);
    expect(await response.json()).toEqual({ error: "owner role required" });
  });

  it("answers 503 with the API address when the API is unreachable", async () => {
    getToken.mockResolvedValue("t");
    vi.mocked(fetch).mockRejectedValue(new TypeError("fetch failed"));

    const response = await proxyPost("/api/v1/x", request({}), "fallback");

    expect(response.status).toBe(503);
    expect((await response.json()).error).toContain("http://api.test");
  });
});

describe("describeError", () => {
  it("passes a string through", () => {
    expect(describeError("project not found")).toBe("project not found");
  });

  it("names the field for each validation error, without the 'body' prefix", () => {
    expect(
      describeError([
        { loc: ["body", "base_url"], msg: "Field required" },
        { loc: ["body", "name"], msg: "String should have at most 100 characters" },
      ]),
    ).toBe("base_url: Field required; name: String should have at most 100 characters");
  });

  it("returns null for anything it cannot read, so the caller's fallback applies", () => {
    expect(describeError(undefined)).toBeNull();
    expect(describeError([{}])).toBeNull();
  });
});
