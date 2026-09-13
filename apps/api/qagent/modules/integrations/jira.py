"""Jira issue sync.

The shape generalizes directly from GitHub Issues sync
(``modules/integrations/github.py``): search-then-create by exact title match,
additive only, never edits or closes an issue a human already owns. Jira's
REST API (basic auth with an API token, and the Atlassian Document Format a
description has to be sent in) is the only genuinely new work.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


class JiraSyncError(RuntimeError):
    """A Jira API call failed outright (bad auth, unknown project, permissions)."""


@dataclass
class IssueResult:
    bug_title: str
    action: str  # "created" | "skipped_existing" | "dry_run"
    issue_key: str | None = None
    issue_url: str | None = None


def _text_to_adf(body: str) -> dict:
    """Jira Cloud's v3 API rejects a plain-string description; it wants the
    Atlassian Document Format. One paragraph node per line is enough fidelity
    for a generated bug report — this isn't rendering rich text, just avoiding
    a 400."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": line}]}
            if line
            else {"type": "paragraph", "content": []}
            for line in body.splitlines()
        ],
    }


def render_issue_body(*, case_name: str, endpoint: str | None, bug: dict) -> str:
    lines = [f"Severity: {bug.get('severity', 'unknown')}", f"Check: {case_name}"]
    if endpoint:
        lines.append(f"Endpoint: {endpoint}")
    lines += [
        "",
        "Expected:",
        bug.get("expected") or "-",
        "",
        "Actual:",
        bug.get("actual") or "-",
        "",
        "Root cause:",
        bug.get("root_cause") or "-",
        "",
        "Suggested fix:",
        bug.get("suggested_fix") or "-",
        "",
        "Filed automatically by QAgent.",
    ]
    return "\n".join(lines)


def find_existing_issue(client: httpx.Client, project_key: str, title: str) -> dict | None:
    """JQL text search is fuzzy, so results are filtered down to an exact
    summary match — the same dedupe contract as the GitHub integration."""
    escaped = title.replace('"', '\\"')
    jql = f'project = "{project_key}" AND summary ~ "{escaped}"'
    response = client.get("/rest/api/3/search", params={"jql": jql, "fields": "summary"})
    response.raise_for_status()
    for issue in response.json().get("issues", []):
        if issue.get("fields", {}).get("summary") == title:
            return issue
    return None


def create_issue(
    client: httpx.Client, project_key: str, *, title: str, body: str, issue_type: str = "Bug"
) -> dict:
    payload = {
        "fields": {
            "project": {"key": project_key},
            "summary": title,
            "description": _text_to_adf(body),
            "issuetype": {"name": issue_type},
        }
    }
    response = client.post("/rest/api/3/issue", json=payload)
    response.raise_for_status()
    return response.json()


def sync_bugs_to_jira(
    *,
    bugs: list[dict],
    base_url: str,
    project_key: str,
    email: str,
    api_token: str,
    issue_type: str = "Bug",
    dry_run: bool = False,
    timeout: float = 15.0,
    client: httpx.Client | None = None,
) -> list[IssueResult]:
    """File a Jira issue for each bug not already tracked by title.

    ``bugs`` are outcome dicts as written by ``qagent scan --json`` (see
    ``sync_bugs_to_github`` for the identical contract).

    ``client`` is an injection point for tests; production callers should leave
    it unset so a correctly configured client (base URL, basic auth) is built.
    """
    results: list[IssueResult] = []
    owned_client = client is None
    active_client = client or httpx.Client(
        base_url=base_url.rstrip("/"), auth=httpx.BasicAuth(email, api_token), timeout=timeout
    )
    browse_root = base_url.rstrip("/")

    try:
        for outcome in bugs:
            bug = outcome.get("bug") or {}
            title = bug.get("title") or outcome.get("name") or "QAgent defect"

            try:
                existing = find_existing_issue(active_client, project_key, title)
            except httpx.HTTPStatusError as exc:
                raise JiraSyncError(f"could not search issues: {exc}") from exc

            if existing:
                key = existing.get("key")
                results.append(
                    IssueResult(
                        bug_title=title,
                        action="skipped_existing",
                        issue_key=key,
                        issue_url=f"{browse_root}/browse/{key}" if key else None,
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
                created = create_issue(
                    active_client, project_key, title=title, body=body, issue_type=issue_type
                )
            except httpx.HTTPStatusError as exc:
                raise JiraSyncError(f"could not create issue '{title}': {exc}") from exc

            key = created.get("key")
            results.append(
                IssueResult(
                    bug_title=title,
                    action="created",
                    issue_key=key,
                    issue_url=f"{browse_root}/browse/{key}" if key else None,
                )
            )
    finally:
        if owned_client:
            active_client.close()

    return results
