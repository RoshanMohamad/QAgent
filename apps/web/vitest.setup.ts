import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Vitest's `globals` option is off (tests import describe/it/expect explicitly),
// so React Testing Library's own auto-cleanup - which hooks into a *global*
// afterEach - never registers itself. Without this, one test's rendered DOM is
// still present when the next test in the same file queries the document.
afterEach(() => {
  cleanup();
});
