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

export interface DashboardSummary {
  projects: number;
  runs: number;
  tests: { total: number; passed: number; failed: number };
  bugs: Partial<Record<Severity, number>>;
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

export interface QualityGate {
  result: "pass" | "block" | "no_data";
  reason: string;
  run?: { id: string; total: number; passed: number; failed: number };
  open_bugs?: Partial<Record<Severity, number>>;
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
