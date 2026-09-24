import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { VerifyBugButton } from "@/components/verify-bug-button";

const push = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
}));

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

describe("VerifyBugButton", () => {
  beforeEach(() => {
    push.mockClear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts to the dashboard's own verify route, then navigates to the queued run", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ run_id: "run-1", status: "pending" })),
    );

    render(<VerifyBugButton bugId="bug-1" />);
    await userEvent.click(screen.getByRole("button", { name: "Verify fix" }));

    expect(fetch).toHaveBeenCalledWith(
      "/api/bugs/bug-1/verify",
      expect.objectContaining({ method: "POST" }),
    );
    expect(push).toHaveBeenCalledWith("/runs/run-1");
  });

  it("disables the button and shows a queuing label while the request is in flight", async () => {
    let resolveVerify!: (value: Response) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockReturnValue(
        new Promise<Response>((resolve) => {
          resolveVerify = resolve;
        }),
      ),
    );

    render(<VerifyBugButton bugId="bug-1" />);
    const click = userEvent.click(screen.getByRole("button", { name: "Verify fix" }));

    expect(await screen.findByRole("button", { name: "Queuing…" })).toBeDisabled();
    resolveVerify(jsonResponse({ run_id: "run-1" }));
    await click;
  });

  it("shows the server's reason and does not navigate when the defect is not replayable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ error: "an access-control finding needs two identities; run a full scan" }, 409),
      ),
    );

    render(<VerifyBugButton bugId="bug-1" />);
    await userEvent.click(screen.getByRole("button", { name: "Verify fix" }));

    expect(await screen.findByText(/needs two identities/)).toBeInTheDocument();
    expect(push).not.toHaveBeenCalled();
  });

  it("shows a network error without navigating when the dashboard's own API is unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("fetch failed")));

    render(<VerifyBugButton bugId="bug-1" />);
    await userEvent.click(screen.getByRole("button", { name: "Verify fix" }));

    expect(await screen.findByText(/could not reach/i)).toBeInTheDocument();
    expect(push).not.toHaveBeenCalled();
  });
});
