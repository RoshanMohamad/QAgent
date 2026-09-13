import Link from "next/link";
import { api, apiBaseUrl, isConfigured, settle } from "@/lib/api";
import { AutoRefresh } from "@/components/auto-refresh";
import { FailureBreakdown } from "@/components/failure-breakdown";
import { Card, Empty, StatTile, compact } from "@/components/figures";
import { SetupNotice } from "@/components/setup-notice";
import { StatusDot } from "@/components/status";
import { FAILURE_LABELS } from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function RunPage({
  params,
}: {
  params: Promise<{ runId: string }>;
}) {
  const { runId } = await params;

  const [run, results] = await Promise.all([
    settle(api.run(runId)),
    settle(api.results(runId, true)),
  ]);

  if (run.error) {
    return (
      <SetupNotice
        error={run.error}
        baseUrl={apiBaseUrl()}
        configured={await isConfigured()}
      />
    );
  }

  const data = run.data;
  const inFlight = data.status === "pending" || data.status === "running";

  return (
    <div className="space-y-6">
      {/* Only polls while the run is still moving; a finished run is static. */}
      {inFlight ? <AutoRefresh intervalMs={4000} /> : null}

      <div className="flex items-center justify-between gap-4">
        <Link
          href="/"
          className="text-xs hover:underline"
          style={{ color: "var(--text-secondary)" }}
        >
          &larr; Dashboard
        </Link>
        <StatusDot
          tone={
            inFlight
              ? "warning"
              : data.status === "passed"
                ? "good"
                : "critical"
          }
          label={inFlight ? `${data.status}, refreshing` : data.status}
        />
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile label="Checks" value={compact(data.total)} />
        <StatTile
          label="Passed"
          value={compact(data.passed)}
          accent={data.passed > 0 ? "var(--good)" : undefined}
        />
        <StatTile
          label="Failed"
          value={compact(data.failed)}
          accent={data.failed > 0 ? "var(--critical)" : undefined}
        />
        <StatTile label="Errored" value={compact(data.errored)} />
      </div>

      <Card
        title="Failure analysis"
        description="Why each failing check failed. A red check is not automatically a defect."
      >
        <FailureBreakdown classifications={data.classifications} />
      </Card>

      <Card
        title="Failing checks"
        description="Passing checks are hidden."
      >
        {results.error ? (
          <Empty>Could not load results.</Empty>
        ) : results.data.length === 0 ? (
          <Empty>
            {inFlight ? "No results yet." : "Every check passed."}
          </Empty>
        ) : (
          <ul className="divide-y" style={{ borderColor: "var(--border)" }}>
            {results.data.map((result) => (
              <li key={result.id} className="py-3 first:pt-0 last:pb-0">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <p className="text-sm font-medium">{result.name}</p>
                  <div className="flex items-center gap-3">
                    {result.failure_class ? (
                      <span
                        className="text-xs font-medium"
                        style={{
                          color:
                            result.failure_class === "real_bug"
                              ? "var(--critical)"
                              : "var(--text-secondary)",
                        }}
                      >
                        {FAILURE_LABELS[result.failure_class]}
                        {result.confidence !== null
                          ? ` · ${Math.round(result.confidence * 100)}%`
                          : ""}
                      </span>
                    ) : null}
                    <span
                      className="tabular text-xs"
                      style={{ color: "var(--text-muted)" }}
                    >
                      {result.duration_ms}ms
                    </span>
                  </div>
                </div>

                {result.failure_message ? (
                  <p
                    className="mt-1 break-words text-xs"
                    style={{ color: "var(--text-secondary)" }}
                  >
                    {result.failure_message}
                  </p>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
