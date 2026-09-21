/** Shapes returned by the QAgent API. Mirrors apps/api/qagent/main.py. */

export type FailureClass =
  | "real_bug"
  | "flaky_test"
  | "environment"
  | "network"
  | "dependency"
  | "test_data"
  | "bad_assertion"
  | "unknown";

export type Severity = "critical" | "high" | "medium" | "low" | "info";

export type RunStatus =
  | "pending"
  | "running"
  | "passed"
  | "failed"
  | "error"
  | "cancelled";

/**
 * Coverage of the API *surface*, not of lines.
 *
 * QAgent tests the application as a black box and never instruments it, so it
 * cannot produce line coverage. `measures` carries that sentence from the API
 * and the UI shows it, because "82%" on a dashboard is read as a claim about
 * tested code paths and this is not one.
 */
export interface SurfaceCoverage {
  endpoints_total: number;
  endpoints_covered: number;
  endpoint_percent: number;
  measures: string;
}

export interface DashboardSummary {
  projects: number;
  runs: number;
  tests: { total: number; passed: number; failed: number; flaky: number };
  bugs: Partial<Record<Severity, number>>;
  security_findings: Partial<Record<Severity, number>>;
  coverage: SurfaceCoverage;
  llm_spend_usd: number;
  generated_at: string;
}

export interface Project {
  id: string;
  name: string;
  repo_url: string | null;
  stack: Record<string, unknown>;
}

export interface Run {
  id: string;
  status: RunStatus;
  total: number;
  passed: number;
  failed: number;
  errored: number;
  started_at: string | null;
  finished_at: string | null;
  classifications: Partial<Record<FailureClass, number>>;
}

export interface Assertion {
  type: string;
  passed: boolean;
  expected: unknown;
  actual: unknown;
  message: string;
}

export interface Result {
  id: string;
  name: string;
  status: "passed" | "failed" | "skipped" | "error";
  duration_ms: number;
  failure_class: FailureClass | null;
  confidence: number | null;
  failure_message: string | null;
  assertions: Assertion[];
}

export interface Bug {
  reference: string;
  title: string;
  severity: Severity;
  status: string;
  expected: string | null;
  actual: string | null;
  root_cause: string | null;
  suggested_fix: string | null;
  steps: string[];
}

export interface SecurityFinding {
  tool: string;
  rule_id: string;
  title: string;
  severity: Severity;
  path: string;
  line: number;
  message: string;
  confidence: string | null;
  cwe: string[];
  owasp: string[];
}

export interface PerformanceRun {
  tool: string;
  base_url: string;
  vus: number;
  duration_s: number;
  requests: number;
  requests_per_s: number;
  failed_rate: number;
  latency_avg_ms: number;
  latency_p95_ms: number;
  latency_p99_ms: number;
  latency_max_ms: number;
  passed: boolean;
  created_at: string;
}

export interface BugEvent {
  event: string;
  from: string | null;
  to: string | null;
  run_id: string | null;
  at: string;
}

export interface BugComment {
  id: string;
  body: string;
  /** True when QAgent wrote it - a machine's opinion, not a colleague's. */
  generated: boolean;
  author_user_id: string | null;
  at: string;
}

export interface BugHistory {
  bug: {
    id: string;
    reference: string;
    title: string;
    severity: Severity;
    status: string;
    first_seen: string;
  };
  events: BugEvent[];
  comments: BugComment[];
  /** How often this defect came back after being closed. */
  reopen_count: number;
}

export interface GateRecord {
  id: string;
  result: "pass" | "block" | "error";
  reason: string | null;
  commit_sha: string | null;
  trigger: string;
  checks: { name: string; passed: boolean; detail: string }[];
  at: string;
}

export interface QualityGate {
  result: "pass" | "block" | "no_data";
  reason: string;
  run?: { id: string; total: number; passed: number; failed: number };
  open_bugs?: Partial<Record<Severity, number>>;
  open_security_findings?: Partial<Record<Severity, number>>;
  failing_performance_scenarios?: number;
}

/** Human-readable labels. The API returns machine names; the UI never shows them raw. */
export const FAILURE_LABELS: Record<FailureClass, string> = {
  real_bug: "Real defect",
  flaky_test: "Flaky test",
  environment: "Environment",
  network: "Network",
  dependency: "Dependency",
  test_data: "Test data",
  bad_assertion: "Over-specified test",
  unknown: "Unclassified",
};

/** One sentence explaining what each classification means, shown on hover. */
export const FAILURE_DESCRIPTIONS: Record<FailureClass, string> = {
  real_bug: "The application is at fault. These are the only failures that block a deploy.",
  flaky_test: "Passed and failed recently without a corresponding change.",
  environment: "Unreachable, or credentials were not configured for this environment.",
  network: "Timed out before the application returned a verdict.",
  dependency: "A database or upstream service was unavailable.",
  test_data: "The fixture this check depends on is missing.",
  bad_assertion: "The application was right; the check was too strict.",
  unknown: "No rule matched with confidence. Escalated for review.",
};
