import type {
  Bug,
  BugHistory,
  DashboardSummary,
  GateRecord,
  Project,
  QualityGate,
  PerformanceRun,
  Result,
  Run,
  SecurityFinding,
} from "./types";
import { getToken } from "./session";

const BASE_URL = process.env.QAGENT_API_URL ?? "http://127.0.0.1:8000";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

/**
 * The dashboard shows live quality data, so nothing here is cached. A stale
 * pass rate is worse than a slow one: it invites a deploy against numbers that
 * no longer hold.
 */
async function get<T>(path: string): Promise<T> {
  const token = await getToken();
  if (!token) {
    throw new ApiError("Not signed in", 401);
  }

  let response: Response;
  try {
    response = await fetch(`${BASE_URL}${path}`, {
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
    });
  } catch (cause) {
    throw new ApiError(`Cannot reach the QAgent API at ${BASE_URL}`, 503);
  }

  if (!response.ok) {
    throw new ApiError(
      `${path} returned ${response.status} ${response.statusText}`,
      response.status,
    );
  }
  return (await response.json()) as T;
}

export const api = {
  dashboard: () => get<DashboardSummary>("/api/v1/dashboard"),
  projects: () => get<Project[]>("/api/v1/projects"),
  bugs: (projectId: string) => get<Bug[]>(`/api/v1/projects/${projectId}/bugs`),
  securityFindings: (projectId: string) =>
    get<SecurityFinding[]>(`/api/v1/projects/${projectId}/security`),
  performanceRuns: (projectId: string) =>
    get<PerformanceRun[]>(`/api/v1/projects/${projectId}/performance`),
  quality: (projectId: string) =>
    get<QualityGate>(`/api/v1/projects/${projectId}/quality`),
  gates: (projectId: string) =>
    get<GateRecord[]>(`/api/v1/projects/${projectId}/gates`),
  bugHistory: (bugId: string) => get<BugHistory>(`/api/v1/bugs/${bugId}/history`),
  run: (runId: string) => get<Run>(`/api/v1/runs/${runId}`),
  results: (runId: string, onlyFailed = true) =>
    get<Result[]>(`/api/v1/runs/${runId}/results?only_failed=${onlyFailed}`),
};

/** Resolve a request without throwing, so one dead panel does not blank the page. */
export async function settle<T>(
  promise: Promise<T>,
): Promise<{ data: T; error: null } | { data: null; error: ApiError }> {
  try {
    return { data: await promise, error: null };
  } catch (error) {
    return {
      data: null,
      error:
        error instanceof ApiError
          ? error
          : new ApiError(String(error), 500),
    };
  }
}

export async function isConfigured(): Promise<boolean> {
  return Boolean(await getToken());
}

export function apiBaseUrl(): string {
  return BASE_URL;
}
