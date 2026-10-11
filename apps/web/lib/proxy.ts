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
