/**
 * Client-side half of the dashboard's writes: POSTs to the dashboard's own
 * route handlers (`lib/proxy.ts` attaches the session token there), and
 * never throws - a form renders `error` instead of crashing.
 */
export type ActionResult<T> = { ok: true; data: T } | { ok: false; error: string };

export async function postAction<T>(path: string, body: unknown): Promise<ActionResult<T>> {
  let response: Response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    return { ok: false, error: "Could not reach the dashboard's own API." };
  }

  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    return { ok: false, error: payload.error ?? `Request failed (${response.status}).` };
  }
  return { ok: true, data: payload as T };
}

export const inputStyle = {
  background: "var(--surface-2)",
  borderColor: "var(--border)",
  color: "var(--text-primary)",
};
