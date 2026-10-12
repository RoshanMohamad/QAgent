"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { inputStyle, postAction } from "@/lib/actions";

/**
 * Step 1-2 of the user journey (CLAUDE.md section 24): create a project and,
 * optionally, connect its GitHub, GitLab or Bitbucket repository in the same submit.
 *
 * Two requests, not one, because they are two API operations with different
 * permissions: any member can create a project, only an owner can make the
 * server clone a repository. If the second fails the project still exists, so
 * the error says so and links to it rather than implying nothing happened.
 */
export function NewProjectForm() {
  const router = useRouter();
  const [name, setName] = useState("");
  const [repoUrl, setRepoUrl] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [orphan, setOrphan] = useState<string | null>(null);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setPending(true);
    setError(null);
    setOrphan(null);

    const created = await postAction<{ id: string }>("/api/projects", { name: name.trim() });
    if (!created.ok) {
      setPending(false);
      setError(created.error);
      return;
    }

    const projectId = created.data.id;
    if (repoUrl.trim()) {
      const connected = await postAction(`/api/projects/${projectId}/connect`, {
        repo_url: repoUrl.trim(),
      });
      if (!connected.ok) {
        setPending(false);
        setOrphan(projectId);
        setError(`Project created, but the repository could not be connected: ${connected.error}`);
        return;
      }
    }

    router.push(`/projects/${projectId}`);
  }

  return (
    <form onSubmit={onSubmit} className="space-y-3">
      <label className="block text-xs">
        Project name
        <input
          required
          maxLength={200}
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="my-ecommerce-app"
          className="mt-1 w-full rounded border px-3 py-2 text-sm"
          style={inputStyle}
        />
      </label>
      <label className="block text-xs">
        Repository <span style={{ color: "var(--text-muted)" }}>(optional)</span>
        <input
          type="url"
          value={repoUrl}
          onChange={(e) => setRepoUrl(e.target.value)}
          placeholder="https://github.com/owner/repo, gitlab.com/..., bitbucket.org/..."
          className="mt-1 w-full rounded border px-3 py-2 text-sm"
          style={inputStyle}
        />
      </label>

      {error ? (
        <p className="text-xs" style={{ color: "var(--critical)" }}>
          {error}{" "}
          {orphan ? (
            <a href={`/projects/${orphan}`} className="underline">
              Open the project
            </a>
          ) : null}
        </p>
      ) : null}

      <button
        type="submit"
        disabled={pending}
        className="w-full rounded px-3 py-2 text-sm font-semibold text-white disabled:opacity-60"
        style={{ background: "var(--accent)" }}
      >
        {pending ? (repoUrl.trim() ? "Cloning and analyzing…" : "Creating…") : "Create project"}
      </button>
    </form>
  );
}
