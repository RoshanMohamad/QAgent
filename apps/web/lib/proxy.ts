import { NextResponse } from "next/server";
import { getToken } from "@/lib/session";

const BASE_URL = process.env.QAGENT_API_URL ?? "http://127.0.0.1:8000";

/**
 * Forwards one write from a client component to the QAgent API.
 *
 * Client components cannot read the httpOnly session cookie, so every action
 * the dashboard takes goes through a route handler that attaches the token
 * server-side. Each handler names its own upstream path - there is no generic
 * catch-all, so the browser can only reach the writes the dashboard exposes.
 *
 * FastAPI reports errors as `detail` (a string, or a list of validation
 * errors); this flattens both into `{ error }` so components show one shape.
 */
export async function proxyPost(
  path: string,
  request: Request,
  fallbackError: string,
): Promise<NextResponse> {
  const token = await getToken();
  if (!token) {
    return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  }

  const body = await request.json().catch(() => ({}));

  let response: Response;
  try {
    response = await fetch(`${BASE_URL}${path}`, {
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
      { error: describeError(payload.detail) ?? fallbackError },
      { status: response.status },
    );
  }
  return NextResponse.json(payload, { status: response.status });
}

/**
 * Streams one binary download (an evidence artifact) from the QAgent API.
 *
 * Same reasoning as `proxyPost`: an `<a href>` in the browser cannot carry the
 * httpOnly token, so the download goes through a handler that attaches it.
 * The body is passed through untouched and only the headers a download needs
 * are copied, so nothing else from the API leaks to the browser.
 */
export async function proxyDownload(path: string): Promise<Response> {
  const token = await getToken();
  if (!token) {
    return NextResponse.json({ error: "Not signed in" }, { status: 401 });
  }

  let response: Response;
  try {
    response = await fetch(`${BASE_URL}${path}`, {
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
    });
  } catch {
    return NextResponse.json(
      { error: `Cannot reach the QAgent API at ${BASE_URL}` },
      { status: 503 },
    );
  }

  if (!response.ok) {
    return NextResponse.json({ error: "Artifact not found" }, { status: response.status });
  }

  const headers = new Headers();
  for (const name of ["content-type", "content-disposition", "content-length"]) {
    const value = response.headers.get(name);
    if (value) headers.set(name, value);
  }
  return new Response(response.body, { status: 200, headers });
}

export function describeError(detail: unknown): string | null {
  if (typeof detail === "string") {
    return detail;
  }
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (!item || typeof item !== "object") return null;
        const { loc, msg } = item as { loc?: unknown[]; msg?: string };
        const field = Array.isArray(loc) ? loc.filter((p) => p !== "body").join(".") : "";
        return msg ? (field ? `${field}: ${msg}` : msg) : null;
      })
      .filter(Boolean);
    return messages.length > 0 ? messages.join("; ") : null;
  }
  return null;
}
