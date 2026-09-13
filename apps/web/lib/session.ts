import { cookies } from "next/headers";

/** Must match the literal used in middleware.ts (edge runtime can't import this module). */
export const SESSION_COOKIE = "qagent_token";

export async function getToken(): Promise<string | null> {
  const store = await cookies();
  return store.get(SESSION_COOKIE)?.value ?? null;
}
