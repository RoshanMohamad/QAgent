"""The qagent command line (CLI surface from CLAUDE.md section 19).

Runs the full pipeline without a database, a queue or a dashboard, so the product can
be demonstrated and evaluated from a single command.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

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
            f"\n[dim]llm: {llm['calls']} calls, {llm['tokens']} tokens, "
            f"${llm['usd']:.4f}[/dim]"
        )


@app.command()
def scan(
    url: str = typer.Option(..., "--url", "-u", help="Base URL of the running application."),
    spec: str | None = typer.Option(None, "--spec", "-s", help="Explicit OpenAPI document URL."),
    header: list[str] = typer.Option([], "--header", "-H", help="Auth header, e.g. 'Authorization: Bearer x'."),
    max_cases: int | None = typer.Option(None, "--max-cases", help="Cap generated checks."),
    timeout: float = typer.Option(30.0, "--timeout", help="Per-request timeout in seconds."),
    output: Path | None = typer.Option(None, "--json", help="Write the full result as JSON."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show passing checks too."),
    fail_on_bug: bool = typer.Option(True, "--fail-on-bug/--no-fail-on-bug", help="Exit non-zero when a defect is found."),
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
        auth_headers=auth_headers,
        max_cases=max_cases,
        timeout_seconds=timeout,
        allow_private=not settings.is_production,
        allowlist=settings.egress_allowlist,
        llm=LlmClient.from_settings(settings),
    )

    _render(result, verbose=verbose)

    if output:
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

    if result.errors and not result.outcomes:
        raise typer.Exit(code=2)
    if fail_on_bug and result.bugs:
        raise typer.Exit(code=1)


@app.command()
def endpoints(
    url: str = typer.Option(..., "--url", "-u"),
    spec: str | None = typer.Option(None, "--spec", "-s"),
) -> None:
    """List discovered endpoints and their risk scores, without running anything."""
    from qagent.pipeline import discover

    found, source = discover(url, spec)
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
        colour = "red" if endpoint.risk_score >= 0.6 else "yellow" if endpoint.risk_score >= 0.35 else "green"
        table.add_row(
            f"[{colour}]{endpoint.risk_score:.2f}[/]",
            endpoint.method,
            endpoint.path,
            "yes" if endpoint.requires_auth else "-",
        )
    console.print(table)


@app.command()
def evaluate(
    fixtures: Path = typer.Option(Path("packages/fixtures"), "--fixtures", help="Fixture directory."),
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


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
