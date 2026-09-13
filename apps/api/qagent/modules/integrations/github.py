"""GitHub Issues sync (CLAUDE.md roadmap: issue tracker sync).

Turns a defect QAgent already found and triaged into a tracked issue, instead
of leaving it sitting in scan output someone has to remember to check.
Deliberately one-way and additive: this module creates issues, it never closes,
edits, or comments on ones a human already owns, and it never re-files a bug
that's already open — the dedupe check is a title search, which works because a
bug report's ``title`` is already specific and stable
(``modules/triage/agent.py``), not a generic "test failed".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


class GithubSyncError(RuntimeError):
    """A GitHub API call failed outright (bad token, repo not found, rate limit)."""


@dataclass
class IssueResult:
    bug_title: str
    action: str  # "created" | "skipped_existing" | "dry_run"
    issue_number: int | None = None
    issue_url: str | None = None


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def render_issue_body(*, case_name: str, endpoint: str | None, bug: dict) -> str:
    lines = [
        f"**Severity:** {bug.get('severity', 'unknown')}",
        "",
        f"**Check:** `{case_name}`",
    ]
    if endpoint:
        lines.append(f"**Endpoint:** `{endpoint}`")
    lines += [
        "",
        "**Expected**",
        bug.get("expected") or "-",
        "",
        "**Actual**",
        bug.get("actual") or "-",
        "",
        "**Root cause**",
        bug.get("root_cause") or "-",
        "",
        "**Suggested fix**",
        bug.get("suggested_fix") or "-",
        "",
        "_Filed automatically by QAgent._",
    ]
    return "\n".join(lines)


def find_existing_issue(client: httpx.Client, repo: str, title: str) -> dict | None:
    """Search open+closed issues for an exact title match, so a re-run of the same
    scan never files a duplicate for a defect already tracked."""
    query = f'repo:{repo} in:title is:issue "{title}"'
    response = client.get(f"{GITHUB_API}/search/issues", params={"q": query})
    response.raise_for_status()
    for item in response.json().get("items", []):
        if item.get("title") == title:
            return item
    return None


def create_issue(
    client: httpx.Client, repo: str, *, title: str, body: str, labels: list[str] | None = None
) -> dict:
    response = client.post(
        f"{GITHUB_API}/repos/{repo}/issues",
        json={"title": title, "body": body, "labels": labels or []},
    )
    response.raise_for_status()
    return response.json()


def sync_bugs_to_github(
    *,
    bugs: list[dict],
    repo: str,
    token: str,
    labels: list[str] | None = None,
    dry_run: bool = False,
    timeout: float = 15.0,
    client: httpx.Client | None = None,
) -> list[IssueResult]:
    """File a GitHub issue for each bug not already tracked by title.

    ``bugs`` are outcome dicts as written by ``qagent scan --json``: each needs a
    ``name``, optionally an ``endpoint``, and a ``bug`` sub-dict with
    title/severity/expected/actual/root_cause/suggested_fix.

    ``client`` is an injection point for tests; production callers should leave
    it unset so a correctly configured client (auth headers, timeout) is built.
    """
    results: list[IssueResult] = []
    owned_client = client is None
    active_client = client or httpx.Client(headers=_headers(token), timeout=timeout)

    try:
        for outcome in bugs:
            bug = outcome.get("bug") or {}
            title = bug.get("title") or outcome.get("name") or "QAgent defect"

            try:
                existing = find_existing_issue(active_client, repo, title)
            except httpx.HTTPStatusError as exc:
                raise GithubSyncError(f"could not search issues: {exc}") from exc

            if existing:
                results.append(
                    IssueResult(
                        bug_title=title,
                        action="skipped_existing",
                        issue_number=existing.get("number"),
                        issue_url=existing.get("html_url"),
                    )
                )
                continue

            if dry_run:
                results.append(IssueResult(bug_title=title, action="dry_run"))
                continue

            body = render_issue_body(
                case_name=outcome.get("name", ""), endpoint=outcome.get("endpoint"), bug=bug
            )
            try:
                created = create_issue(active_client, repo, title=title, body=body, labels=labels)
            except httpx.HTTPStatusError as exc:
                raise GithubSyncError(f"could not create issue '{title}': {exc}") from exc

            results.append(
                IssueResult(
                    bug_title=title,
                    action="created",
                    issue_number=created.get("number"),
                    issue_url=created.get("html_url"),
                )
            )
    finally:
        if owned_client:
            active_client.close()

    return results
