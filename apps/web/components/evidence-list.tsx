import type { Artifact } from "@/lib/types";
import { Empty } from "@/components/figures";

/** What each kind is, and what a reader does with it once downloaded. */
const KINDS: Record<string, { label: string; hint: string }> = {
  screenshot: { label: "Screenshot", hint: "The page at the moment it failed." },
  log: { label: "Console log", hint: "Console errors and uncaught exceptions." },
  har: {
    label: "HAR",
    hint: "Replay the request in browser DevTools, Postman or Insomnia.",
  },
  trace: {
    label: "Playwright trace",
    hint: "Open with `npx playwright show-trace` or at trace.playwright.dev.",
  },
};

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function EvidenceList({ artifacts }: { artifacts: Artifact[] }) {
  if (artifacts.length === 0) {
    return <Empty>No evidence stored, or it has passed the retention window.</Empty>;
  }

  return (
    <ul className="space-y-2">
      {artifacts.map((artifact) => {
        const kind = KINDS[artifact.kind] ?? { label: artifact.kind, hint: "" };
        return (
          <li
            key={artifact.id}
            className="flex flex-wrap items-baseline justify-between gap-2 border-b pb-2 last:border-0 last:pb-0"
            style={{ borderColor: "var(--border)" }}
          >
            <span>
              <span className="text-sm font-medium">{kind.label}</span>
              {kind.hint ? (
                <span className="ml-2 text-xs" style={{ color: "var(--text-muted)" }}>
                  {kind.hint}
                </span>
              ) : null}
            </span>
            <span className="flex items-center gap-3">
              <span className="tabular text-xs" style={{ color: "var(--text-muted)" }}>
                {formatBytes(artifact.size_bytes)}
              </span>
              <a
                href={`/api/artifacts/${encodeURIComponent(artifact.id)}`}
                className="text-xs hover:underline"
                style={{ color: "var(--accent)" }}
                download
              >
                download
              </a>
            </span>
          </li>
        );
      })}
    </ul>
  );
}
