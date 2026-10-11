"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { inputStyle, postAction } from "@/lib/actions";
import type { Environment } from "@/lib/types";

const buttonClass =
  "rounded px-3 py-2 text-sm font-semibold text-white disabled:opacity-60";

function ErrorText({ error }: { error: string | null }) {
  return error ? (
    <p className="text-xs" style={{ color: "var(--critical)" }}>
      {error}
    </p>
  ) : null;
}

/**
 * "Run QA" (CLAUDE.md section 24, step 5). Queues a scan and hands off to the
 * run page, which already polls a run to completion - the same hand-off the
 * verify button makes, so run progress is tracked in exactly one place.
 */
export function RunQaForm({
  projectId,
  environments,
}: {
  projectId: string;
  environments: Environment[];
}) {
  const router = useRouter();
  const [environmentId, setEnvironmentId] = useState(environments[0]?.id ?? "");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (environments.length === 0) {
    return (
      <p className="text-xs" style={{ color: "var(--text-secondary)" }}>
        Add an environment first: QAgent needs the address of a running copy of
        the application to test.
      </p>
    );
  }

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setPending(true);
    setError(null);
    const result = await postAction<{ id: string }>(`/api/projects/${projectId}/runs`, {
      environment_id: environmentId,
      trigger: "manual",
    });
    if (!result.ok) {
      setPending(false);
      setError(result.error);
      return;
    }
    router.push(`/runs/${result.data.id}`);
  }

  return (
    <form onSubmit={onSubmit} className="flex flex-wrap items-end gap-3">
      <label className="block min-w-[200px] flex-1 text-xs">
        Environment
        <select
          value={environmentId}
          onChange={(e) => setEnvironmentId(e.target.value)}
          className="mt-1 w-full rounded border px-3 py-2 text-sm"
          style={inputStyle}
        >
          {environments.map((env) => (
            <option key={env.id} value={env.id}>
              {env.name} — {env.base_url ?? env.mode}
            </option>
          ))}
        </select>
      </label>
      <button
        type="submit"
        disabled={pending}
        className={buttonClass}
        style={{ background: "var(--accent)" }}
      >
        {pending ? "Queuing…" : "Run QA"}
      </button>
      <div className="w-full">
        <ErrorText error={error} />
      </div>
    </form>
  );
}

/** Where a scan sends traffic. Credentials are deliberately not collected
 *  here: they belong in a secret store, referenced by name via the API. */
export function AddEnvironmentForm({ projectId }: { projectId: string }) {
  const router = useRouter();
  const [name, setName] = useState("staging");
  const [baseUrl, setBaseUrl] = useState("");
  const [openapiUrl, setOpenapiUrl] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setPending(true);
    setError(null);
    const result = await postAction(`/api/projects/${projectId}/environments`, {
      name: name.trim() || "default",
      base_url: baseUrl.trim(),
      openapi_url: openapiUrl.trim() || null,
    });
    setPending(false);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    setBaseUrl("");
    setOpenapiUrl("");
    router.refresh();
  }

  return (
    <form onSubmit={onSubmit} className="grid gap-3 sm:grid-cols-[1fr_2fr_2fr_auto] sm:items-end">
      <label className="block text-xs">
        Name
        <input
          required
          maxLength={100}
          value={name}
          onChange={(e) => setName(e.target.value)}
          className="mt-1 w-full rounded border px-3 py-2 text-sm"
          style={inputStyle}
        />
      </label>
      <label className="block text-xs">
        Base URL
        <input
          required
          type="url"
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          placeholder="https://staging.example.com"
          className="mt-1 w-full rounded border px-3 py-2 text-sm"
          style={inputStyle}
        />
      </label>
      <label className="block text-xs">
        OpenAPI URL <span style={{ color: "var(--text-muted)" }}>(optional)</span>
        <input
          type="url"
          value={openapiUrl}
          onChange={(e) => setOpenapiUrl(e.target.value)}
          placeholder="probed automatically"
          className="mt-1 w-full rounded border px-3 py-2 text-sm"
          style={inputStyle}
        />
      </label>
      <button
        type="submit"
        disabled={pending}
        className={buttonClass}
        style={{ background: "var(--accent)" }}
      >
        {pending ? "Adding…" : "Add"}
      </button>
      <div className="sm:col-span-4">
        <ErrorText error={error} />
      </div>
    </form>
  );
}

/** Clone + Project Analyst (CLAUDE.md section 6). Synchronous on the API, so
 *  the label says what is taking the time. */
export function ConnectRepoForm({
  projectId,
  repoUrl,
}: {
  projectId: string;
  repoUrl: string | null;
}) {
  const router = useRouter();
  const [url, setUrl] = useState(repoUrl ?? "");
  const [branch, setBranch] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setPending(true);
    setError(null);
    const result = await postAction(`/api/projects/${projectId}/connect`, {
      repo_url: url.trim(),
      branch: branch.trim() || null,
    });
    setPending(false);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    router.refresh();
  }

  return (
    <form onSubmit={onSubmit} className="grid gap-3 sm:grid-cols-[3fr_1fr_auto] sm:items-end">
      <label className="block text-xs">
        GitHub repository
        <input
          required
          type="url"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://github.com/owner/repo"
          className="mt-1 w-full rounded border px-3 py-2 text-sm"
          style={inputStyle}
        />
      </label>
      <label className="block text-xs">
        Branch <span style={{ color: "var(--text-muted)" }}>(optional)</span>
        <input
          value={branch}
          onChange={(e) => setBranch(e.target.value)}
          placeholder="default"
          className="mt-1 w-full rounded border px-3 py-2 text-sm"
          style={inputStyle}
        />
      </label>
      <button
        type="submit"
        disabled={pending}
        className={buttonClass}
        style={{ background: "var(--accent)" }}
      >
        {pending ? "Cloning and analyzing…" : repoUrl ? "Re-analyze" : "Connect"}
      </button>
      <div className="sm:col-span-3">
        <ErrorText error={error} />
      </div>
    </form>
  );
}
