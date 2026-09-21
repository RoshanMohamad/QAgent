import Link from "next/link";
import { api, apiBaseUrl, isConfigured, settle } from "@/lib/api";
import { Card, Empty, HeroFigure, StatTile, compact } from "@/components/figures";
import { SetupNotice } from "@/components/setup-notice";
import { SeverityBadge } from "@/components/status";
import type { Severity } from "@/lib/types";

export const dynamic = "force-dynamic";

const SEVERITY_ORDER: Severity[] = ["critical", "high", "medium", "low", "info"];

export default async function DashboardPage() {
  const [summary, projects] = await Promise.all([
    settle(api.dashboard()),
    settle(api.projects()),
  ]);

  if (summary.error) {
    return (
      <SetupNotice
        error={summary.error}
        baseUrl={apiBaseUrl()}
        configured={await isConfigured()}
      />
    );
  }

  const data = summary.data;
  const { total, passed, failed } = data.tests;
  // Older deployments predate this field; the UI must not crash on them.
  const coverage = data.coverage ?? {
    endpoints_total: 0,
    endpoints_covered: 0,
    endpoint_percent: 0,
    measures: "",
  };
  const passRate = total > 0 ? Math.round((passed / total) * 100) : null;

  const openBugs = SEVERITY_ORDER.reduce(
    (sum, severity) => sum + (data.bugs[severity] ?? 0),
    0,
  );
  const openFindings = SEVERITY_ORDER.reduce(
    (sum, severity) => sum + (data.security_findings[severity] ?? 0),
    0,
  );
  const blocking =
    (data.bugs.critical ?? 0) +
    (data.bugs.high ?? 0) +
    (data.security_findings.critical ?? 0) +
    (data.security_findings.high ?? 0);

  return (
    <div className="space-y-8">
      {/* Exactly one hero figure per view. */}
      <div className="flex flex-wrap items-end justify-between gap-6">
        <HeroFigure
          label="Checks passing"
          value={passRate === null ? "--" : `${passRate}%`}
          tone={passRate === null ? "neutral" : passRate >= 95 ? "good" : "critical"}
          caption={
            total > 0 ? (
              <>
                {compact(passed)} of {compact(total)} checks across{" "}
                {data.runs} {data.runs === 1 ? "run" : "runs"}
              </>
            ) : (
              "No checks have run yet"
            )
          }
        />

        <div className="text-right">
          <p className="text-xs" style={{ color: "var(--text-secondary)" }}>
            Open defects + findings
          </p>
          <p
            className="mt-1 text-3xl font-semibold"
            style={{
              color: blocking > 0 ? "var(--critical)" : "var(--text-primary)",
            }}
          >
            {openBugs + openFindings}
          </p>
          {blocking > 0 ? (
            <p className="mt-1 text-xs" style={{ color: "var(--critical)" }}>
              {blocking} blocking a deploy
            </p>
          ) : null}
        </div>
      </div>

      {/* KPI row: headline numbers, not a grouped bar chart. */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
        <StatTile label="Projects" value={compact(data.projects)} />
        <StatTile label="Checks executed" value={compact(total)} />
        <StatTile
          label="Failing"
          value={compact(failed)}
          accent={failed > 0 ? "var(--critical)" : undefined}
          detail={failed > 0 ? "before classification" : "nothing red"}
        />
        <StatTile
          label="Flaky"
          value={compact(data.tests.flaky)}
          accent={data.tests.flaky > 0 ? "var(--warning)" : undefined}
          detail="quarantined, not blocking"
        />
        <StatTile
          label="API surface"
          value={
            coverage.endpoints_total === 0
              ? "--"
              : `${coverage.endpoint_percent}%`
          }
          accent={
            coverage.endpoints_total > 0 && coverage.endpoint_percent < 80
              ? "var(--warning)"
              : undefined
          }
          detail={
            coverage.endpoints_total === 0
              ? "nothing discovered yet"
              : `${coverage.endpoints_covered} of ${coverage.endpoints_total} endpoints`
          }
        />
        <StatTile
          label="AI spend"
          value={`$${data.llm_spend_usd.toFixed(2)}`}
          detail="across all runs"
        />
      </div>

      {/* The label is doing real work here. "82%" on a dashboard reads as line
          coverage, and this is not that - QAgent never instruments the app. */}
      {coverage.endpoints_total > 0 ? (
        <p className="text-xs" style={{ color: "var(--text-secondary)" }}>
          {coverage.measures}
        </p>
      ) : null}

      <div className="grid gap-6 md:grid-cols-3">
        <Card
          title="Open defects by severity"
          description="Only defects appear here. Test-side failures are excluded by triage."
        >
          {openBugs === 0 ? (
            <Empty>No open defects.</Empty>
          ) : (
            <ul className="space-y-2">
              {SEVERITY_ORDER.filter((s) => (data.bugs[s] ?? 0) > 0).map(
                (severity) => (
                  <li
                    key={severity}
                    className="flex items-center justify-between"
                  >
                    <SeverityBadge severity={severity} />
                    <span className="tabular text-sm font-medium">
                      {data.bugs[severity]}
                    </span>
                  </li>
                ),
              )}
            </ul>
          )}
        </Card>

        <Card
          title="Security findings"
          description="Semgrep, Trivy and ZAP. Critical/high count toward the quality gate."
        >
          {openFindings === 0 ? (
            <Empty>No open findings.</Empty>
          ) : (
            <ul className="space-y-2">
              {SEVERITY_ORDER.filter((s) => (data.security_findings[s] ?? 0) > 0).map(
                (severity) => (
                  <li
                    key={severity}
                    className="flex items-center justify-between"
                  >
                    <SeverityBadge severity={severity} />
                    <span className="tabular text-sm font-medium">
                      {data.security_findings[severity]}
                    </span>
                  </li>
                ),
              )}
            </ul>
          )}
        </Card>

        <Card title="Projects" description="Connected applications under test.">
          {projects.error ? (
            <Empty>Could not load projects.</Empty>
          ) : projects.data.length === 0 ? (
            <Empty>
              No projects yet. Create one through the API, then point an
              environment at a running application.
            </Empty>
          ) : (
            <ul className="divide-y" style={{ borderColor: "var(--border)" }}>
              {projects.data.map((project) => (
                <li key={project.id} className="py-2 first:pt-0 last:pb-0">
                  <Link
                    href={`/projects/${project.id}`}
                    className="flex items-center justify-between gap-4 hover:underline"
                  >
                    <span className="text-sm font-medium">{project.name}</span>
                    <span
                      className="truncate text-xs"
                      style={{ color: "var(--text-muted)" }}
                    >
                      {project.repo_url ?? "no repository"}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </div>
  );
}
