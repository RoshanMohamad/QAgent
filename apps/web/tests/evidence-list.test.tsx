import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { EvidenceList, formatBytes } from "@/components/evidence-list";

describe("EvidenceList", () => {
  it("links each artifact through the dashboard's own download route", () => {
    render(
      <EvidenceList
        artifacts={[
          { id: "a-1", kind: "trace", content_type: "application/zip", size_bytes: 2_500_000 },
          { id: "a-2", kind: "har", content_type: "application/json", size_bytes: 512 },
        ]}
      />,
    );

    const links = screen.getAllByRole("link", { name: "download" });
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "/api/artifacts/a-1",
      "/api/artifacts/a-2",
    ]);
    expect(screen.getByText("Playwright trace")).toBeInTheDocument();
    expect(screen.getByText(/playwright show-trace/)).toBeInTheDocument();
    expect(screen.getByText("2.4 MB")).toBeInTheDocument();
  });

  it("says why there is nothing, rather than rendering an empty list", () => {
    render(<EvidenceList artifacts={[]} />);
    expect(screen.getByText(/retention window/)).toBeInTheDocument();
  });

  it("falls back to the raw kind for one it does not know", () => {
    render(
      <EvidenceList
        artifacts={[{ id: "a-3", kind: "video", content_type: "video/webm", size_bytes: 10 }]}
      />,
    );
    expect(screen.getByText("video")).toBeInTheDocument();
  });
});

describe("formatBytes", () => {
  it("picks a readable unit", () => {
    expect(formatBytes(900)).toBe("900 B");
    expect(formatBytes(9 * 1024)).toBe("9.0 KB");
    expect(formatBytes(3 * 1024 * 1024)).toBe("3.0 MB");
  });
});
