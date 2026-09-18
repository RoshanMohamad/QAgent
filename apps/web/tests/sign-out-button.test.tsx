import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SignOutButton } from "@/components/sign-out-button";

const push = vi.fn();
const refresh = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, refresh }),
}));

describe("SignOutButton", () => {
  beforeEach(() => {
    push.mockClear();
    refresh.mockClear();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true } as Response),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts to /api/logout, then navigates to /login and refreshes", async () => {
    render(<SignOutButton />);

    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));

    expect(fetch).toHaveBeenCalledWith("/api/logout", { method: "POST" });
    expect(push).toHaveBeenCalledWith("/login");
    expect(refresh).toHaveBeenCalled();
  });

  it("navigates away only after the logout request settles, not before", async () => {
    let resolveLogout!: (value: Response) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockReturnValue(
        new Promise<Response>((resolve) => {
          resolveLogout = resolve;
        }),
      ),
    );

    render(<SignOutButton />);
    const click = userEvent.click(screen.getByRole("button", { name: "Sign out" }));

    expect(push).not.toHaveBeenCalled();
    resolveLogout({ ok: true } as Response);
    await click;

    expect(push).toHaveBeenCalledWith("/login");
  });
});
