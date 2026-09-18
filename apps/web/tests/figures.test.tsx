import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { compact, Card, Empty, HeroFigure, StatTile } from "@/components/figures";

describe("compact", () => {
  it("formats small numbers with thousands separators", () => {
    expect(compact(1284)).toBe("1,284");
  });

  it("formats thousands as K, dropping a trailing .0", () => {
    expect(compact(12_900)).toBe("12.9K");
    expect(compact(10_000)).toBe("10K");
  });

  it("formats millions as M, dropping a trailing .0", () => {
    expect(compact(4_200_000)).toBe("4.2M");
    expect(compact(2_000_000)).toBe("2M");
  });

  it("handles negative values by magnitude", () => {
    expect(compact(-12_900)).toBe("-12.9K");
  });

  it("leaves values under the 10K threshold as plain numbers", () => {
    expect(compact(9_999)).toBe("9,999");
  });
});

describe("HeroFigure", () => {
  it("renders the label and value", () => {
    render(<HeroFigure label="Overall score" value="87/100" />);
    expect(screen.getByText("Overall score")).toBeInTheDocument();
    expect(screen.getByText("87/100")).toBeInTheDocument();
  });

  it("renders an optional caption only when given", () => {
    const { rerender } = render(<HeroFigure label="x" value="1" />);
    expect(screen.queryByText("caption text")).not.toBeInTheDocument();

    rerender(<HeroFigure label="x" value="1" caption="caption text" />);
    expect(screen.getByText("caption text")).toBeInTheDocument();
  });

  it("colors the value by tone: good, critical, or the neutral default", () => {
    const { rerender } = render(<HeroFigure label="x" value="87" tone="good" />);
    expect(screen.getByText("87")).toHaveStyle({ color: "var(--good)" });

    rerender(<HeroFigure label="x" value="87" tone="critical" />);
    expect(screen.getByText("87")).toHaveStyle({ color: "var(--critical)" });

    rerender(<HeroFigure label="x" value="87" />);
    expect(screen.getByText("87")).toHaveStyle({ color: "var(--text-primary)" });
  });
});

describe("StatTile", () => {
  it("renders label, value and optional detail", () => {
    render(<StatTile label="Tests" value="1,248" detail="18 flaky" />);
    expect(screen.getByText("Tests")).toBeInTheDocument();
    expect(screen.getByText("1,248")).toBeInTheDocument();
    expect(screen.getByText("18 flaky")).toBeInTheDocument();
  });

  it("omits the detail line when none is given", () => {
    render(<StatTile label="Tests" value="1,248" />);
    expect(screen.queryByText("18 flaky")).not.toBeInTheDocument();
  });
});

describe("Card", () => {
  it("renders a title, optional description and children", () => {
    render(
      <Card title="Failure analysis" description="per-run breakdown">
        <p>child content</p>
      </Card>,
    );
    expect(screen.getByText("Failure analysis")).toBeInTheDocument();
    expect(screen.getByText("per-run breakdown")).toBeInTheDocument();
    expect(screen.getByText("child content")).toBeInTheDocument();
  });

  it("renders an action slot when given", () => {
    render(
      <Card title="x" action={<button type="button">Refresh</button>}>
        child
      </Card>,
    );
    expect(screen.getByRole("button", { name: "Refresh" })).toBeInTheDocument();
  });
});

describe("Empty", () => {
  it("renders its message", () => {
    render(<Empty>No failures to classify.</Empty>);
    expect(screen.getByText("No failures to classify.")).toBeInTheDocument();
  });
});
