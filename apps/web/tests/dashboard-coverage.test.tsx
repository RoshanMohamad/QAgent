import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { StatTile } from "@/components/figures";
import type { DashboardSummary } from "@/lib/types";

/**
 * The dashboard's coverage tile.
 *
 * The assertions here are mostly about *wording*, which is unusual for a UI
 * test and deliberate: CLAUDE.md's mock asks for "Backend 82%", which reads as
 * line coverage. QAgent tests the application as a black box and never
 * instruments it, so it cannot produce that number. A tile that showed a bare
 * percentage would be read as a claim about tested code paths, and it is not
 * one - so the label and the caption are load-bearing, not decoration.
 */

function coverageOf(summary: Partial<DashboardSummary>) {
  // Mirrors the fallback in app/page.tsx: an older deployment has no
  // `coverage` key at all, and the page must render rather than crash.
  return (
    summary.coverage ?? {
      endpoints_total: 0,
      endpoints_covered: 0,
      endpoint_percent: 0,
      measures: "",
    }
  );
}

describe("dashboard coverage tile", () => {
  it("shows the share of the surface that was exercised", () => {
    const coverage = coverageOf({
      coverage: {
        endpoints_total: 8,
        endpoints_covered: 2,
        endpoint_percent: 25,
        measures: "Not line coverage.",
      },
    });

    render(
      <StatTile
        label="API surface"
        value={`${coverage.endpoint_percent}%`}
        detail={`${coverage.endpoints_covered} of ${coverage.endpoints_total} endpoints`}
      />,
    );

    expect(screen.getByText("25%")).toBeInTheDocument();
    expect(screen.getByText("2 of 8 endpoints")).toBeInTheDocument();
  });

  it("is labelled 'API surface', never 'Backend coverage'", () => {
    render(<StatTile label="API surface" value="25%" />);

    expect(screen.getByText("API surface")).toBeInTheDocument();
    expect(screen.queryByText(/backend coverage/i)).toBeNull();
  });

  it("renders a placeholder rather than 0% when nothing was discovered", () => {
    const coverage = coverageOf({});

    render(
      <StatTile
        label="API surface"
        value={coverage.endpoints_total === 0 ? "--" : `${coverage.endpoint_percent}%`}
        detail={
          coverage.endpoints_total === 0
            ? "nothing discovered yet"
            : `${coverage.endpoints_covered} of ${coverage.endpoints_total} endpoints`
        }
      />,
    );

    // 0% would read as "your API is untested"; "--" reads as "we have not looked".
    expect(screen.getByText("--")).toBeInTheDocument();
    expect(screen.queryByText("0%")).toBeNull();
  });

  it("survives a response from a deployment that predates the field", () => {
    const coverage = coverageOf({ projects: 1 } as Partial<DashboardSummary>);

    expect(coverage.endpoints_total).toBe(0);
    expect(() =>
      render(<StatTile label="API surface" value="--" detail="nothing discovered yet" />),
    ).not.toThrow();
  });
});
