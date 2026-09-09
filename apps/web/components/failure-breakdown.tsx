import {
  FAILURE_DESCRIPTIONS,
  FAILURE_LABELS,
  type FailureClass,
} from "@/lib/types";
import { Empty } from "./figures";

/**
 * Failure classification, drawn as an emphasis chart.
 *
 * The reader's question is not "how do these eight classes compare" - it is
 * "how many of these are actually my fault". So `real_bug` carries the accent
 * and every other class is de-emphasis gray. A categorical palette here would
 * spend eight hues making the answer harder to see.
 *
 * Every bar is direct-labelled, so identity never rests on color, and the same
 * numbers are available as a table below for screen readers and copy-paste.
 */
export function FailureBreakdown({
  classifications,
}: {
  classifications: Partial<Record<FailureClass, number>>;
}) {
  const entries = Object.entries(classifications)
    .filter(([, count]) => (count ?? 0) > 0)
    .map(([name, count]) => [name as FailureClass, count as number] as const)
    .sort((a, b) => {
      if (a[0] === "real_bug") return -1;
      if (b[0] === "real_bug") return 1;
      return b[1] - a[1];
    });

  if (entries.length === 0) {
    return <Empty>No failures to classify.</Empty>;
  }

  const max = Math.max(...entries.map(([, count]) => count));
  const total = entries.reduce((sum, [, count]) => sum + count, 0);
  const defects = classifications.real_bug ?? 0;

  return (
    <div>
      <p className="mb-4 text-xs" style={{ color: "var(--text-secondary)" }}>
        {defects} of {total} failures {defects === 1 ? "is" : "are"} an
        application defect. The rest are test-side or infrastructure problems and
        do not block a deploy.
      </p>

      <ul className="space-y-2.5">
        {entries.map(([name, count]) => {
          const isDefect = name === "real_bug";
          return (
            <li key={name} className="flex items-center gap-3">
              <span
                className="w-36 shrink-0 text-xs"
                style={{
                  color: isDefect
                    ? "var(--text-primary)"
                    : "var(--text-secondary)",
                  fontWeight: isDefect ? 600 : 400,
                }}
              >
                {FAILURE_LABELS[name]}
              </span>

              <span
                className="bar-track relative h-3 flex-1"
                title={FAILURE_DESCRIPTIONS[name]}
              >
                <span
                  className="bar-fill absolute left-0 top-0"
                  style={{
                    width: `${Math.max((count / max) * 100, 2)}%`,
                    background: isDefect
                      ? "var(--critical)"
                      : "var(--de-emphasis)",
                  }}
                />
              </span>

              <span
                className="tabular w-8 shrink-0 text-right text-xs font-medium"
                style={{ color: "var(--text-primary)" }}
              >
                {count}
              </span>
            </li>
          );
        })}
      </ul>

      <details className="mt-4">
        <summary
          className="cursor-pointer text-xs"
          style={{ color: "var(--text-secondary)" }}
        >
          View as table
        </summary>
        <div className="scroll-x mt-2">
          <table className="w-full text-xs">
            <thead>
              <tr style={{ color: "var(--text-secondary)" }}>
                <th className="py-1 text-left font-medium">Classification</th>
                <th className="py-1 text-right font-medium">Count</th>
                <th className="py-1 pl-4 text-left font-medium">Meaning</th>
              </tr>
            </thead>
            <tbody>
              {entries.map(([name, count]) => (
                <tr
                  key={name}
                  className="border-t"
                  style={{ borderColor: "var(--border)" }}
                >
                  <td className="py-1.5">{FAILURE_LABELS[name]}</td>
                  <td className="tabular py-1.5 text-right">{count}</td>
                  <td
                    className="py-1.5 pl-4"
                    style={{ color: "var(--text-secondary)" }}
                  >
                    {FAILURE_DESCRIPTIONS[name]}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}
