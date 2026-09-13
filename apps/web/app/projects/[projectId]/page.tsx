import Link from "next/link";
import { api, apiBaseUrl, isConfigured, settle } from "@/lib/api";
import { Card, Empty } from "@/components/figures";
import { SetupNotice } from "@/components/setup-notice";
import { GateBanner, SeverityBadge } from "@/components/status";
import type { Severity } from "@/lib/types";

export const dynamic = "force-dynamic";

const SEVERITY_RANK: Record<Severity, number> = {
  critical: 0,
  high: 1,
  medium: 2,
  low: 3,
  info: 4,
};

export default async function ProjectPage({
  params,
}: {
  params: Promise<{ projectId: string }>;
}) {
  const { projectId } = await params;

  const [gate, bugs, findings, performance] = await Promise.all([
    settle(api.quality(projectId)),
    settle(api.bugs(projectId)),
    settle(api.securityFindings(projectId)),
    settle(api.performanceRuns(projectId)),
  ]);

  if (gate.error && bugs.error) {
    return (
      <SetupNotice
        error={gate.error}
        baseUrl={apiBaseUrl()}
        configured={await isConfigured()}
      />
    );
  }

  const sorted = (bugs.data ?? [])
    .slice()
    .sort((a, b) => SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity]);

  const sortedFindings = (findings.data ?? [])
    .slice()
    .sort((a, b) => SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity]);

  return (
    <div className="space-y-6">
      <Link
        href="/"
        className="text-xs hover:underline"
        style={{ color: "var(--text-secondary)" }}
      >
        &larr; All projects
      </Link>

      {gate.data ? <GateBanner gate={gate.data} /> : null}

      {gate.data?.run ? (
        <Link
          href={`/runs/${gate.data.run.id}`}
          className="inline-block text-xs hover:underline"
          style={{ color: "var(--accent)" }}
        >
          View the latest run &rarr;
        </Link>
      ) : null}

      <Card
        title="Defects"
        description="Reproducible reports for failures triaged as application faults."
      >
        {sorted.length === 0 ? (
          <Empty>No defects reported for this project.</Empty>
        ) : (
          <ul className="space-y-4">
            {sorted.map((bug) => (
              <li
                key={bug.reference}
                className="rounded-md border px-4 py-3"
                style={{
                  background: "var(--surface-2)",
                  borderColor: "var(--border)",
                }}
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-3">
                    <span
                      className="tabular text-xs font-medium"
                      style={{ color: "var(--text-muted)" }}
                    >
                      {bug.reference}
                    </span>
                    <SeverityBadge severity={bug.severity} />
                  </div>
                  <span className="text-xs" style={{ color: "var(--text-muted)" }}>
                    {bug.status}
                  </span>
                </div>

                <h3 className="mt-2 text-sm font-semibold">{bug.title}</h3>

                <dl className="mt-3 grid gap-x-6 gap-y-2 text-xs sm:grid-cols-2">
                  <div>
                    <dt style={{ color: "var(--text-muted)" }}>Expected</dt>
                    <dd className="mt-0.5">{bug.expected ?? "--"}</dd>
                  </div>
                  <div>
                    <dt style={{ color: "var(--text-muted)" }}>Actual</dt>
                    <dd className="mt-0.5 break-words">{bug.actual ?? "--"}</dd>
                  </div>
                </dl>

                {bug.steps.length > 0 ? (
                  <div className="mt-3">
                    <p className="text-xs" style={{ color: "var(--text-muted)" }}>
                      Reproduction
                    </p>
                    <ol
                      className="mt-1 list-inside list-decimal space-y-0.5 text-xs"
                      style={{ color: "var(--text-secondary)" }}
                    >
                      {bug.steps.map((step, index) => (
                        <li key={index} className="break-words">
                          {step}
                        </li>
                      ))}
                    </ol>
                  </div>
                ) : null}

                {bug.root_cause ? (
                  <p
                    className="mt-3 text-xs"
                    style={{ color: "var(--text-secondary)" }}
                  >
                    <span style={{ color: "var(--text-muted)" }}>
                      Root cause.{" "}
                    </span>
                    {bug.root_cause}
                  </p>
                ) : null}

                {bug.suggested_fix ? (
                  <p
                    className="mt-1.5 text-xs"
                    style={{ color: "var(--text-secondary)" }}
                  >
                    <span style={{ color: "var(--text-muted)" }}>
                      Suggested fix.{" "}
                    </span>
                    {bug.suggested_fix}
                  </p>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card
        title="Security findings"
        description="Static analysis (Semgrep). Critical/high count toward the quality gate."
      >
        {findings.error ? (
          <Empty>Could not load security findings.</Empty>
        ) : sortedFindings.length === 0 ? (
          <Empty>No security findings for this project.</Empty>
        ) : (
          <ul className="space-y-3">
            {sortedFindings.map((finding) => (
              <li
                key={`${finding.rule_id}:${finding.path}:${finding.line}`}
                className="rounded-md border px-4 py-3"
                style={{
                  background: "var(--surface-2)",
                  borderColor: "var(--border)",
                }}
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-3">
                    <SeverityBadge severity={finding.severity} />
                    <span
                      className="tabular text-xs"
                      style={{ color: "var(--text-muted)" }}
                    >
                      {finding.path}:{finding.line}
                    </span>
                  </div>
                  <span className="text-xs" style={{ color: "var(--text-muted)" }}>
                    {finding.tool}
                  </span>
                </div>
                <p className="mt-2 text-xs font-medium">{finding.rule_id}</p>
                <p className="mt-1 text-xs" style={{ color: "var(--text-secondary)" }}>
                  {finding.message}
                </p>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card
        title="Load test scenarios"
        description="k6, latest scans first. One row per VU level; the most recent scan's failures count toward the quality gate."
      >
        {performance.error ? (
          <Empty>Could not load performance runs.</Empty>
        ) : (performance.data ?? []).length === 0 ? (
          <Empty>No load tests recorded for this project.</Empty>
        ) : (
          <div className="scroll-x">
            <table className="w-full text-xs">
              <thead>
                <tr
                  className="text-left"
                  style={{ color: "var(--text-muted)" }}
                >
                  <th className="pb-2 pr-4 font-medium">VUs</th>
                  <th className="pb-2 pr-4 font-medium">req/s</th>
                  <th className="pb-2 pr-4 font-medium">failed</th>
                  <th className="pb-2 pr-4 font-medium">p95</th>
                  <th className="pb-2 pr-4 font-medium">p99</th>
                  <th className="pb-2 font-medium">result</th>
                </tr>
              </thead>
              <tbody className="divide-y" style={{ borderColor: "var(--border)" }}>
                {(performance.data ?? []).map((run, index) => (
                  <tr key={index} className="tabular">
                    <td className="py-1.5 pr-4">{run.vus}</td>
                    <td className="py-1.5 pr-4">{run.requests_per_s.toFixed(1)}</td>
                    <td className="py-1.5 pr-4">{(run.failed_rate * 100).toFixed(2)}%</td>
                    <td className="py-1.5 pr-4">{run.latency_p95_ms.toFixed(0)}ms</td>
                    <td className="py-1.5 pr-4">{run.latency_p99_ms.toFixed(0)}ms</td>
                    <td className="py-1.5">
                      <span
                        style={{
                          color: run.passed ? "var(--good)" : "var(--critical)",
                        }}
                      >
                        {run.passed ? "pass" : "fail"}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
