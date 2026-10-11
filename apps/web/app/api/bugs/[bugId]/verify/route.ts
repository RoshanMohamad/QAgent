import { proxyPost } from "@/lib/proxy";

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
  const { bugId } = await params;
  return proxyPost(
    `/api/v1/bugs/${encodeURIComponent(bugId)}/verify`,
    request,
    "Could not queue verification",
  );
}
