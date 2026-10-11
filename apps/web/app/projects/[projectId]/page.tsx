import Link from "next/link";
import { api, apiBaseUrl, isConfigured, settle } from "@/lib/api";
import { AutoRefresh } from "@/components/auto-refresh";
import { Card, Empty } from "@/components/figures";
import {
  AddEnvironmentForm,
  ConnectRepoForm,
  RunQaForm,
} from "@/components/project-actions";
import { SetupNotice } from "@/components/setup-notice";
import { GateBanner, SeverityBadge, StatusDot } from "@/components/status";
import { VerifyBugButton } from "@/components/verify-bug-button";
import type { RunStatus, Severity } from "@/lib/types";

export const dynamic = "force-dynamic";

const IN_FLIGHT: RunStatus[] = ["pending", "running"];

function runTone(status: RunStatus) {
  if (IN_FLIGHT.includes(status)) return "warning" as const;
  return status === "passed" ? ("good" as const) : ("critical" as const);
}

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

  const [gate, bugs, findings, performance, gates, projects, environments, runs, recordings] =
    await Promise.all([
      settle(api.quality(projectId)),
      settle(api.bugs(projectId)),
      settle(api.securityFindings(projectId)),
      settle(api.performanceRuns(projectId)),
      settle(api.gates(projectId)),
      settle(api.projects()),
      settle(api.environments(projectId)),
      settle(api.runs(projectId)),
      settle(api.recordings(projectId)),
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

  const project = (projects.data ?? []).find((p) => p.id === projectId) ?? null;
  const recentRuns = runs.data ?? [];
  const anyInFlight = recentRuns.some((r) => IN_FLIGHT.includes(r.status));
  const environmentName = new Map(
    (environments.data ?? []).map((env) => [env.id, env.name]),
  );

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

      {/* Polls only while something this page shows is still moving. */}
      {anyInFlight ? <AutoRefresh intervalMs={4000} /> : null}

      {project ? (
        <div>
          <h1 className="text-xl font-semibold">{project.name}</h1>
          <p className="mt-1 text-xs" style={{ color: "var(--text-muted)" }}>
            {project.repo_url ?? "No repository connected"}
          </p>
        </div>
      ) : null}

      {gate.data ? <GateBanner gate={gate.data} /> : null}

      <Card
        title="Run QA"
        description="Discover the API, generate checks, execute them and triage every failure against the chosen environment."
      >
        {environments.error ? (
          <Empty>Could not load environments.</Empty>
        ) : (
          <RunQaForm projectId={projectId} environments={environments.data} />
        )}
      </Card>

      <Card
        title="Recent runs"
        description="Newest first. Pending and running scans refresh on their own."
      >
        {runs.error ? (
          <Empty>Could not load runs.</Empty>
        ) : recentRuns.length === 0 ? (
          <Empty>No runs yet.</Empty>
        ) : (
          <ul className="divide-y" style={{ borderColor: "var(--border)" }}>
            {recentRuns.map((run) => (
              <li key={run.id} className="py-2 first:pt-0 last:pb-0">
                <Link
                  href={`/runs/${run.id}`}
                  className="flex flex-wrap items-center justify-between gap-3 hover:underline"
                >
                  <span className="flex items-center gap-3">
                    <StatusDot tone={runTone(run.status)} label={run.status} />
                    <span className="text-xs" style={{ color: "var(--text-secondary)" }}>
                      {run.trigger}
                      {run.environment_id && environmentName.get(run.environment_id)
                        ? ` · ${environmentName.get(run.environment_id)}`
                        : ""}
                    </span>
                    {run.commit_sha ? (
                      <code className="tabular text-xs" style={{ color: "var(--text-muted)" }}>
                        {run.commit_sha.slice(0, 8)}
                      </code>
                    ) : null}
                  </span>
                  <span className="tabular text-xs" style={{ color: "var(--text-secondary)" }}>
                    {run.total > 0
                      ? `${run.passed}/${run.total} passed`
                      : IN_FLIGHT.includes(run.status)
                        ? "in progress"
                        : "no checks"}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card
          title="Environments"
          description="Running copies of the application a scan sends traffic to. Credentials are set through the API as a secret reference, never typed here."
        >
          {(environments.data ?? []).length > 0 ? (
            <ul className="mb-4 space-y-1.5">
              {(environments.data ?? []).map((env) => (
                <li key={env.id} className="flex flex-wrap items-baseline gap-2 text-xs">
                  <span className="font-medium">{env.name}</span>
                  <span className="break-all" style={{ color: "var(--text-secondary)" }}>
                    {env.base_url ?? env.mode}
                  </span>
                  {env.has_credentials ? (
                    <span style={{ color: "var(--text-muted)" }}>· authenticated</span>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : null}
          <AddEnvironmentForm projectId={projectId} />
        </Card>

        <Card
          title="Repository"
          description="Cloned into a temporary directory, analyzed, then deleted. Only GitHub is supported."
        >
          <ConnectRepoForm projectId={projectId} repoUrl={project?.repo_url ?? null} />
        </Card>
      </div>

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
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="flex items-center gap-3">
                    <Link
                      href={`/bugs/${bug.id}`}
                      className="tabular text-xs font-medium hover:underline"
                      style={{ color: "var(--text-muted)" }}
                    >
                      {bug.reference}
                    </Link>
                    <SeverityBadge severity={bug.severity} />
                    <span className="text-xs" style={{ color: "var(--text-muted)" }}>
                      {bug.status}
                    </span>
                  </div>
                  {bug.status === "open" ? <VerifyBugButton bugId={bug.id} /> : null}
                </div>

                <Link href={`/bugs/${bug.id}`} className="hover:underline">
                  <h3 className="mt-2 text-sm font-semibold">{bug.title}</h3>
                </Link>

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
        title="Recordings"
        description="Sessions captured with the browser extension, stored as a UI flow plus the API checks the flow's traffic produced. Upload with qagent record-import --report-to."
      >
        {recordings.error ? (
          <Empty>Could not load recordings.</Empty>
        ) : (recordings.data ?? []).length === 0 ? (
          <Empty>No recordings stored for this project.</Empty>
        ) : (
          <ul className="divide-y" style={{ borderColor: "var(--border)" }}>
            {(recordings.data ?? []).map((recording) => (
              <li
                key={recording.id}
                className="flex flex-wrap items-baseline justify-between gap-2 py-2 first:pt-0 last:pb-0"
              >
                <span className="text-sm font-medium">{recording.name}</span>
                <span className="tabular text-xs" style={{ color: "var(--text-secondary)" }}>
                  {recording.summary.actions} steps · {recording.api_cases} API checks
                  {recording.diagnostics > 0 ? (
                    <span style={{ color: "var(--critical)" }}>
                      {" "}
                      · {recording.diagnostics} console error
                      {recording.diagnostics === 1 ? "" : "s"}
                    </span>
                  ) : null}
                </span>
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

      <Card
        title="Gate history"
        description="What the gate decided at the moment each change shipped - recorded with the numbers it saw, because recomputing it later gives a different answer."
      >
        {(gates.data ?? []).length === 0 ? (
          <Empty>
            No decisions recorded. Run <code>qagent gate --report-to</code> in CI
            to start keeping this history.
          </Empty>
        ) : (
          <ul className="space-y-2">
            {(gates.data ?? []).map((record) => (
              <li
                key={record.id}
                className="flex flex-wrap items-baseline justify-between gap-2 border-b pb-2 last:border-0"
                style={{ borderColor: "var(--border)" }}
              >
                <span className="flex items-baseline gap-2">
                  <span
                    className="text-sm font-medium"
                    style={{
                      color:
                        record.result === "block"
                          ? "var(--critical)"
                          : record.result === "pass"
                            ? "var(--good)"
                            : "var(--warning)",
                    }}
                  >
                    {record.result}
                  </span>
                  {record.commit_sha ? (
                    <code className="tabular text-xs" style={{ color: "var(--text-secondary)" }}>
                      {record.commit_sha.slice(0, 8)}
                    </code>
                  ) : null}
                  <span className="text-xs" style={{ color: "var(--text-secondary)" }}>
                    {record.trigger}
                  </span>
                </span>
                <span className="text-xs" style={{ color: "var(--text-secondary)" }}>
                  {record.reason ?? "no blocking defects"}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
