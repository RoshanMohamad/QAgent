"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

type Mode = "login" | "register";

const inputStyle = {
  background: "var(--surface-2)",
  borderColor: "var(--border)",
  color: "var(--text-primary)",
};

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<Mode>("login");
  const [orgName, setOrgName] = useState("");
  const [orgSlug, setOrgSlug] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setPending(true);

    const path = mode === "login" ? "/api/login" : "/api/register";
    const body =
      mode === "login"
        ? { org_slug: orgSlug, email, password }
        : { org_name: orgName, org_slug: orgSlug, email, password };

    try {
      const response = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error ?? "Something went wrong");
      }
      router.push("/");
      router.refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Something went wrong");
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="mx-auto max-w-sm">
      <div
        className="rounded-lg border px-6 py-6"
        style={{ background: "var(--surface-1)", borderColor: "var(--border)" }}
      >
        <div className="mb-5 flex gap-4 text-sm">
          <button
            type="button"
            onClick={() => setMode("login")}
            className="font-semibold"
            style={{
              color: mode === "login" ? "var(--text-primary)" : "var(--text-muted)",
              borderBottom: mode === "login" ? "2px solid var(--accent)" : "2px solid transparent",
              paddingBottom: 4,
            }}
          >
            Sign in
          </button>
          <button
            type="button"
            onClick={() => setMode("register")}
            className="font-semibold"
            style={{
              color: mode === "register" ? "var(--text-primary)" : "var(--text-muted)",
              borderBottom:
                mode === "register" ? "2px solid var(--accent)" : "2px solid transparent",
              paddingBottom: 4,
            }}
          >
            Create organization
          </button>
        </div>

        <form onSubmit={onSubmit} className="space-y-3">
          {mode === "register" ? (
            <label className="block text-xs">
              Organization name
              <input
                required
                value={orgName}
                onChange={(e) => setOrgName(e.target.value)}
                className="mt-1 w-full rounded border px-3 py-2 text-sm"
                style={inputStyle}
              />
            </label>
          ) : null}

          <label className="block text-xs">
            Organization slug
            <input
              required
              pattern="[a-z0-9-]{2,100}"
              value={orgSlug}
              onChange={(e) => setOrgSlug(e.target.value)}
              placeholder="acme-corp"
              className="mt-1 w-full rounded border px-3 py-2 text-sm"
              style={inputStyle}
            />
          </label>

          <label className="block text-xs">
            Email
            <input
              required
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="mt-1 w-full rounded border px-3 py-2 text-sm"
              style={inputStyle}
            />
          </label>

          <label className="block text-xs">
            Password
            <input
              required
              type="password"
              minLength={8}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mt-1 w-full rounded border px-3 py-2 text-sm"
              style={inputStyle}
            />
          </label>

          {error ? (
            <p className="text-xs" style={{ color: "var(--critical)" }}>
              {error}
            </p>
          ) : null}

          <button
            type="submit"
            disabled={pending}
            className="w-full rounded px-3 py-2 text-sm font-semibold text-white disabled:opacity-60"
            style={{ background: "var(--accent)" }}
          >
            {pending
              ? "Please wait..."
              : mode === "login"
                ? "Sign in"
                : "Create organization"}
          </button>
        </form>
      </div>
    </div>
  );
}
