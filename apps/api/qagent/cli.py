"""The qagent command line (CLI surface from CLAUDE.md section 19).

Runs the full pipeline without a database, a queue or a dashboard, so the product can
be demonstrated and evaluated from a single command.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from qagent.config import get_settings
from qagent.modules.llm.client import LlmClient
from qagent.pipeline import PipelineResult, run_pipeline

app = typer.Typer(help="QAgent - autonomous API quality checks.", no_args_is_help=True)
console = Console()

_STATUS_STYLE = {"passed": "green", "failed": "red", "error": "yellow"}
_CLASS_STYLE = {
    "real_bug": "bold red",
    "flaky_test": "yellow",
    "environment": "cyan",
    "network": "cyan",
    "dependency": "cyan",
    "test_data": "magenta",
    "bad_assertion": "blue",
    "unknown": "dim",
}


def _render(result: PipelineResult, *, verbose: bool) -> None:
    summary = result.summary()

    if result.errors:
        for error in result.errors:
            console.print(f"[red]error[/red] {error}")

    console.print(
        Panel(
            f"[bold]{summary['base_url']}[/bold]\n"
            f"spec: {summary['spec_url'] or 'not found'}\n"
            f"{summary['endpoints']} endpoints discovered, "
            f"{summary['total']} checks executed in {summary['duration_s']}s",
            title="QAgent scan",
            border_style="blue",
        )
    )

    if not result.outcomes:
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("result", width=8)
    table.add_column("check", overflow="fold")
    table.add_column("classification", width=16)
    table.add_column("ms", justify="right", width=6)

    for outcome in result.outcomes:
        if outcome.status == "passed" and not verbose:
            continue
        klass = outcome.verdict["failure_class"] if outcome.verdict else ""
        table.add_row(
            f"[{_STATUS_STYLE.get(outcome.status, 'white')}]{outcome.status}[/]",
            outcome.name,
            f"[{_CLASS_STYLE.get(klass, 'white')}]{klass}[/]" if klass else "",
            str(outcome.duration_ms),
        )

    if table.row_count:
        console.print(table)

    console.print(
        f"\n[green]{summary['passed']} passed[/green]  "
        f"[red]{summary['failed']} failed[/red]  "
        f"[yellow]{summary['errored']} errored[/yellow]"
    )

    if summary["classifications"]:
        console.print("\n[bold]Failure analysis[/bold]")
        for name, count in sorted(summary["classifications"].items(), key=lambda kv: -kv[1]):
            console.print(f"  {count:>3}  [{_CLASS_STYLE.get(name, 'white')}]{name}[/]")

    for outcome in result.bugs:
        bug = outcome.bug or {}
        console.print(
            Panel(
                f"[bold]{bug.get('title')}[/bold]\n\n"
                f"severity   {bug.get('severity')}\n"
                f"expected   {bug.get('expected')}\n"
                f"actual     {bug.get('actual')}\n\n"
                f"root cause {bug.get('root_cause')}\n\n"
                f"fix        {bug.get('suggested_fix')}",
                border_style="red",
                title="defect",
            )
        )

    llm = summary.get("llm") or {}
    if llm.get("calls"):
        console.print(
            f"\n[dim]llm: {llm['calls']} calls, {llm['tokens']} tokens, ${llm['usd']:.4f}[/dim]"
        )


@app.command()
def scan(
    url: str = typer.Option(..., "--url", "-u", help="Base URL of the running application."),
    spec: str | None = typer.Option(None, "--spec", "-s", help="Explicit OpenAPI document URL."),
    repo: Path | None = typer.Option(
        None,
        "--repo",
        help="Repo source root to statically parse routes from if no OpenAPI doc is found.",
    ),
    header: list[str] = typer.Option(
        [], "--header", "-H", help="Auth header, e.g. 'Authorization: Bearer x'."
    ),
    max_cases: int | None = typer.Option(None, "--max-cases", help="Cap generated checks."),
    timeout: float = typer.Option(30.0, "--timeout", help="Per-request timeout in seconds."),
    output: Path | None = typer.Option(None, "--json", help="Write the full result as JSON."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show passing checks too."),
    fail_on_bug: bool = typer.Option(
        True, "--fail-on-bug/--no-fail-on-bug", help="Exit non-zero when a defect is found."
    ),
) -> None:
    """Discover, generate, execute and triage API checks against a running app."""
    auth_headers: dict[str, str] = {}
    for item in header:
        if ":" not in item:
            console.print(f"[red]ignoring malformed header:[/red] {item}")
            continue
        name, _, value = item.partition(":")
        auth_headers[name.strip()] = value.strip()

    settings = get_settings()
    result = run_pipeline(
        base_url=url,
        openapi_url=spec,
        repo_path=repo,
        auth_headers=auth_headers,
        max_cases=max_cases,
        timeout_seconds=timeout,
        allow_private=not settings.is_production,
        allowlist=settings.egress_allowlist,
        llm=LlmClient.from_settings(settings),
    )

    _render(result, verbose=verbose)

    if output:
        _write_json_report(result, output)

    if result.errors and not result.outcomes:
        raise typer.Exit(code=2)
    if fail_on_bug and result.bugs:
        raise typer.Exit(code=1)


def _write_json_report(result: PipelineResult, output: Path) -> None:
    payload = {
        "summary": result.summary(),
        "outcomes": [
            {
                "name": o.name,
                "kind": o.kind,
                "endpoint": o.endpoint_key,
                "status": o.status,
                "duration_ms": o.duration_ms,
                "request": o.request,
                "response": {k: v for k, v in o.response.items() if k != "body_text"},
                "assertions": o.assertions,
                "failure_message": o.failure_message,
                "verdict": o.verdict,
                "bug": o.bug,
            }
            for o in result.outcomes
        ],
    }
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    console.print(f"\n[dim]wrote {output}[/dim]")


@app.command("scan-repo")
def scan_repo(
    compose_file: Path = typer.Option(
        Path("docker-compose.yml"), "--compose", "-c", help="Path to the repo's compose file."
    ),
    service: str = typer.Option(
        ..., "--service", help="Compose service name that exposes the app under test."
    ),
    port: int = typer.Option(..., "--port", help="Container port that service listens on."),
    spec: str | None = typer.Option(None, "--spec", "-s", help="Explicit OpenAPI document URL."),
    repo: Path | None = typer.Option(
        None,
        "--repo",
        help="Repo source root for route-parsing fallback (default: the compose file's directory).",
    ),
    health_path: str = typer.Option(
        "/", "--health-path", help="Path polled until the service answers with < 500."
    ),
    startup_timeout: float = typer.Option(
        120.0, "--startup-timeout", help="Seconds to wait for the stack to become healthy."
    ),
    max_cases: int | None = typer.Option(None, "--max-cases", help="Cap generated checks."),
    output: Path | None = typer.Option(None, "--json", help="Write the full result as JSON."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show passing checks too."),
    fail_on_bug: bool = typer.Option(
        True, "--fail-on-bug/--no-fail-on-bug", help="Exit non-zero when a defect is found."
    ),
) -> None:
    """Bring a repository's own docker-compose stack up, scan it, tear it down (ADR-0001)."""
    from qagent.modules.provisioning.compose import ComposeError, run_pipeline_from_compose

    if not compose_file.exists():
        console.print(f"[red]compose file not found:[/red] {compose_file}")
        raise typer.Exit(code=2)

    settings = get_settings()
    console.print(f"[dim]bringing up {service} from {compose_file} ...[/dim]")
    try:
        result = run_pipeline_from_compose(
            compose_file=compose_file,
            target_service=service,
            target_port=port,
            health_path=health_path,
            startup_timeout_seconds=startup_timeout,
            openapi_url=spec,
            repo_path=repo or compose_file.resolve().parent,
            max_cases=max_cases,
            allowlist=settings.egress_allowlist,
            llm=LlmClient.from_settings(settings),
        )
    except ComposeError as exc:
        console.print(f"[red]compose error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    _render(result, verbose=verbose)

    if output:
        _write_json_report(result, output)

    if result.errors and not result.outcomes:
        raise typer.Exit(code=2)
    if fail_on_bug and result.bugs:
        raise typer.Exit(code=1)


def _render_browser_result(result: Any, *, title: str) -> None:
    summary = result.summary()
    console.print(
        Panel(
            f"[bold]{summary['base_url']}[/bold]\n"
            f"{summary['total']} pages checked in {summary['duration_s']}s",
            title=title,
            border_style="blue",
        )
    )

    table = Table(show_header=True, header_style="bold")
    table.add_column("result", width=8)
    table.add_column("url", overflow="fold")
    table.add_column("http", width=5)
    table.add_column("ms", justify="right", width=6)
    table.add_column("issue", overflow="fold")

    for check in result.checks:
        table.add_row(
            f"[{_STATUS_STYLE.get(check.status, 'white')}]{check.status}[/]",
            check.url,
            str(check.http_status or "-"),
            str(check.load_time_ms),
            check.failure_message or "",
        )
    console.print(table)
    console.print(
        f"\n[green]{summary['passed']} passed[/green]  [red]{summary['failed']} failed[/red]"
    )


def _write_browser_json(result: Any, output: Path) -> None:
    payload = {
        "summary": result.summary(),
        "checks": [
            {
                "url": c.url,
                "status": c.status,
                "http_status": c.http_status,
                "load_time_ms": c.load_time_ms,
                "console_errors": c.console_errors,
                "page_errors": c.page_errors,
                "failure_message": c.failure_message,
            }
            for c in result.checks
        ],
    }
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    console.print(f"\n[dim]wrote {output}[/dim]")


def _import_browser_runner() -> Any:
    try:
        from qagent.modules.browser.runner import run_browser_checks
    except ModuleNotFoundError as exc:
        console.print(
            "[red]playwright is not installed.[/red] Run "
            "[bold]pip install qagent[e2e] && playwright install chromium[/bold]."
        )
        raise typer.Exit(code=2) from exc
    return run_browser_checks


@app.command("scan-ui")
def scan_ui(
    url: str = typer.Option(..., "--url", "-u", help="Base URL of the running frontend."),
    route: list[str] = typer.Option(
        [], "--route", "-r", help="Additional path to check, e.g. '/login'. Repeatable."
    ),
    timeout: float = typer.Option(
        15.0, "--timeout", help="Per-page navigation timeout in seconds."
    ),
    headed: bool = typer.Option(False, "--headed", help="Show the browser instead of headless."),
    output: Path | None = typer.Option(None, "--json", help="Write the full result as JSON."),
    fail_on_defect: bool = typer.Option(
        True, "--fail-on-defect/--no-fail-on-defect", help="Exit non-zero when a page check fails."
    ),
) -> None:
    """Browser E2E: load a page and each --route, and flag JS errors or a dead page (ADR-0002).

    No clicking, no generated selectors — those come with the Explorer Agent, once
    there's a state graph to explore. This checks the one thing a browser can see
    that an HTTP client can't: whether the page actually renders clean.
    """
    run_browser_checks = _import_browser_runner()
    result = run_browser_checks(
        base_url=url, routes=route, timeout_seconds=timeout, headless=not headed
    )
    _render_browser_result(result, title="QAgent scan-ui")

    if output:
        _write_browser_json(result, output)

    if fail_on_defect and result.failed:
        raise typer.Exit(code=1)


@app.command()
def explore(
    url: str = typer.Option(..., "--url", "-u", help="Base URL to start crawling from."),
    max_pages: int = typer.Option(
        25, "--max-pages", help="Stop after discovering this many pages."
    ),
    max_depth: int = typer.Option(
        3, "--max-depth", help="Stop following links beyond this depth."
    ),
    timeout: float = typer.Option(
        15.0, "--timeout", help="Per-page navigation timeout in seconds."
    ),
    headed: bool = typer.Option(False, "--headed", help="Show the browser instead of headless."),
    check: bool = typer.Option(
        False, "--check", help="Also run scan-ui-style checks against every discovered page."
    ),
    output: Path | None = typer.Option(None, "--json", help="Write the state graph as JSON."),
    fail_on_defect: bool = typer.Option(
        True, "--fail-on-defect/--no-fail-on-defect", help="With --check, exit non-zero on failure."
    ),
) -> None:
    """Explorer agent (CLAUDE.md §8-9): crawl same-origin links and build a state graph.

    MVP scope: link discovery only — no form filling, no clicking, no inferred
    actions. What it buys the pipeline: pages nobody listed with --route. Add
    --check to run scan-ui's page-load checks against everything it finds.
    """
    try:
        from qagent.modules.explorer.crawler import explore as run_explore
    except ModuleNotFoundError as exc:
        console.print(
            "[red]playwright is not installed.[/red] Run "
            "[bold]pip install qagent[e2e] && playwright install chromium[/bold]."
        )
        raise typer.Exit(code=2) from exc

    graph = run_explore(
        base_url=url,
        max_pages=max_pages,
        max_depth=max_depth,
        timeout_seconds=timeout,
        headless=not headed,
    )

    console.print(
        Panel(
            f"[bold]{graph.root}[/bold]\n"
            f"{len(graph.nodes)} page(s) discovered, {len(graph.edges)} link(s) followed",
            title="QAgent explore",
            border_style="blue",
        )
    )

    table = Table(show_header=True, header_style="bold")
    table.add_column("depth", width=5, justify="right")
    table.add_column("url", overflow="fold")
    table.add_column("title", overflow="fold")
    table.add_column("links", justify="right", width=6)
    for node in sorted(graph.nodes.values(), key=lambda n: (n.depth, n.url)):
        table.add_row(str(node.depth), node.url, node.title or "", str(node.link_count))
    console.print(table)

    if output:
        output.write_text(json.dumps(graph.to_dict(), indent=2), encoding="utf-8")
        console.print(f"\n[dim]wrote {output}[/dim]")

    if not check:
        return

    run_browser_checks = _import_browser_runner()
    result = run_browser_checks(
        base_url=graph.root, routes=graph.routes, timeout_seconds=timeout, headless=not headed
    )
    console.print()
    _render_browser_result(result, title="QAgent explore --check")

    if fail_on_defect and result.failed:
        raise typer.Exit(code=1)


@app.command()
def endpoints(
    url: str = typer.Option(..., "--url", "-u"),
    spec: str | None = typer.Option(None, "--spec", "-s"),
    repo: Path | None = typer.Option(
        None, "--repo", help="Repo source root to statically parse routes from as a fallback."
    ),
) -> None:
    """List discovered endpoints and their risk scores, without running anything."""
    from qagent.pipeline import discover

    found, source = discover(url, spec, repo)
    if not found:
        console.print("[red]no OpenAPI document found[/red]")
        raise typer.Exit(code=2)

    console.print(f"[dim]{source}[/dim]")
    table = Table(show_header=True, header_style="bold")
    table.add_column("risk", justify="right", width=6)
    table.add_column("method", width=7)
    table.add_column("path", overflow="fold")
    table.add_column("auth", width=5)

    for endpoint in found:
        colour = (
            "red"
            if endpoint.risk_score >= 0.6
            else "yellow"
            if endpoint.risk_score >= 0.35
            else "green"
        )
        table.add_row(
            f"[{colour}]{endpoint.risk_score:.2f}[/]",
            endpoint.method,
            endpoint.path,
            "yes" if endpoint.requires_auth else "-",
        )
    console.print(table)


@app.command()
def evaluate(
    fixtures: Path = typer.Option(
        Path("packages/fixtures"), "--fixtures", help="Fixture directory."
    ),
    url: str = typer.Option(..., "--url", "-u", help="Base URL of the running fixture app."),
    name: str = typer.Option("buggy-shop", "--name", help="Fixture to evaluate against."),
) -> None:
    """Score the pipeline against a fixture with known seeded defects (ADR-0003)."""
    from qagent.eval.harness import run_evaluation

    report = run_evaluation(fixtures_dir=fixtures, fixture_name=name, base_url=url)
    console.print(Panel(report.render(), title="evaluation", border_style="magenta"))
    if report.false_positive_rate > 0.25:
        console.print("[red]false positive rate above threshold[/red]")
        raise typer.Exit(code=1)


@app.command("report-issues")
def report_issues(
    report_path: Path = typer.Option(
        ..., "--json", "-j", help="A qagent scan --json (or scan-repo --json) output file."
    ),
    repo: str = typer.Option(..., "--repo", help="GitHub repo to file issues against, owner/name."),
    token: str | None = typer.Option(
        None, "--token", help="GitHub token; defaults to $GITHUB_TOKEN."
    ),
    label: list[str] = typer.Option([], "--label", help="Label to apply. Repeatable."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be filed without calling GitHub."
    ),
) -> None:
    """Issue tracker sync: file a GitHub issue for each real defect in a scan report.

    Additive only — never edits, closes or comments on an existing issue, and
    skips (rather than duplicates) any bug already tracked by title.
    """
    import os

    from qagent.modules.integrations.github import GithubSyncError, sync_bugs_to_github

    resolved_token = token or os.environ.get("GITHUB_TOKEN")
    if not resolved_token and not dry_run:
        console.print(
            "[red]no GitHub token.[/red] Pass --token, set $GITHUB_TOKEN, or use --dry-run."
        )
        raise typer.Exit(code=2)

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    bugs = [o for o in payload.get("outcomes", []) if o.get("bug")]

    if not bugs:
        console.print("[green]no defects in this report — nothing to file.[/green]")
        return

    try:
        results = sync_bugs_to_github(
            bugs=bugs, repo=repo, token=resolved_token or "", labels=label, dry_run=dry_run
        )
    except GithubSyncError as exc:
        console.print(f"[red]github sync error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    table = Table(show_header=True, header_style="bold")
    table.add_column("action", width=16)
    table.add_column("title", overflow="fold")
    table.add_column("issue")

    style_by_action = {"created": "green", "skipped_existing": "yellow", "dry_run": "dim"}
    for r in results:
        style = style_by_action.get(r.action, "white")
        issue_ref = r.issue_url or (f"#{r.issue_number}" if r.issue_number else "")
        table.add_row(f"[{style}]{r.action}[/]", r.bug_title, issue_ref)
    console.print(table)

    created = sum(1 for r in results if r.action == "created")
    skipped = sum(1 for r in results if r.action == "skipped_existing")
    console.print(f"\n[green]{created} created[/green]  [yellow]{skipped} already tracked[/yellow]")


@app.command("report-issues-jira")
def report_issues_jira(
    report_path: Path = typer.Option(
        ..., "--json", "-j", help="A qagent scan --json (or scan-repo --json) output file."
    ),
    base_url: str = typer.Option(..., "--base-url", help="Jira site, e.g. https://org.atlassian.net."),
    project: str = typer.Option(..., "--project", help="Jira project key, e.g. QA."),
    email: str | None = typer.Option(
        None, "--email", help="Jira account email; defaults to $JIRA_EMAIL."
    ),
    token: str | None = typer.Option(
        None, "--token", help="Jira API token; defaults to $JIRA_API_TOKEN."
    ),
    issue_type: str = typer.Option("Bug", "--issue-type", help="Jira issue type to create."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be filed without calling Jira."
    ),
) -> None:
    """Issue tracker sync: file a Jira issue for each real defect in a scan report.

    Same contract as report-issues (GitHub): additive only, skips a bug already
    tracked by exact title match instead of duplicating it.
    """
    import os

    from qagent.modules.integrations.jira import JiraSyncError, sync_bugs_to_jira

    resolved_email = email or os.environ.get("JIRA_EMAIL")
    resolved_token = token or os.environ.get("JIRA_API_TOKEN")
    if not dry_run and not (resolved_email and resolved_token):
        console.print(
            "[red]missing Jira credentials.[/red] Pass --email/--token, set "
            "$JIRA_EMAIL/$JIRA_API_TOKEN, or use --dry-run."
        )
        raise typer.Exit(code=2)

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    bugs = [o for o in payload.get("outcomes", []) if o.get("bug")]

    if not bugs:
        console.print("[green]no defects in this report — nothing to file.[/green]")
        return

    try:
        results = sync_bugs_to_jira(
            bugs=bugs,
            base_url=base_url,
            project_key=project,
            email=resolved_email or "",
            api_token=resolved_token or "",
            issue_type=issue_type,
            dry_run=dry_run,
        )
    except JiraSyncError as exc:
        console.print(f"[red]jira sync error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    table = Table(show_header=True, header_style="bold")
    table.add_column("action", width=16)
    table.add_column("title", overflow="fold")
    table.add_column("issue")

    style_by_action = {"created": "green", "skipped_existing": "yellow", "dry_run": "dim"}
    for r in results:
        style = style_by_action.get(r.action, "white")
        issue_ref = r.issue_url or (r.issue_key or "")
        table.add_row(f"[{style}]{r.action}[/]", r.bug_title, issue_ref)
    console.print(table)

    created = sum(1 for r in results if r.action == "created")
    skipped = sum(1 for r in results if r.action == "skipped_existing")
    console.print(f"\n[green]{created} created[/green]  [yellow]{skipped} already tracked[/yellow]")


@app.command("heal-selectors")
def heal_selectors(
    url: str = typer.Option(..., "--url", "-u", help="Page to look for replacements on."),
    selector: list[str] = typer.Option(
        ..., "--selector", "-s", help="A selector that stopped matching. Repeatable."
    ),
    threshold: float = typer.Option(
        0.7, "--threshold", help="Minimum confidence to propose a replacement."
    ),
    timeout: float = typer.Option(15.0, "--timeout", help="Navigation timeout in seconds."),
    headed: bool = typer.Option(False, "--headed", help="Show the browser instead of headless."),
    output: Path | None = typer.Option(None, "--json", help="Write proposals as JSON."),
) -> None:
    """Self-healing selectors (CLAUDE.md §11): propose replacements, never apply them.

    Scores every element on the page against each selector's own semantics (id,
    data-testid, aria-label, text, ...) rather than DOM position, so a selector
    that moved but still means the same thing scores high. Every proposal needs
    a human to actually edit the test — this command only ever prints them.
    """
    try:
        from qagent.modules.browser.healing import find_replacements
    except ModuleNotFoundError as exc:
        console.print(
            "[red]playwright is not installed.[/red] Run "
            "[bold]pip install qagent[e2e] && playwright install chromium[/bold]."
        )
        raise typer.Exit(code=2) from exc

    proposals = find_replacements(
        base_url=url,
        old_selectors=selector,
        threshold=threshold,
        timeout_seconds=timeout,
        headless=not headed,
    )

    table = Table(show_header=True, header_style="bold")
    table.add_column("old selector", overflow="fold")
    table.add_column("candidate", overflow="fold")
    table.add_column("confidence", justify="right", width=10)

    for old_selector, proposal in proposals.items():
        if proposal is None:
            table.add_row(old_selector, "[dim]no confident match[/dim]", "-")
        else:
            table.add_row(old_selector, proposal.new_selector, f"{proposal.confidence:.0%}")
    console.print(table)
    console.print("\n[dim]Review and apply manually — QAgent never rewrites a test file.[/dim]")

    if output:
        payload = {
            old: (
                {
                    "new_selector": p.new_selector,
                    "confidence": p.confidence,
                    "requires_approval": p.requires_approval,
                }
                if p
                else None
            )
            for old, p in proposals.items()
        }
        output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        console.print(f"[dim]wrote {output}[/dim]")


_FINDING_SEVERITY_STYLE = {
    "critical": "bold red",
    "high": "red",
    "medium": "yellow",
    "low": "cyan",
    "info": "dim",
}
_SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}


@app.command()
def security(
    repo: Path = typer.Option(..., "--repo", "-r", help="Path to the repository to scan."),
    config: str = typer.Option(
        "auto", "--config", help="Semgrep config: 'auto', a ruleset name, or a local rules file."
    ),
    timeout: float = typer.Option(300.0, "--timeout", help="Scan timeout in seconds."),
    fail_on: str = typer.Option(
        "high", "--fail-on", help="Minimum severity that exits non-zero: critical|high|medium|low."
    ),
    output: Path | None = typer.Option(None, "--json", help="Write findings as JSON."),
) -> None:
    """Static analysis via Semgrep (CLAUDE.md section 16): `qagent security`.

    Parses source, never executes it, so this needs none of the sandboxing the
    runner/browser/explorer commands require for a live target. Findings are
    scored against the same severity scale as everything else QAgent reports
    (critical/high/medium/low/info), with SQL-injection- and access-control-shaped
    findings promoted to critical regardless of Semgrep's own severity label.
    """
    from qagent.modules.security.semgrep import SemgrepError, SemgrepUnavailable, run_semgrep

    try:
        result = run_semgrep(repo, config=config, timeout_seconds=timeout)
    except SemgrepUnavailable as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    except SemgrepError as exc:
        console.print(f"[red]semgrep error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    counts = result.counts_by_severity()
    console.print(
        Panel(
            f"[bold]{result.root}[/bold]\n{len(result.findings)} finding(s)",
            title="QAgent security",
            border_style="blue",
        )
    )

    table = Table(show_header=True, header_style="bold")
    table.add_column("severity", width=10)
    table.add_column("rule", overflow="fold")
    table.add_column("location", overflow="fold")
    table.add_column("message", overflow="fold")
    for finding in sorted(
        result.findings, key=lambda f: _SEVERITY_RANK.get(f.severity, 0), reverse=True
    ):
        style = _FINDING_SEVERITY_STYLE.get(finding.severity, "")
        table.add_row(
            f"[{style}]{finding.severity}[/{style}]" if style else finding.severity,
            finding.rule_id,
            f"{finding.path}:{finding.line}",
            finding.message,
        )
    console.print(table)

    if result.scan_errors:
        console.print(f"\n[yellow]{len(result.scan_errors)} file(s) could not be scanned.[/yellow]")

    if output:
        payload = {
            "root": result.root,
            "findings": [
                {
                    "rule_id": f.rule_id,
                    "title": f.title,
                    "severity": f.severity,
                    "path": f.path,
                    "line": f.line,
                    "message": f.message,
                    "confidence": f.confidence,
                    "cwe": f.cwe,
                    "owasp": f.owasp,
                }
                for f in result.findings
            ],
            "by_severity": counts,
            "scan_errors": result.scan_errors,
        }
        output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        console.print(f"\n[dim]wrote {output}[/dim]")

    threshold = _SEVERITY_RANK.get(fail_on, 3)
    if any(_SEVERITY_RANK.get(sev, 0) >= threshold and n for sev, n in counts.items()):
        raise typer.Exit(code=1)


def _parse_vus_levels(raw: str) -> list[int]:
    try:
        return [int(v.strip()) for v in raw.split(",") if v.strip()]
    except ValueError as exc:
        raise typer.BadParameter(
            "expected a comma-separated list of integers, e.g. 100,500,1000"
        ) from exc


@app.command()
def perf(
    url: str = typer.Option(..., "--url", "-u", help="Base URL to load test."),
    vus: str = typer.Option(
        "100,500,1000,5000", "--vus", help="Comma-separated VU levels, one scenario each."
    ),
    duration: float = typer.Option(30.0, "--duration", help="Seconds per scenario."),
    path: list[str] = typer.Option(
        ["/"], "--path", "-p", help="GET path to hit, relative to --url. Repeatable."
    ),
    max_failed_rate: float = typer.Option(
        0.01, "--max-failed-rate", help="Fail the gate above this fraction of failed requests."
    ),
    max_p95_ms: float = typer.Option(
        1000.0, "--max-p95-ms", help="Fail the gate above this p95 latency in milliseconds."
    ),
    output: Path | None = typer.Option(None, "--json", help="Write all scenario results as JSON."),
) -> None:
    """Load testing via k6 (CLAUDE.md section 17): `qagent perf`.

    Runs one scenario per VU level, sequentially, so degradation as load rises is
    visible scenario-to-scenario rather than confounded by simultaneous runs
    against the same target. Only GET requests against --path are ever sent -
    never a discovered POST/PUT/DELETE endpoint - since guessing at a destructive
    path under sustained concurrent load needs a human to opt in, not an inference.

    Measures latency, throughput and error rate from the client side. CPU/memory
    (also named in CLAUDE.md section 17) need an agent on the target host, which
    a load generator has no way to provide - out of scope here for that reason,
    not from an oversight.
    """
    from qagent.modules.performance.k6 import K6Error, K6Unavailable, run_load_test

    vus_levels = _parse_vus_levels(vus)

    try:
        result = run_load_test(url, vus_levels=vus_levels, duration_seconds=duration, paths=path)
    except K6Unavailable as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    except K6Error as exc:
        console.print(f"[red]k6 error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    console.print(
        Panel(f"[bold]{result.base_url}[/bold]", title="QAgent perf", border_style="blue")
    )

    table = Table(show_header=True, header_style="bold")
    table.add_column("vus", justify="right", width=8)
    table.add_column("req/s", justify="right", width=10)
    table.add_column("failed", justify="right", width=10)
    table.add_column("p95", justify="right", width=10)
    table.add_column("p99", justify="right", width=10)
    table.add_column("result", width=8)

    all_passed = True
    for scenario in result.scenarios:
        passed = scenario.passes(max_failed_rate=max_failed_rate, max_p95_ms=max_p95_ms)
        all_passed = all_passed and passed
        table.add_row(
            str(scenario.vus),
            f"{scenario.requests_per_s:.1f}",
            f"{scenario.failed_rate:.2%}",
            f"{scenario.latency_p95_ms:.0f}ms",
            f"{scenario.latency_p99_ms:.0f}ms",
            "[green]pass[/green]" if passed else "[bold red]fail[/bold red]",
        )
    console.print(table)

    if output:
        payload = {
            "base_url": result.base_url,
            "scenarios": [
                {
                    "vus": s.vus,
                    "duration_s": s.duration_s,
                    "requests": s.requests,
                    "requests_per_s": s.requests_per_s,
                    "failed_rate": s.failed_rate,
                    "latency_avg_ms": s.latency_avg_ms,
                    "latency_p95_ms": s.latency_p95_ms,
                    "latency_p99_ms": s.latency_p99_ms,
                    "latency_max_ms": s.latency_max_ms,
                    "passed": s.passes(max_failed_rate=max_failed_rate, max_p95_ms=max_p95_ms),
                }
                for s in result.scenarios
            ],
        }
        output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        console.print(f"\n[dim]wrote {output}[/dim]")

    if not all_passed:
        raise typer.Exit(code=1)


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
