import Link from "next/link";
import type { ApiError } from "@/lib/api";

/**
 * The dashboard is useless without a reachable API, so it says exactly what is
 * wrong and how to fix it rather than rendering an empty shell of zeroes. A
 * dashboard showing 0 defects because it cannot connect is actively dangerous.
 */
export function SetupNotice({
  error,
  baseUrl,
  configured,
}: {
  error: ApiError;
  baseUrl: string;
  configured: boolean;
}) {
  const unreachable = error.status === 503;
  const needsSignIn = !configured || error.status === 401;

  return (
    <div
      className="rounded-lg border px-5 py-4"
      style={{
        background: "var(--surface-1)",
        borderColor: "var(--border)",
        borderLeft: "3px solid var(--warning)",
      }}
    >
      <h2 className="text-sm font-semibold">
        {needsSignIn
          ? "You're not signed in"
          : unreachable
            ? "Cannot reach the QAgent API"
            : "The API returned an error"}
      </h2>

      <p className="mt-1 text-sm" style={{ color: "var(--text-secondary)" }}>
        {error.message}
      </p>

      <div className="mt-3 text-xs" style={{ color: "var(--text-secondary)" }}>
        {needsSignIn ? (
          <p>
            <Link href="/login" className="underline">
              Sign in or create an organization
            </Link>{" "}
            to view this dashboard.
          </p>
        ) : (
          <>
            <p>Start the API and its dependencies:</p>
            <pre
              className="scroll-x mt-2 rounded border px-3 py-2"
              style={{
                background: "var(--surface-2)",
                borderColor: "var(--border)",
              }}
            >
              docker compose up postgres redis api
            </pre>
            <p className="mt-2">
              API base URL: <code>{baseUrl}</code>
            </p>
          </>
        )}
      </div>
    </div>
  );
}
