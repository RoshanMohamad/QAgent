import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { GateBanner, severityColor, SeverityBadge, StatusDot } from "@/components/status";
import type { QualityGate } from "@/lib/types";

describe("severityColor", () => {
  it("maps every known severity to a CSS variable", () => {
    expect(severityColor("critical")).toBe("var(--critical)");
    expect(severityColor("high")).toBe("var(--serious)");
    expect(severityColor("medium")).toBe("var(--warning)");
    expect(severityColor("low")).toBe("var(--text-muted)");
    expect(severityColor("info")).toBe("var(--text-muted)");
  });
});

describe("SeverityBadge", () => {
  it("renders the severity as its own visible label, not color alone", () => {
    render(<SeverityBadge severity="critical" />);
    expect(screen.getByText("critical")).toBeInTheDocument();
  });
});

describe("StatusDot", () => {
  it("renders the label text", () => {
    render(<StatusDot tone="good" label="Deploy" />);
    expect(screen.getByText("Deploy")).toBeInTheDocument();
  });

  it("maps each named tone to its own color variable", () => {
    const cases: Array<["good" | "warning" | "serious" | "critical", string]> = [
      ["good", "var(--good)"],
      ["warning", "var(--warning)"],
      ["serious", "var(--serious)"],
      ["critical", "var(--critical)"],
    ];
    for (const [tone, expected] of cases) {
      const { container, unmount } = render(<StatusDot tone={tone} label="x" />);
      const dot = container.querySelector("[aria-hidden]");
      expect(dot).toHaveStyle({ background: expected });
      unmount();
    }
  });

  it("falls back to the muted color for the 'muted' tone", () => {
    const { container } = render(<StatusDot tone="muted" label="x" />);
    const dot = container.querySelector("[aria-hidden]");
    expect(dot).toHaveStyle({ background: "var(--text-muted)" });
  });
});

describe("GateBanner", () => {
  function gate(overrides: Partial<QualityGate>): QualityGate {
    return { result: "pass", reason: "all checks green", ...overrides };
  }

  it("shows Deploy in the good tone when the gate passes", () => {
    render(<GateBanner gate={gate({ result: "pass" })} />);
    expect(screen.getByText("Deploy")).toBeInTheDocument();
  });

  it("shows Block when the gate fails, never silently as a pass", () => {
    render(<GateBanner gate={gate({ result: "block", reason: "critical vuln found" })} />);
    expect(screen.getByText("Block")).toBeInTheDocument();
    expect(screen.getByText("critical vuln found")).toBeInTheDocument();
  });

  it("shows 'No data' rather than a false pass or block when there is nothing to gate on", () => {
    render(<GateBanner gate={gate({ result: "no_data", reason: "no runs yet" })} />);
    expect(screen.getByText("No data")).toBeInTheDocument();
  });

  it("always states the reason, since a bare verdict isn't actionable", () => {
    render(<GateBanner gate={gate({ result: "block", reason: "2 critical bugs open" })} />);
    expect(screen.getByText("2 critical bugs open")).toBeInTheDocument();
  });
});
