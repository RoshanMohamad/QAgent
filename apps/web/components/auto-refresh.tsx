"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

/**
 * Re-fetches the server component tree while a run is still in flight.
 *
 * Deliberately not a data-fetching library: the page is already a server
 * component reading fresh data, so refreshing the route is the whole
 * requirement. The interval stops as soon as the run reaches a terminal state,
 * because the parent stops rendering this component.
 */
export function AutoRefresh({ intervalMs = 4000 }: { intervalMs?: number }) {
  const router = useRouter();

  useEffect(() => {
    const id = setInterval(() => router.refresh(), intervalMs);
    return () => clearInterval(id);
  }, [router, intervalMs]);

  return null;
}
