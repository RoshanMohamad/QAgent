import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NewProjectForm } from "@/components/new-project-form";

const push = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
}));

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

describe("NewProjectForm", () => {
  beforeEach(() => {
    push.mockClear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("creates the project and opens it", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ id: "p-1" }, 201)));

    render(<NewProjectForm />);
    await userEvent.type(screen.getByLabelText("Project name"), "shop");
    await userEvent.click(screen.getByRole("button", { name: "Create project" }));

    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledWith(
      "/api/projects",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ name: "shop" }) }),
    );
    expect(push).toHaveBeenCalledWith("/projects/p-1");
  });

  it("connects the repository in the same submit when one is given", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(jsonResponse({ id: "p-1" }, 201))
        .mockResolvedValueOnce(jsonResponse({ id: "p-1", stack: {} })),
    );

    render(<NewProjectForm />);
    await userEvent.type(screen.getByLabelText("Project name"), "shop");
    await userEvent.type(
      screen.getByLabelText(/GitHub repository/),
      "https://github.com/acme/shop",
    );
    await userEvent.click(screen.getByRole("button", { name: "Create project" }));

    expect(fetch).toHaveBeenLastCalledWith(
      "/api/projects/p-1/connect",
      expect.objectContaining({
        body: JSON.stringify({ repo_url: "https://github.com/acme/shop" }),
      }),
    );
    expect(push).toHaveBeenCalledWith("/projects/p-1");
  });

  it("says the project exists and links to it when only the connect step fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(jsonResponse({ id: "p-1" }, 201))
        .mockResolvedValueOnce(jsonResponse({ error: "repository not found" }, 422)),
    );

    render(<NewProjectForm />);
    await userEvent.type(screen.getByLabelText("Project name"), "shop");
    await userEvent.type(
      screen.getByLabelText(/GitHub repository/),
      "https://github.com/acme/missing",
    );
    await userEvent.click(screen.getByRole("button", { name: "Create project" }));

    expect(await screen.findByText(/Project created, but .*repository not found/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the project" })).toHaveAttribute(
      "href",
      "/projects/p-1",
    );
    expect(push).not.toHaveBeenCalled();
  });

  it("shows the API's reason when creation fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ error: "Not signed in" }, 401)),
    );

    render(<NewProjectForm />);
    await userEvent.type(screen.getByLabelText("Project name"), "shop");
    await userEvent.click(screen.getByRole("button", { name: "Create project" }));

    expect(await screen.findByText("Not signed in")).toBeInTheDocument();
    expect(push).not.toHaveBeenCalled();
  });
});
