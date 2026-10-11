import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  AddEnvironmentForm,
  ConnectRepoForm,
  RunQaForm,
} from "@/components/project-actions";
import type { Environment } from "@/lib/types";

const push = vi.fn();
const refresh = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, refresh }),
}));

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

function env(id: string, name: string): Environment {
  return {
    id,
    name,
    mode: "target_url",
    base_url: `https://${name}.example.com`,
    openapi_url: null,
    e2e_enabled: false,
    has_credentials: false,
  };
}

beforeEach(() => {
  push.mockClear();
  refresh.mockClear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("RunQaForm", () => {
  it("asks for an environment instead of offering a button that cannot work", () => {
    render(<RunQaForm projectId="p-1" environments={[]} />);

    expect(screen.queryByRole("button", { name: "Run QA" })).not.toBeInTheDocument();
    expect(screen.getByText(/Add an environment first/)).toBeInTheDocument();
  });

  it("queues a run against the chosen environment and opens the run page", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ id: "run-9" }, 202)));

    render(
      <RunQaForm projectId="p-1" environments={[env("e-1", "staging"), env("e-2", "prod")]} />,
    );
    await userEvent.selectOptions(screen.getByLabelText("Environment"), "e-2");
    await userEvent.click(screen.getByRole("button", { name: "Run QA" }));

    expect(fetch).toHaveBeenCalledWith(
      "/api/projects/p-1/runs",
      expect.objectContaining({
        body: JSON.stringify({ environment_id: "e-2", trigger: "manual" }),
      }),
    );
    expect(push).toHaveBeenCalledWith("/runs/run-9");
  });

  it("shows a rate-limit refusal instead of navigating", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ error: "rate limit exceeded" }, 429)),
    );

    render(<RunQaForm projectId="p-1" environments={[env("e-1", "staging")]} />);
    await userEvent.click(screen.getByRole("button", { name: "Run QA" }));

    expect(await screen.findByText("rate limit exceeded")).toBeInTheDocument();
    expect(push).not.toHaveBeenCalled();
  });
});

describe("AddEnvironmentForm", () => {
  it("posts the environment, leaves out an empty OpenAPI URL, and refreshes the page", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ id: "e-1" }, 201)));

    render(<AddEnvironmentForm projectId="p-1" />);
    await userEvent.type(screen.getByLabelText("Base URL"), "https://staging.example.com");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));

    expect(fetch).toHaveBeenCalledWith(
      "/api/projects/p-1/environments",
      expect.objectContaining({
        body: JSON.stringify({
          name: "staging",
          base_url: "https://staging.example.com",
          openapi_url: null,
        }),
      }),
    );
    expect(refresh).toHaveBeenCalled();
  });

  it("shows why the API refused, e.g. a member who is not an owner", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ error: "owner role required" }, 403)),
    );

    render(<AddEnvironmentForm projectId="p-1" />);
    await userEvent.type(screen.getByLabelText("Base URL"), "https://staging.example.com");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));

    expect(await screen.findByText("owner role required")).toBeInTheDocument();
    expect(refresh).not.toHaveBeenCalled();
  });
});

describe("ConnectRepoForm", () => {
  it("offers re-analysis when a repository is already connected", () => {
    render(<ConnectRepoForm projectId="p-1" repoUrl="https://github.com/acme/shop" />);

    expect(screen.getByLabelText("GitHub repository")).toHaveValue(
      "https://github.com/acme/shop",
    );
    expect(screen.getByRole("button", { name: "Re-analyze" })).toBeInTheDocument();
  });

  it("connects with an optional branch and refreshes the page", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ id: "p-1" })));

    render(<ConnectRepoForm projectId="p-1" repoUrl={null} />);
    await userEvent.type(screen.getByLabelText("GitHub repository"), "https://github.com/acme/shop");
    await userEvent.type(screen.getByLabelText(/Branch/), "develop");
    await userEvent.click(screen.getByRole("button", { name: "Connect" }));

    expect(fetch).toHaveBeenCalledWith(
      "/api/projects/p-1/connect",
      expect.objectContaining({
        body: JSON.stringify({ repo_url: "https://github.com/acme/shop", branch: "develop" }),
      }),
    );
    expect(refresh).toHaveBeenCalled();
  });
});
