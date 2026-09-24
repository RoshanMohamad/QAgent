import Link from "next/link";
import { api, apiBaseUrl, isConfigured, settle } from "@/lib/api";
import { Card, Empty } from "@/components/figures";
import { SetupNotice } from "@/components/setup-notice";
import { SeverityBadge } from "@/components/status";
import { VerifyBugButton } from "@/components/verify-bug-button";

export const dynamic = "force-dynamic";

/** One line per event, in the reader's terms rather than the raw event name. */
const EVENT_LABELS: Record<string, string> = {
  opened: "Opened",
  reproduced: "Reproduced again",
  reopened: "Reopened - a regression",
  severity_changed: "Severity changed",
  closed: "Closed",
  verified_fixed: "Verified fixed",
  verification_failed: "Verification failed - still reproduces",
  verification_inconclusive: "Verification inconclusive",
  synced: "Synced to an external tracker",
};

export default async function BugPage({
  params,
}: {
  params: Promise<{ bugId: string }>;
}) {
  const { bugId } = await params;
  const history = await settle(api.bugHistory(bugId));

  if (history.error) {
    return (
      <SetupNotice
        error={history.error}
        baseUrl={apiBaseUrl()}
        configured={await isConfigured()}
      />
    );
  }

  const { bug, events, comments, reopen_count } = history.data;

  return (
    <div className="space-y-6">
      <Link
        href="/"
        className="text-xs hover:underline"
        style={{ color: "var(--text-secondary)" }}
      >
        &larr; Dashboard
      </Link>

      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-3">
            <span className="tabular text-xs" style={{ color: "var(--text-muted)" }}>
              {bug.reference}
            </span>
            <SeverityBadge severity={bug.severity} />
            <span className="text-xs" style={{ color: "var(--text-muted)" }}>
              {bug.status}
            </span>
          </div>
          <h1 className="mt-1 text-lg font-semibold">{bug.title}</h1>
        </div>

        {bug.status === "open" ? <VerifyBugButton bugId={bug.id} /> : null}
      </div>

      <Card
        title="History"
        description={
          reopen_count > 0
            ? `Reopened ${reopen_count} time${reopen_count === 1 ? "" : "s"} after being closed.`
            : "Every state change, in order."
        }
      >
        {events.length === 0 ? (
          <Empty>No events recorded.</Empty>
        ) : (
          <ol className="space-y-3">
            {events.map((event, index) => (
              <li
                key={index}
                className="flex flex-wrap items-baseline justify-between gap-2 border-b pb-3 last:border-0 last:pb-0"
                style={{ borderColor: "var(--border)" }}
              >
                <span className="flex items-baseline gap-2">
                  <span
                    className="text-sm font-medium"
                    style={{
                      color:
                        event.event === "reopened" || event.event === "verification_failed"
                          ? "var(--critical)"
                          : event.event === "verified_fixed" || event.event === "closed"
                            ? "var(--good)"
                            : "var(--text-primary)",
                    }}
                  >
                    {EVENT_LABELS[event.event] ?? event.event}
                  </span>
                  {event.from && event.to ? (
                    <span className="text-xs" style={{ color: "var(--text-muted)" }}>
                      {event.from} &rarr; {event.to}
                    </span>
                  ) : null}
                </span>
                <span className="flex items-center gap-3">
                  {event.run_id ? (
                    <Link
                      href={`/runs/${event.run_id}`}
                      className="text-xs hover:underline"
                      style={{ color: "var(--accent)" }}
                    >
                      view run
                    </Link>
                  ) : null}
                  <span className="tabular text-xs" style={{ color: "var(--text-muted)" }}>
                    {new Date(event.at).toLocaleString()}
                  </span>
                </span>
              </li>
            ))}
          </ol>
        )}
      </Card>

      <Card
        title="Comments"
        description="Human notes and QAgent's own retrieved root-cause evidence."
      >
        {comments.length === 0 ? (
          <Empty>No comments yet.</Empty>
        ) : (
          <ul className="space-y-3">
            {comments.map((comment) => (
              <li
                key={comment.id}
                className="rounded-md border px-4 py-3"
                style={{ background: "var(--surface-2)", borderColor: "var(--border)" }}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs font-medium" style={{ color: "var(--text-muted)" }}>
                    {comment.generated ? "QAgent" : "Team member"}
                  </span>
                  <span className="tabular text-xs" style={{ color: "var(--text-muted)" }}>
                    {new Date(comment.at).toLocaleString()}
                  </span>
                </div>
                <p className="mt-2 whitespace-pre-wrap text-xs" style={{ color: "var(--text-secondary)" }}>
                  {comment.body}
                </p>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
