import { describe, expect, it } from "vitest";
import { NextRequest } from "next/server";
import { middleware } from "@/middleware";

describe("middleware", () => {
  it("redirects to /login when there is no session cookie", () => {
    const request = new NextRequest("http://x.test/projects/abc");

    const response = middleware(request);

    expect(response.status).toBe(307);
    expect(response.headers.get("location")).toBe("http://x.test/login");
  });

  it("passes the request through when a session cookie is present", () => {
    const request = new NextRequest("http://x.test/projects/abc", {
      headers: { cookie: "qagent_token=some-jwt" },
    });

    const response = middleware(request);

    // NextResponse.next() carries no redirect status/location.
    expect(response.headers.get("location")).toBeNull();
  });
});
