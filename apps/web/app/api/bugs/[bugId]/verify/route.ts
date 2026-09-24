import { NextResponse } from "next/server";
import { getToken } from "@/lib/session";

const BASE_URL = process.env.QAGENT_API_URL ?? "http://127.0.0.1:8000";

/**
 * Proxies "verify this fix" to the QAgent API.
 *
 * The verify button is a client component, and a client component cannot read
 * the httpOnly session cookie `lib/api.ts`'s server-side `get()` reads - the
 * same reason sign-in and sign-out go through a route handler instead of
 * calling the QAgent API directly from the browser.
 */
export async function POST(
  request: Request,
  { params }: { params: Promise<{ bugId: string }> },
) {
  const token = await getToken();
  if (!token) {
    return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  }

  const { bugId } = await params;
  const body = await request.json().catch(() => ({}));

  let response: Response;
  try {
    response = await fetch(`${BASE_URL}/api/v1/bugs/${bugId}/verify`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
  } catch {
    return NextResponse.json(
      { error: `Cannot reach the QAgent API at ${BASE_URL}` },
      { status: 503 },
    );
  }

  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    return NextResponse.json(
      { error: payload.detail ?? "Could not queue verification" },
      { status: response.status },
    );
  }
  return NextResponse.json(payload, { status: response.status });
}
