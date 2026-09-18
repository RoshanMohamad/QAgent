import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, cleanup } from "@testing-library/react";
import { AutoRefresh } from "@/components/auto-refresh";

const refresh = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh }),
}));

describe("AutoRefresh", () => {
  beforeEach(() => {
    refresh.mockClear();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("refreshes the route on the given interval while a run is in flight", () => {
    render(<AutoRefresh intervalMs={4000} />);

    expect(refresh).not.toHaveBeenCalled();
    vi.advanceTimersByTime(4000);
    expect(refresh).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(8000);
    expect(refresh).toHaveBeenCalledTimes(3);
  });

  it("stops refreshing once unmounted, e.g. when the run reaches a terminal state", () => {
    const { unmount } = render(<AutoRefresh intervalMs={1000} />);
    vi.advanceTimersByTime(1000);
    expect(refresh).toHaveBeenCalledTimes(1);

    unmount();
    vi.advanceTimersByTime(5000);
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("renders nothing - it's a side-effect-only component", () => {
    const { container } = render(<AutoRefresh />);
    expect(container).toBeEmptyDOMElement();
    cleanup();
  });
});
