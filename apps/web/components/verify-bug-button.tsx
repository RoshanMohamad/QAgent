"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

/**
 * Replays one defect's check to confirm a fix (qagent/verification.py),
 * without waiting for the whole suite to run again.
 *
 * Verification is queued the same way a scan is, so on success this hands off
 * to the run page - it already polls a run to completion and shows pass/fail,
 * and duplicating that here would be two places tracking the same state.
 */
export function VerifyBugButton({ bugId }: { bugId: string }) {
  const router = useRouter();
  const [state, setState] = useState<"idle" | "posting" | "error">("idle");
  const [error, setError] = useState<string | null>(null);

  async function verify() {
    setState("posting");
    setError(null);

    let response: Response;
    try {
      response = await fetch(`/api/bugs/${bugId}/verify`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
    } catch {
      setState("error");
      setError("Could not reach the dashboard's own API.");
      return;
    }

    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      setState("error");
      // 409: this defect's check cannot be replayed alone (e.g. IDOR, or a
      // browser defect) - the message names why; a full scan is the fallback.
      setError(payload.error ?? "Could not queue verification.");
      return;
    }

    router.push(`/runs/${payload.run_id}`);
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <button
        type="button"
        onClick={verify}
        disabled={state === "posting"}
        className="rounded-md border px-2.5 py-1 text-xs font-medium disabled:opacity-60"
        style={{ borderColor: "var(--border)", color: "var(--accent)" }}
      >
        {state === "posting" ? "Queuing…" : "Verify fix"}
      </button>
      {state === "error" && error ? (
        <p className="max-w-[220px] text-right text-xs" style={{ color: "var(--critical)" }}>
          {error}
        </p>
      ) : null}
    </div>
  );
}
