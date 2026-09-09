import type { QualityGate, Severity } from "@/lib/types";

/**
 * Status colors are reserved and never reused as series colors. Each one ships
 * with its own text label, so state is never carried by color alone - which is
 * also what keeps it readable under forced-colors and full CVD.
 */
const SEVERITY_COLOR: Record<Severity, string> = {
  critical: "var(--critical)",
  high: "var(--serious)",
  medium: "var(--warning)",
  low: "var(--text-muted)",
  info: "var(--text-muted)",
};

export function severityColor(severity: Severity): string {
  return SEVERITY_COLOR[severity] ?? "var(--text-muted)";
}

export function SeverityBadge({ severity }: { severity: Severity }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-medium">
      <span
        aria-hidden
        className="inline-block h-2 w-2 rounded-full"
        style={{ background: severityColor(severity) }}
      />
      <span style={{ color: "var(--text-primary)" }}>{severity}</span>
    </span>
  );
}

export function StatusDot({
  tone,
  label,
}: {
  tone: "good" | "warning" | "serious" | "critical" | "muted";
  label: string;
}) {
  const color =
    tone === "muted" ? "var(--text-muted)" : `var(--${tone})`;
  return (
    <span className="inline-flex items-center gap-1.5 text-xs">
      <span
        aria-hidden
        className="inline-block h-2 w-2 rounded-full"
        style={{ background: color }}
      />
      <span style={{ color: "var(--text-secondary)" }}>{label}</span>
    </span>
  );
}

/**
 * The deploy decision.
 *
 * It blocks on classified defects, never on red tests: a gate that blocks
 * because staging was down is a gate that gets switched off permanently.
 */
export function GateBanner({ gate }: { gate: QualityGate }) {
  const passing = gate.result === "pass";
  const noData = gate.result === "no_data";

  const color = noData
    ? "var(--text-muted)"
    : passing
      ? "var(--good)"
      : "var(--critical)";

  const headline = noData
    ? "No data"
    : passing
      ? "Deploy"
      : "Block";

  return (
    <div
      className="flex items-start gap-4 rounded-lg border px-5 py-4"
      style={{
        background: "var(--surface-1)",
        borderColor: "var(--border)",
        borderLeft: `3px solid ${color}`,
      }}
    >
      <div>
        <p className="text-xs" style={{ color: "var(--text-secondary)" }}>
          Quality gate
        </p>
        <p className="mt-0.5 text-lg font-semibold" style={{ color }}>
          {headline}
        </p>
        <p className="mt-1 text-sm" style={{ color: "var(--text-secondary)" }}>
          {gate.reason}
        </p>
      </div>
    </div>
  );
}
