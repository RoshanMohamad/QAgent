# ADR-0009: A connected GitHub repository is cloned host-pinned, read once, and deleted

**Status:** Accepted
**Date:** 2026-09-19

## Context

ADR-0006 settled what the Project Analyst may *do* with a checkout: read text, parse it
with non-executing parsers, never run the project's own tooling. It deliberately said
nothing about where the checkout comes from, so `qagent analyze` and
`POST /api/v1/projects/{id}/analyze` both took a local `repo_path`. CLAUDE.md section 6
describes the step before that one — a user pastes
`https://github.com/company/project` and "the system clones the repository into an
isolated environment" — and that step did not exist. Connecting a repository meant
cloning it by hand first, which is not a connection, and `Project.repo_url` was a label
nothing ever acted on.

`git clone` is not a neutral fetch. Given a URL it will happily use transports that
execute a command (`ext::sh -c ...`), read the local filesystem (`file://`), or open a
connection to whatever host the string names — which, from inside the API process, includes
every internal service and cloud metadata endpoint that process can reach. It will also
block forever prompting for credentials when there is no terminal attached. And a clone
of a private repository needs a token, which is exactly the kind of secret that leaks
through `ps`, a `.git/config` left on disk, or a git error message echoed into a log line
or an HTTP response.

## Decision

Cloning is a provisioning concern, so it lives next to the other one
(`modules/provisioning/clone.py`, beside `compose.py`) and is shaped the same way: a
context manager that acquires on enter and tears down on exit unconditionally. It owns
no analysis of its own — it hands `analyze_repository` a directory, so there is one
analyzer, not a second code path for remote repositories.

**The URL is validated before it is a URL.** `parse_repo_url` accepts only
`https://github.com/owner/repo` (optionally `.git`, optionally `www.`) or the
`owner/repo` shorthand, and returns a `RepoRef`. Nothing else in the module will build a
clone target, so holding a `RepoRef` is proof validation happened. Everything else —
other hosts, `github.com.evil.test`, `git@`/`ssh://`/`file://`/`ext::`, embedded
credentials, a query string, a raw IP — is refused rather than normalised. One host is a
narrower contract than "any git remote", and narrowing it is what removes SSRF,
command-executing transports and local-file reads in a single rule instead of three
partial defenses.

**The argv is hardened behind that check.** `protocol.ext.allow=never` and
`protocol.file.allow=never` hold even if a redirect or a submodule tries to reintroduce
a forbidden transport; `--no-recurse-submodules`, `--depth 1`, `--single-branch` and
`--no-tags` keep the fetch to the only thing that has a reader — the working tree at one
commit; `--` before the URL means a repository name can never be read as an option; and
`GIT_TERMINAL_PROMPT=0` turns a private repo without a usable token into an immediate,
legible failure instead of a process hung on a prompt until the timeout.

**A token never touches argv or the database.** It goes into a `.git-credentials` file in
the per-run temp directory, chmod 0600, that git is pointed at for the duration of the
clone and that is deleted the moment the clone returns — and again in a `finally`. The
remote URL git records carries no credential. Token-shaped text is stripped from git's
own output before that output reaches an exception, a log line or an HTTP response,
because git echoes the remote URL in most of its failure messages. The API model marks
the field `exclude`/`repr=False` so it cannot reach a traceback or a request dump either.
It is read from the request or `$GITHUB_TOKEN` — the contract `qagent report-issues`
already established — and stored nowhere.

**The checkout is deleted when analysis ends.** Nothing downstream reads source after
the analyzer has run: checks execute against a running application (ADR-0001), not
against files. A retained third-party checkout is standing risk — untrusted content
(ADR-0004) sitting on a disk the API process can read — in exchange for nothing.

## Consequences

- "Connect a repository" is now one call. `POST /api/v1/projects/{id}/connect` and
  `qagent connect --repo owner/repo` clone, analyze, persist `Project.stack`,
  `repo_url` and the branch, and return the commit SHA the analysis describes — so a
  stored analysis always names the commit it came from.
- Every `CloneError` is a 422, never a 500: a bad URL, a bad branch, a missing
  credential and a private repository are all facts about the caller's request.
- Analysis is a snapshot, not a subscription. Nothing re-clones when the repository
  changes; a push does not update `Project.stack`. That needs webhooks, which need a
  publicly reachable callback, and is deferred with the rest of Phase 5 rather than
  half-built here.
- Only github.com works. GitLab and Bitbucket — listed in CLAUDE.md section 5 — are one
  entry in the allowlist plus their URL shape each, but they are not free: each is a
  different host to trust, so each is a deliberate decision rather than a regex widening.
- The clone is synchronous, like `analyze` and `security/scan` beside it. A large
  repository holds a request thread for as long as the network takes; if that becomes a
  problem the fix is the queue that already exists for scans, not a different clone.
- Cloning is the one place QAgent fetches third-party content *before* any sandbox
  exists (ADR-0006), which is why the hardening lives in the clone itself rather than in
  a container around it.
