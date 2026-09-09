import type { ReactNode } from "react";

/** Auto-compact formatting: 1,284 / 12.9K / 4.2M. */
export function compact(value: number): string {
  if (Math.abs(value) >= 1_000_000) {
    return `${(value / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  }
  if (Math.abs(value) >= 10_000) {
    return `${(value / 1_000).toFixed(1).replace(/\.0$/, "")}K`;
  }
  return value.toLocaleString("en-US");
}

/**
 * The single number the view leads with. Exactly one per page.
 * Proportional figures, not tabular: at display sizes tabular digits read loose.
 */
export function HeroFigure({
  label,
  value,
  caption,
  tone = "neutral",
}: {
  label: string;
  value: string;
  caption?: ReactNode;
  tone?: "neutral" | "good" | "critical";
}) {
  const color =
    tone === "good"
      ? "var(--good)"
      : tone === "critical"
        ? "var(--critical)"
        : "var(--text-primary)";

  return (
    <div>
      <p className="text-sm" style={{ color: "var(--text-secondary)" }}>
        {label}
      </p>
      <p
        className="mt-1 font-semibold leading-none"
        style={{ fontSize: "56px", color }}
      >
        {value}
      </p>
      {caption ? (
        <p className="mt-2 text-sm" style={{ color: "var(--text-secondary)" }}>
          {caption}
        </p>
      ) : null}
    </div>
  );
}

/**
 * A headline number. Deliberately not a one-bar chart: a single current value
 * is a figure, and a KPI row of these beats a grouped bar chart.
 */
export function StatTile({
  label,
  value,
  detail,
  accent,
}: {
  label: string;
  value: string;
  detail?: string;
  accent?: string;
}) {
  return (
    <div
      className="rounded-lg border px-4 py-3"
      style={{
        background: "var(--surface-2)",
        borderColor: "var(--border)",
      }}
    >
      <p className="text-xs" style={{ color: "var(--text-secondary)" }}>
        {label}
      </p>
      <p
        className="mt-1 text-2xl font-semibold leading-tight"
        style={{ color: accent ?? "var(--text-primary)" }}
      >
        {value}
      </p>
      {detail ? (
        <p className="mt-0.5 text-xs" style={{ color: "var(--text-muted)" }}>
          {detail}
        </p>
      ) : null}
    </div>
  );
}

export function Card({
  title,
  description,
  children,
  action,
}: {
  title: string;
  description?: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <section
      className="rounded-lg border"
      style={{ background: "var(--surface-1)", borderColor: "var(--border)" }}
    >
      <header
        className="flex items-start justify-between gap-4 border-b px-5 py-4"
        style={{ borderColor: "var(--border)" }}
      >
        <div>
          <h2 className="text-sm font-semibold">{title}</h2>
          {description ? (
            <p
              className="mt-0.5 text-xs"
              style={{ color: "var(--text-secondary)" }}
            >
              {description}
            </p>
          ) : null}
        </div>
        {action}
      </header>
      <div className="px-5 py-4">{children}</div>
    </section>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <p className="py-6 text-center text-sm" style={{ color: "var(--text-muted)" }}>
      {children}
    </p>
  );
}
