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
        body = (
            f"[bold]{bug.get('title')}[/bold]\n\n"
            f"severity   {bug.get('severity')}\n"
            f"expected   {bug.get('expected')}\n"
            f"actual     {bug.get('actual')}\n\n"
            f"root cause {bug.get('root_cause')}\n\n"
        )
        # CLAUDE.md section 13's "Affected: OrderService.createOrder()" line.
        # Present only when a checkout was indexed, so its absence is not a gap
        # in the report, it is the honest consequence of having no source.
        if bug.get("affected_location"):
            body += f"affected   {bug['affected_location']}\n"
            others = [
                c["location"]
                for c in (bug.get("affected_code") or [])
                if c["location"] != bug["affected_location"]
            ]
            if others:
                body += f"also see   {', '.join(others[:3])}\n"
            body += "\n"
        body += f"fix        {bug.get('suggested_fix')}"

        console.print(Panel(body, border_style="red", title="defect"))

    # Surface coverage answers a different question from plan coverage: not
    # "did we run the plan" but "how much of the API did anything touch".
    surface = (summary.get("surface") or {}).get("endpoint_surface") or {}
    if surface.get("total"):
        colour = "green" if surface["percent"] == 100 else "yellow"
        console.print(
            f"\n[{colour}]surface[/{colour}] {surface['covered']}/{surface['total']} "
            f"discovered endpoints exercised ({surface['percent']}%)"
        )
        if surface.get("uncovered"):
            shown = ", ".join(surface["uncovered"][:5])
            more = surface["uncovered_total"] - min(5, len(surface["uncovered"]))
            console.print(
                f"  [yellow]untouched:[/yellow] {shown}" + (f" (+{more} more)" if more > 0 else "")
            )

    # Only worth the reader's attention when the plan was not fully covered -
    # a coverage line that always says 100% teaches people to stop reading it.
    cov = summary.get("coverage") or {}
    if cov.get("missing"):
        console.print(
            f"\n[yellow]coverage[/yellow] {cov['generated']}/{cov['planned']} planned checks "
            f"generated ({cov['ratio']:.0%}); {cov['missing']} skipped by the case limit."
        )
        if cov.get("uncovered_modules"):
            console.print(
                f"  [yellow]untested modules:[/yellow] {', '.join(cov['uncovered_modules'])}"
            )

    llm = summary.get("llm") or {}
    if llm.get("calls"):
        console.print(
            f"\n[dim]llm: {llm['calls']} calls, {llm['tokens']} tokens, ${llm['usd']:.4f}[/dim]"
        )


def _code_index_for(repo: Path | None, settings: Any) -> Any:
    """Build a repository index when a checkout is available, else None.

    Indexing is best-effort by design: a repository QAgent cannot chunk still
    gets scanned, it just gets bug reports without an "Affected" line. Failing
    the run over it would trade the whole result for one field.
    """
    if repo is None:
        return None

    from qagent.modules.rag.chunker import chunk_repository
    from qagent.modules.rag.embeddings import build_embedder
    from qagent.modules.rag.index import build_index

    try:
        chunks = chunk_repository(repo)
    except Exception as exc:  # noqa: BLE001 - evidence is an upgrade, not a requirement
        console.print(f"[yellow]could not index {repo}: {exc}[/yellow]")
        return None

    if not chunks:
        return None

    built = build_index(chunks, embedder=build_embedder(settings))
    console.print(
        f"[dim]indexed {built.summary()['chunks']} chunks from "
        f"{built.summary()['files']} files for root-cause evidence[/dim]"
    )
    return built


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
        code_index=_code_index_for(repo, settings),
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
        "plan": result.plan.to_dict() if result.plan else None,
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
    interact: bool = typer.Option(
        False, "--interact", help="Fill forms and click through pages, not just follow links."
    ),
    max_actions: int = typer.Option(
        40, "--max-actions", help="With --interact, hard cap on total actions taken."
    ),
    allow_destructive: bool = typer.Option(
        False,
        "--allow-destructive",
        help="With --interact, permit delete/pay/cancel-looking actions (off by default).",
    ),
    output: Path | None = typer.Option(None, "--json", help="Write the state graph as JSON."),
    fail_on_defect: bool = typer.Option(
        True,
        "--fail-on-defect/--no-fail-on-defect",
        help="With --check or --interact, exit non-zero on a defect.",
    ),
) -> None:
    """Explorer agent (CLAUDE.md §8-9): crawl same-origin pages and build a state graph.

    Link discovery only by default — what that buys the pipeline: pages nobody
    listed with --route. Add --check to run scan-ui's page-load checks against
    everything it finds, or --interact to fill forms and click through pages
    instead of only following links (see ADR-0005). Destructive-looking
    actions (delete, pay, cancel, ...) are skipped unless --allow-destructive
    is passed, and forms are only ever filled with synthetic values.
    """
    if interact:
        try:
            from qagent.modules.explorer.actions import InteractionPolicy
            from qagent.modules.explorer.interact import explore_interactive
        except ModuleNotFoundError as exc:
            console.print(
                "[red]playwright is not installed.[/red] Run "
                "[bold]pip install qagent[e2e] && playwright install chromium[/bold]."
            )
            raise typer.Exit(code=2) from exc

        graph = explore_interactive(
            base_url=url,
            max_pages=max_pages,
            max_depth=max_depth,
            max_total_actions=max_actions,
            timeout_seconds=timeout,
            headless=not headed,
            policy=InteractionPolicy(allow_destructive=allow_destructive),
            llm=LlmClient.from_settings(get_settings()),
        )
    else:
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
    if interact:
        table.add_column("actions", justify="right", width=7)
    for node in sorted(graph.nodes.values(), key=lambda n: (n.depth, n.url)):
        row = [str(node.depth), node.url, node.title or "", str(node.link_count)]
        if interact:
            row.append(str(len(node.actions_taken)))
        table.add_row(*row)
    console.print(table)

    defect_found = False
    if interact:
        action_table = Table(show_header=True, header_style="bold", title="actions taken")
        action_table.add_column("url", overflow="fold")
        action_table.add_column("action")
        action_table.add_column("element", overflow="fold")
        action_table.add_column("result")
        for node in graph.nodes.values():
            for outcome in node.actions_taken:
                if not outcome.ok:
                    result_text = f"[yellow]error: {outcome.error}[/yellow]"
                elif outcome.page_errors or outcome.console_errors:
                    result_text = "[bold red]defect[/bold red]"
                    defect_found = True
                else:
                    result_text = "[green]ok[/green]"
                element = outcome.action.target.text or outcome.action.target.selector
                action_table.add_row(node.url, outcome.action.type.value, element, result_text)
        if any(node.actions_taken for node in graph.nodes.values()):
            console.print()
            console.print(action_table)

    if output:
        output.write_text(json.dumps(graph.to_dict(), indent=2), encoding="utf-8")
        console.print(f"\n[dim]wrote {output}[/dim]")

    if not check:
        if interact and fail_on_defect and defect_found:
            raise typer.Exit(code=1)
        return

    run_browser_checks = _import_browser_runner()
    result = run_browser_checks(
        base_url=graph.root, routes=graph.routes, timeout_seconds=timeout, headless=not headed
    )
    console.print()
    _render_browser_result(result, title="QAgent explore --check")

    if fail_on_defect and (result.failed or defect_found):
        raise typer.Exit(code=1)


@app.command()
def analyze(
    repo: Path = typer.Option(..., "--repo", "-r", help="Repository checkout to analyze."),
    spec: str | None = typer.Option(
        None, "--spec", "-s", help="OpenAPI document URL, if the app is also running."
    ),
    url: str | None = typer.Option(
        None, "--url", "-u", help="Base URL to probe for an OpenAPI document, same as `endpoints`."
    ),
    output: Path | None = typer.Option(None, "--json", help="Write the full analysis as JSON."),
) -> None:
    """Project Analyst agent: detect the stack and build the module tree (CLAUDE.md
    sections 6-8, agent 1) from a repo checkout alone -- nothing here needs the app
    to be running. Pass --url/--spec too and endpoint grouping uses the real OpenAPI
    surface instead of falling back to static route parsing.
    """
    from qagent.modules.analyzer.analyst import analyze_repository

    if not repo.is_dir():
        console.print(f"[red]not a directory:[/red] {repo}")
        raise typer.Exit(code=2)

    found_endpoints = None
    if url or spec:
        from qagent.pipeline import discover

        found_endpoints, _source = discover(url or "", spec, None)
        found_endpoints = found_endpoints or None

    analysis = analyze_repository(repo, endpoints=found_endpoints)
    _render_analysis(analysis, label=str(repo), title="QAgent analyze", output=output)


def _render_analysis(analysis: Any, *, label: str, title: str, output: Path | None) -> None:
    """Print a ``ProjectAnalysis``. Shared by `analyze` (a local checkout) and
    `connect` (a freshly cloned one) so the two never drift into reporting the
    same analysis differently."""
    summary = analysis.summary()

    console.print(
        Panel(
            f"[bold]{label}[/bold]\n"
            f"{len(analysis.stack.technologies)} technologies - "
            f"{summary['endpoint_count']} endpoints - "
            f"{summary['frontend_route_count']} frontend routes - "
            f"{summary['database_model_count']} database models",
            title=title,
            border_style="blue",
        )
    )

    if analysis.stack.technologies:
        table = Table(title="Detected stack", show_header=True, header_style="bold")
        table.add_column("category")
        table.add_column("technology")
        table.add_column("version")
        table.add_column("confidence", justify="right")
        for tech in analysis.stack.technologies:
            table.add_row(
                tech.category, tech.name, tech.version or "-", f"{tech.confidence:.2f}"
            )
        console.print(table)

    if analysis.tree.frontend:
        console.print("\n[bold]Frontend routes[/bold]")
        for route in analysis.tree.frontend:
            console.print(f"  {route}")

    if analysis.tree.backend:
        console.print(f"\n[bold]Backend modules[/bold] ({analysis.endpoint_source})")
        for module in analysis.tree.backend:
            flag = " [red]![/red]" if module.risk_reason else ""
            console.print(f"  {module.name} ({len(module.endpoints)} endpoint(s)){flag}")

    if analysis.tree.database:
        console.print("\n[bold]Database models[/bold]")
        for name in analysis.tree.database:
            console.print(f"  {name}")

    if analysis.tree.risky_components:
        console.print("\n[bold red]Risky components[/bold red]")
        for risky in analysis.tree.risky_components:
            console.print(f"  {risky.name} (risk {risky.max_risk_score:.2f}): {risky.reason}")

    for warning in analysis.warnings:
        console.print(f"\n[yellow]warning:[/yellow] {warning}")

    if output:
        output.write_text(json.dumps(analysis.to_dict(), indent=2), encoding="utf-8")
        console.print(f"\n[dim]wrote {output}[/dim]")


@app.command()
def connect(
    repo_url: str = typer.Option(
        ..., "--repo", "-r", help="https://github.com/owner/repo, or the owner/repo shorthand."
    ),
    branch: str | None = typer.Option(None, "--branch", "-b", help="Branch (default: the repo's)."),
    token: str | None = typer.Option(
        None, "--token", help="GitHub token for private repos; defaults to $GITHUB_TOKEN."
    ),
    output: Path | None = typer.Option(None, "--json", help="Write the full analysis as JSON."),
) -> None:
    """Clone a GitHub repository and analyze it (CLAUDE.md section 6).

    The same Project Analyst `analyze` runs, against a shallow clone made in a
    temporary directory and deleted when this exits. Nothing in the checkout is
    executed (ADR-0006) -- only text is read.
    """
    import os

    from qagent.modules.analyzer.analyst import analyze_repository
    from qagent.modules.provisioning.clone import CloneError, cloned_repository, head_commit

    resolved_token = token or os.environ.get("GITHUB_TOKEN")

    try:
        console.print(f"[dim]cloning {repo_url} ...[/dim]")
        with cloned_repository(repo_url, branch=branch, token=resolved_token) as repo_dir:
            commit_sha = head_commit(repo_dir)
            analysis = analyze_repository(repo_dir)
    except CloneError as exc:
        console.print(f"[red]clone failed:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    label = repo_url if not commit_sha else f"{repo_url} @ {commit_sha[:8]}"
    _render_analysis(analysis, label=label, title="QAgent connect", output=output)


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


@app.command("record-import")
def record_import(
    session_file: Path = typer.Argument(..., help="Session JSON exported by the extension."),
    base_url: str | None = typer.Option(
        None, "--url", "-u", help="Only keep traffic to this origin."
    ),
    out: Path | None = typer.Option(
        None, "--out", "-o", help="Directory to emit runnable pytest files into."
    ),
    output: Path | None = typer.Option(None, "--json", help="Write the converted session."),
) -> None:
    """Turn a recorded browser session into tests (CLAUDE.md section 10).

    The clicks become a UI flow document; the XHR/fetch traffic the flow
    provoked becomes API checks in the same declarative shape the generator
    produces, so they run through the existing runner and triage unchanged.

    That second half is the point. A recorded session reaches requests static
    discovery cannot: they need a logged-in user, a cart with something in it,
    an order that already exists.
    """
    from qagent.modules.recorder.session import parse_session, to_api_cases, to_ui_flow

    try:
        document = json.loads(session_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        console.print(f"[red]could not read session file:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    session = parse_session(document)
    cases = to_api_cases(session, base_url=base_url)
    flow = to_ui_flow(session)

    console.print(
        Panel(
            f"[bold]{session_file.name}[/bold]\n"
            f"start: {session.start_url or 'unknown'}\n"
            f"{len(session.actions)} UI action(s), {len(session.requests)} request(s)\n"
            f"{len(cases)} API check(s) derived",
            title="QAgent record",
            border_style="blue",
        )
    )

    for warning in session.warnings:
        console.print(f"[yellow]warning[/yellow] {warning}")

    # A console error captured while recording is a finding in its own right:
    # the flow already reproduced something before any test was written.
    for diagnostic in session.diagnostics:
        console.print(f"[red]observed[/red] {diagnostic}")

    if flow["requires_secrets"]:
        console.print(
            f"\n[yellow]{len(flow['requires_secrets'])} field(s) were redacted[/yellow] "
            "(password-like inputs). Supply them from a secret store when replaying."
        )

    if cases:
        table = Table(show_header=True, header_style="bold")
        table.add_column("method", width=7)
        table.add_column("path", overflow="fold")
        table.add_column("body", width=9)
        table.add_column("asserts", overflow="fold")
        for case in cases:
            request = case.spec["request"]
            success = next(
                (a for a in case.spec["assertions"] if a["type"] == "status_in"), None
            )
            # A request with no recorded body cannot be replayed faithfully, so
            # it only asserts the invariant that holds regardless of input.
            # Showing that plainly beats printing a list of 5xx codes under a
            # column headed "expects".
            table.add_row(
                request["method"],
                request["path"],
                "recorded" if request.get("json") is not None else "[dim]none[/dim]",
                f"status in {success['value']}" if success else "never a 5xx",
            )
        console.print(table)

    if out:
        from qagent.modules.emitter.pytest_emitter import emit as emit_pytest

        report = emit_pytest(
            cases, base_url=base_url or session.start_url or "http://localhost", out_dir=out
        )
        console.print(f"\n[dim]wrote {report.case_count} test(s) to {out}[/dim]")

    if output:
        output.write_text(
            json.dumps(
                {
                    "summary": session.summary(),
                    "ui_flow": flow,
                    "api_cases": [c.to_dict() for c in cases],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        console.print(f"[dim]wrote {output}[/dim]")

    if not session.actions and not session.requests:
        raise typer.Exit(code=2)


@app.command()
def emit(
    url: str = typer.Option(..., "--url", "-u", help="Base URL of the running application."),
    out: Path = typer.Option(..., "--out", "-o", help="Directory to write the test files into."),
    spec: str | None = typer.Option(None, "--spec", "-s", help="Explicit OpenAPI document URL."),
    repo: Path | None = typer.Option(
        None, "--repo", help="Repo source root to statically parse routes from as a fallback."
    ),
    max_cases: int | None = typer.Option(None, "--max-cases", help="Cap generated checks."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would be written."),
) -> None:
    """Write the generated checks out as runnable pytest files (agent 3).

    The emitted suite depends only on pytest and httpx - not on QAgent - so the
    tests keep working if this tool is uninstalled. Point them at an application
    with QAGENT_BASE_URL and supply credentials with QAGENT_AUTH_HEADER.
    """
    from qagent.modules.emitter.pytest_emitter import emit as emit_pytest
    from qagent.modules.generator.rules import generate
    from qagent.modules.planner.strategy import build_plan
    from qagent.pipeline import discover

    endpoints, source = discover(url, spec, repo)
    if not endpoints:
        console.print("[red]no endpoints discovered; nothing to emit[/red]")
        raise typer.Exit(code=2)

    # Planned ordering matters here too: under --max-cases, the emitted suite
    # should contain the critical modules, not whatever came first.
    test_plan = build_plan(endpoints)
    generation = generate(endpoints, max_cases=max_cases, plan=test_plan)

    report = emit_pytest(generation.cases, base_url=url, out_dir=out, dry_run=dry_run)

    console.print(
        Panel(
            f"[bold]{out}[/bold]\nsource: {source}\n"
            f"{report.case_count} test(s) across {len(report.files)} file(s)"
            + ("  [yellow](dry run, nothing written)[/yellow]" if dry_run else ""),
            title="QAgent emit",
            border_style="blue",
        )
    )

    table = Table(show_header=True, header_style="bold")
    table.add_column("file", overflow="fold")
    table.add_column("tests", justify="right", width=6)
    for emitted in report.files:
        table.add_row(emitted.path, str(emitted.case_count))
    if table.row_count:
        console.print(table)

    for skipped in report.skipped:
        console.print(f"[yellow]skipped[/yellow] {skipped}")

    if not dry_run and report.files:
        console.print(
            f"\n[dim]run them with: QAGENT_BASE_URL={url} python -m pytest {out}[/dim]"
        )


@app.command()
def gate(
    url: str = typer.Option(..., "--url", "-u", help="Base URL of the running application."),
    spec: str | None = typer.Option(None, "--spec", "-s", help="Explicit OpenAPI document URL."),
    repo: Path | None = typer.Option(
        None, "--repo", help="Repo source root, for route parsing and root-cause evidence."
    ),
    header: list[str] = typer.Option(
        [], "--header", "-H", help="Auth header, e.g. 'Authorization: Bearer x'."
    ),
    max_cases: int | None = typer.Option(None, "--max-cases", help="Cap generated checks."),
    timeout: float = typer.Option(30.0, "--timeout", help="Per-request timeout in seconds."),
    max_critical: int = typer.Option(0, "--max-critical", help="Critical defects allowed."),
    max_high: int = typer.Option(0, "--max-high", help="High defects allowed."),
    max_medium: int | None = typer.Option(None, "--max-medium", help="Medium defects allowed."),
    max_low: int | None = typer.Option(None, "--max-low", help="Low defects allowed."),
    min_coverage: float | None = typer.Option(
        None, "--min-coverage", help="Fraction of the test plan that must run, 0..1."
    ),
    max_errors: int | None = typer.Option(
        None, "--max-errors", help="Errored checks allowed (usually infrastructure, not defects)."
    ),
    output: Path | None = typer.Option(None, "--json", help="Write the decision as JSON."),
) -> None:
    """Run QA and exit non-zero if the result should block a deploy.

    This is the CI entry point (CLAUDE.md sections 18-19). It needs no database,
    no project and no org, because a pull request has none of those - requiring
    them would mean standing up Postgres to find out whether a branch is safe.

    Exit codes: 0 deploy, 1 block, 2 the run could not be evaluated at all.
    """
    from qagent.modules.gate.policy import GatePolicy, evaluate, render

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
        code_index=_code_index_for(repo, settings),
    )

    if result.errors and not result.outcomes:
        # Nothing ran. Reporting "pass" here would be the worst possible
        # outcome: a gate that waves through every deploy because it never
        # managed to test anything.
        for error in result.errors:
            console.print(f"[red]error[/red] {error}")
        console.print("\n[red]RESULT: ERROR[/red] - nothing was tested, so nothing was verified.")
        raise typer.Exit(code=2)

    decision = evaluate(
        result,
        GatePolicy(
            max_critical=max_critical,
            max_high=max_high,
            max_medium=max_medium,
            max_low=max_low,
            min_coverage=min_coverage,
            max_errors=max_errors,
        ),
    )

    console.print(
        Panel(
            render(decision),
            border_style="red" if decision.blocked else "green",
            title="quality gate",
        )
    )

    if output:
        output.write_text(
            json.dumps(
                {"summary": result.summary(), "gate": decision.to_dict()}, indent=2, default=str
            ),
            encoding="utf-8",
        )
        console.print(f"[dim]wrote {output}[/dim]")

    raise typer.Exit(code=decision.exit_code)


@app.command()
def search(
    query: str = typer.Argument(..., help="What to look for, e.g. 'create order price'."),
    repo: Path = typer.Option(..., "--repo", help="Repository checkout to search."),
    k: int = typer.Option(5, "--k", help="Number of results."),
    show_code: bool = typer.Option(False, "--code", help="Print the matching source."),
) -> None:
    """Search a checkout with the same retriever bug reports use (modules/rag/).

    Exposed as its own command because it is the only way to see *why* a bug
    report cited the function it cited, and the fastest way to tell whether
    retrieval is working on a given repository before trusting it in a run.
    """
    from qagent.modules.rag.chunker import chunk_repository
    from qagent.modules.rag.embeddings import build_embedder
    from qagent.modules.rag.index import build_index

    try:
        chunks = chunk_repository(repo)
    except NotADirectoryError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    index = build_index(chunks, embedder=build_embedder())
    summary = index.summary()
    console.print(
        f"[dim]{summary['chunks']} chunks from {summary['files']} files "
        f"({summary['symbols']} named symbols), retriever={summary['retriever']}[/dim]\n"
    )

    hits = index.search(query, k=k)
    if not hits:
        console.print("[yellow]no matches[/yellow]")
        raise typer.Exit(code=1)

    for hit in hits:
        console.print(f"[green]{hit.score:>7.3f}[/green]  {hit.chunk.location}")
        if show_code:
            console.print(Panel(hit.chunk.text[:1200], border_style="dim"))


@app.command()
def index(
    repo: Path = typer.Option(..., "--repo", help="Repository checkout to index."),
    output: Path | None = typer.Option(None, "--json", help="Write the chunk manifest as JSON."),
) -> None:
    """Chunk a checkout and report what the index would contain.

    Useful before a scan: it answers whether QAgent can actually see this
    repository's source, which is a different question from whether the scan
    passes.
    """
    from qagent.modules.rag.chunker import chunk_repository
    from qagent.modules.rag.embeddings import build_embedder
    from qagent.modules.rag.index import build_index

    try:
        chunks = chunk_repository(repo)
    except NotADirectoryError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    built = build_index(chunks, embedder=build_embedder())
    summary = built.summary()
    note = "" if summary["embedded"] else "  [dim](lexical only; no embedder configured)[/dim]"

    console.print(
        Panel(
            f"[bold]{repo}[/bold]\n"
            f"{summary['chunks']} chunks from {summary['files']} files\n"
            f"{summary['symbols']} named symbols\n"
            f"retriever: {summary['retriever']}{note}",
            title="QAgent index",
            border_style="blue",
        )
    )

    by_language: dict[str, int] = {}
    for chunk in chunks:
        by_language[chunk.language] = by_language.get(chunk.language, 0) + 1
    for language, count in sorted(by_language.items(), key=lambda kv: -kv[1]):
        console.print(f"  {count:>5}  {language}")

    if output:
        output.write_text(
            json.dumps({"summary": summary, "chunks": [c.to_dict() for c in chunks]}, indent=2),
            encoding="utf-8",
        )
        console.print(f"\n[dim]wrote {output}[/dim]")


_PRIORITY_STYLE = {
    "critical": "bold red",
    "high": "red",
    "medium": "yellow",
    "low": "dim",
}


@app.command()
def plan(
    url: str = typer.Option(..., "--url", "-u", help="Base URL of the running application."),
    spec: str | None = typer.Option(None, "--spec", "-s", help="Explicit OpenAPI document URL."),
    repo: Path | None = typer.Option(
        None, "--repo", help="Repo source root to statically parse routes from as a fallback."
    ),
    max_cases: int | None = typer.Option(
        None, "--max-cases", help="Show what a run capped at this many checks would cover."
    ),
    enrich: bool = typer.Option(
        False, "--enrich", help="Spend one model call letting a review raise module priorities."
    ),
    show_checks: bool = typer.Option(False, "--checks", help="List every required check."),
    output: Path | None = typer.Option(None, "--json", help="Write the plan as JSON."),
) -> None:
    """Show the test strategy (agent 2) without executing anything.

    Useful on its own -- it answers "what would you test, and in what order?"
    before committing to a run -- and useful with ``--max-cases``, which is the
    only way to see what a budgeted run is about to leave untested.
    """
    from qagent.modules.generator.rules import generate
    from qagent.modules.planner.strategy import build_plan, coverage, enrich_plan
    from qagent.pipeline import discover

    found, source = discover(url, spec, repo)
    if not found:
        console.print("[red]no endpoints discovered; nothing to plan[/red]")
        raise typer.Exit(code=2)

    test_plan = build_plan(found)
    if enrich:
        llm = LlmClient.from_settings()
        if not llm.available:
            console.print("[yellow]no model provider configured; plan is rules-only[/yellow]")
        test_plan = enrich_plan(test_plan, llm)

    summary = test_plan.summary()
    console.print(
        Panel(
            f"[bold]{url}[/bold]\nsource: {source}\n"
            f"{len(found)} endpoints in {summary['modules']} modules, "
            f"{summary['required_checks']} required checks",
            title="QAgent plan",
            border_style="blue",
        )
    )

    for warning in test_plan.warnings:
        console.print(f"[yellow]warning[/yellow] {warning}")

    table = Table(show_header=True, header_style="bold")
    table.add_column("priority", width=9)
    table.add_column("module", width=18, overflow="fold")
    table.add_column("endpoints", justify="right", width=9)
    table.add_column("checks", justify="right", width=6)
    table.add_column("why", overflow="fold")

    for module in test_plan.modules:
        style = _PRIORITY_STYLE.get(module.priority.value, "white")
        table.add_row(
            f"[{style}]{module.priority.value}[/]",
            module.name,
            str(len(module.endpoint_keys)),
            str(len(module.checks)),
            module.rationale,
        )
    console.print(table)

    if show_checks:
        for module in test_plan.modules:
            if not module.checks:
                continue
            console.print(f"\n[bold]{module.name}[/bold] ({module.priority.value})")
            for check in module.checks:
                console.print(f"  [green]+[/green] {check.endpoint_key} - {check.intent}")

    if max_cases is not None:
        result = coverage(test_plan, generate(found, max_cases=max_cases, plan=test_plan))
        colour = "green" if result.missing == 0 else "yellow"
        console.print(
            f"\n[{colour}]at --max-cases {max_cases}:[/] {result.generated}/{result.planned} "
            f"planned checks would run ({result.ratio:.0%})"
        )
        if result.uncovered_modules:
            console.print(
                f"  [yellow]left untested:[/yellow] {', '.join(result.uncovered_modules)}"
            )

    if output:
        output.write_text(json.dumps(test_plan.to_dict(), indent=2), encoding="utf-8")
        console.print(f"\n[dim]wrote {output}[/dim]")


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
    scanners: str = typer.Option(
        "semgrep,trivy",
        "--scanners",
        help="Comma-separated static scanners. A missing one is skipped, not fatal.",
    ),
    config: str = typer.Option(
        "auto", "--config", help="Semgrep config: 'auto', a ruleset name, or a local rules file."
    ),
    timeout: float = typer.Option(600.0, "--timeout", help="Scan timeout in seconds."),
    fail_on: str = typer.Option(
        "high", "--fail-on", help="Minimum severity that exits non-zero: critical|high|medium|low."
    ),
    output: Path | None = typer.Option(None, "--json", help="Write findings as JSON."),
) -> None:
    """Static analysis over a checkout (CLAUDE.md section 16): `qagent security`.

    Runs Semgrep (bugs in the code this project wrote) and Trivy (known CVEs in
    the code it imported). Both parse files and never execute them, so this
    needs none of the sandboxing the runner/browser/explorer commands require
    for a live target. `qagent dast` is the running-application scanner.

    A scanner that is not installed is reported as a skip rather than failing
    the run: almost nobody has all of them on day one, and a scan that refuses
    to start is a scan that gets removed from CI. The skip is always printed,
    because "no findings" means nothing without knowing what actually ran.
    """
    from qagent.modules.security.aggregate import scan_repository

    requested = tuple(name.strip() for name in scanners.split(",") if name.strip())
    if not requested:
        console.print("[red]no scanners requested[/red]")
        raise typer.Exit(code=2)

    if not repo.is_dir():
        console.print(f"[red]not a directory: {repo}[/red]")
        raise typer.Exit(code=2)

    result = scan_repository(repo, scanners=requested, timeout_seconds=timeout)

    counts = result.counts_by_severity()
    by_scanner = result.counts_by_scanner()
    ran = ", ".join(f"{k} ({v})" for k, v in sorted(by_scanner.items())) or "none"
    console.print(
        Panel(
            f"[bold]{result.root}[/bold]\n{len(result.findings)} finding(s)\n"
            f"scanners with findings: {ran}",
            title="QAgent security",
            border_style="blue",
        )
    )

    for name, reason in sorted(result.skipped.items()):
        console.print(f"[yellow]skipped {name}:[/yellow] {reason}")

    table = Table(show_header=True, header_style="bold")
    table.add_column("severity", width=10)
    table.add_column("from", width=8)
    table.add_column("rule", overflow="fold")
    table.add_column("location", overflow="fold")
    table.add_column("message", overflow="fold")
    for finding in result.sorted_findings():
        style = _FINDING_SEVERITY_STYLE.get(finding.severity, "")
        message = finding.message
        # The fixed version is the single most actionable field a dependency
        # finding has; it must not get lost inside a truncated description.
        if finding.fixed_version:
            message = f"[green]fix: {finding.fixed_version}[/green] - {message}"
        table.add_row(
            f"[{style}]{finding.severity}[/{style}]" if style else finding.severity,
            finding.scanner,
            finding.rule_id,
            f"{finding.path}:{finding.line}" if finding.line else finding.path,
            message,
        )
    if table.row_count:
        console.print(table)

    for error in result.scan_errors:
        console.print(f"\n[yellow]{error}[/yellow]")

    if output:
        output.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
        console.print(f"\n[dim]wrote {output}[/dim]")

    threshold = _SEVERITY_RANK.get(fail_on, 3)
    if any(_SEVERITY_RANK.get(sev, 0) >= threshold and n for sev, n in counts.items()):
        raise typer.Exit(code=1)


@app.command()
def dast(
    url: str = typer.Option(..., "--url", "-u", help="Base URL of the running application."),
    zap_url: str = typer.Option(
        "http://127.0.0.1:8090", "--zap", help="Address of the ZAP daemon's API."
    ),
    api_key: str | None = typer.Option(None, "--api-key", help="ZAP API key, if one is set."),
    max_pages: int = typer.Option(50, "--max-pages", help="Cap on pages the spider may visit."),
    max_wait: float = typer.Option(300.0, "--max-wait", help="Seconds to wait per scan phase."),
    active: bool = typer.Option(
        False,
        "--active",
        help="Send attack traffic. Only against a system you are authorised to attack.",
    ),
    fail_on: str = typer.Option(
        "high", "--fail-on", help="Minimum severity that exits non-zero: critical|high|medium|low."
    ),
    output: Path | None = typer.Option(None, "--json", help="Write findings as JSON."),
) -> None:
    """Dynamic analysis of a running application via OWASP ZAP (CLAUDE.md §16).

    The third kind of scanner: `security` reads source and lockfiles, this
    sends requests and reads what comes back, which is the only way to see a
    missing security header or a cookie without HttpOnly.

    Passive by default. `--active` makes ZAP send injection and traversal
    payloads and can mutate application state, so it is opt-in and always
    announced - a QA tool that attacks a host because a flag defaulted to true
    is a liability, not a feature.
    """
    from qagent.modules.security.base import ScannerError, ScannerUnavailable
    from qagent.modules.security.zap import ZapConfig, run_zap

    if active:
        console.print(
            "[yellow]--active: sending attack traffic. Only do this against a system "
            "you are authorised to test.[/yellow]"
        )

    config = ZapConfig(
        base_url=zap_url, api_key=api_key, max_children=max_pages, max_wait_seconds=max_wait
    )

    try:
        result = run_zap(url, config=config, active=active)
    except ScannerUnavailable as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    except ScannerError as exc:
        console.print(f"[red]zap error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    counts = result.counts_by_severity()
    console.print(
        Panel(
            f"[bold]{url}[/bold]\n{len(result.findings)} finding(s)\n"
            f"mode: {'active (attack traffic sent)' if active else 'passive'}",
            title="QAgent DAST",
            border_style="blue",
        )
    )

    table = Table(show_header=True, header_style="bold")
    table.add_column("severity", width=10)
    table.add_column("alert", overflow="fold")
    table.add_column("url", overflow="fold")
    for finding in result.sorted_findings():
        style = _FINDING_SEVERITY_STYLE.get(finding.severity, "")
        table.add_row(
            f"[{style}]{finding.severity}[/{style}]" if style else finding.severity,
            finding.title,
            finding.path,
        )
    if table.row_count:
        console.print(table)

    if output:
        output.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
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
