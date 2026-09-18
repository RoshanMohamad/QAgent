import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { FailureBreakdown } from "@/components/failure-breakdown";

/** The bar list, not the collapsible <details> table repeating the same labels. */
function barLabels(container: HTMLElement): (string | null)[] {
  const list = container.querySelector("ul");
  if (!list) return [];
  return within(list)
    .getAllByText(/Real defect|Flaky test|Environment/)
    .map((el) => el.textContent);
}

describe("FailureBreakdown", () => {
  it("shows the empty state when there are no failures", () => {
    render(<FailureBreakdown classifications={{}} />);
    expect(screen.getByText("No failures to classify.")).toBeInTheDocument();
  });

  it("drops classifications with a zero count instead of showing an empty bar", () => {
    render(<FailureBreakdown classifications={{ real_bug: 3, flaky_test: 0 }} />);
    // "Real defect" legitimately appears twice: the bar list and the
    // accessible <details> table repeating the same numbers.
    expect(screen.getAllByText("Real defect").length).toBeGreaterThan(0);
    expect(screen.queryByText("Flaky test")).not.toBeInTheDocument();
  });

  it("always sorts real_bug first regardless of count, since that's the reader's question", () => {
    const { container } = render(
      <FailureBreakdown
        classifications={{ flaky_test: 10, environment: 8, real_bug: 1 }}
      />,
    );
    expect(barLabels(container)[0]).toBe("Real defect");
  });

  it("sorts everything after real_bug by count, descending", () => {
    const { container } = render(
      <FailureBreakdown classifications={{ flaky_test: 2, environment: 8 }} />,
    );
    expect(barLabels(container)).toEqual(["Environment", "Flaky test"]);
  });

  it("uses singular phrasing for exactly one defect", () => {
    render(<FailureBreakdown classifications={{ real_bug: 1, flaky_test: 1 }} />);
    expect(screen.getByText(/1 of 2 failures is an/)).toBeInTheDocument();
  });

  it("uses plural phrasing for more than one defect", () => {
    render(<FailureBreakdown classifications={{ real_bug: 2, flaky_test: 1 }} />);
    expect(screen.getByText(/2 of 3 failures are an/)).toBeInTheDocument();
  });

  it("treats a class with zero defects as plural ('0 ... are')", () => {
    render(<FailureBreakdown classifications={{ flaky_test: 5 }} />);
    expect(screen.getByText(/0 of 5 failures are an/)).toBeInTheDocument();
  });

  it("renders the same numbers in the collapsible table for screen readers", () => {
    render(<FailureBreakdown classifications={{ real_bug: 3 }} />);
    const table = screen.getByRole("table");
    expect(table).toHaveTextContent("Real defect");
    expect(table).toHaveTextContent("3");
  });
});
