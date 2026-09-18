import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { SetupNotice } from "@/components/setup-notice";
import { ApiError } from "@/lib/api";

describe("SetupNotice", () => {
  it("prompts sign-in when the dashboard was never configured, regardless of error status", () => {
    render(
      <SetupNotice error={new ApiError("Not signed in", 401)} baseUrl="http://x" configured={false} />,
    );
    expect(screen.getByText("You're not signed in")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /sign in or create an organization/i })).toHaveAttribute(
      "href",
      "/login",
    );
  });

  it("prompts sign-in on a 401 even when a token was configured (e.g. expired)", () => {
    render(
      <SetupNotice error={new ApiError("token expired", 401)} baseUrl="http://x" configured={true} />,
    );
    expect(screen.getByText("You're not signed in")).toBeInTheDocument();
  });

  it("shows an unreachable-API message on a 503, distinct from a sign-in problem", () => {
    render(
      <SetupNotice
        error={new ApiError("Cannot reach the QAgent API at http://x", 503)}
        baseUrl="http://x"
        configured={true}
      />,
    );
    expect(screen.getByText("Cannot reach the QAgent API")).toBeInTheDocument();
    expect(screen.getByText("docker compose up postgres redis api")).toBeInTheDocument();
    expect(screen.getByText("http://x")).toBeInTheDocument();
  });

  it("shows a generic error message for anything else, never a blank/zeroed dashboard", () => {
    render(
      <SetupNotice
        error={new ApiError("/api/v1/dashboard returned 500", 500)}
        baseUrl="http://x"
        configured={true}
      />,
    );
    expect(screen.getByText("The API returned an error")).toBeInTheDocument();
    expect(screen.getByText("/api/v1/dashboard returned 500")).toBeInTheDocument();
  });
});
