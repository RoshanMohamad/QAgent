"""`qagent report`: a saved scan result rendered as a section 15 defect report.

Everything in a scan result came from a third-party application's responses,
so beyond layout these tests check that a hostile value can't inject Markdown
structure (a heading, a closed code fence, an HTML tag).
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from qagent.cli import app
from qagent.modules.reporting.markdown import render_markdown

runner = CliRunner()


def _outcome(name: str, severity: str | None, *, actual: str = "HTTP 500") -> dict:
    outcome = {
        "name": name,
        "endpoint": "POST /orders",
        "status": "failed",
        "request": {"method": "POST", "path": "/orders", "json": {"product_id": 999}},
        "response": {"status": 500},
        "failure_message": "expected status in [201], got 500",
        "verdict": {"failure_class": "real_bug", "confidence": 0.9},
        "bug": None,
    }
    if severity:
        outcome["bug"] = {
            "title": name,
            "severity": severity,
            "expected": "A 4xx for an unknown product.",
            "actual": actual,
            "root_cause": "OrderService reads product.price before checking the product exists.",
            "suggested_fix": "Return 404 when the product lookup is empty.",
            "steps": ["Send POST /orders", 'Body: {"product_id": 999}', "Observe HTTP 500"],
        }
    return outcome


def _result(*outcomes: dict) -> dict:
    return {
        "summary": {
            "base_url": "http://shop.test",
            "total": len(outcomes),
            "passed": 0,
            "failed": len(outcomes),
            "errored": 0,
            "classifications": {"real_bug": 2, "environment": 1},
            "surface": {"endpoints_total": 10, "endpoints_covered": 8, "endpoint_percent": 80},
            "duration_s": 1.2,
        },
        "outcomes": list(outcomes),
    }


def test_defects_are_ordered_by_severity_with_every_section_15_field() -> None:
    markdown = render_markdown(
        _result(_outcome("low one", "low"), _outcome("crash", "critical"), _outcome("ok", None))
    )

    assert markdown.index("BUG-1: crash") < markdown.index("BUG-2: low one")
    assert "BUG-3" not in markdown  # a passing/non-defect outcome is not a defect
    for field in ("**Severity:** CRITICAL", "**Steps to reproduce**", "**Expected:**",
                  "**Actual:**", "**Root cause:**", "**Suggested fix:**", "Evidence"):
        assert field in markdown
    assert "1. Send POST /orders" in markdown


def test_summary_and_failure_analysis_are_reported() -> None:
    markdown = render_markdown(_result(_outcome("crash", "high")))

    assert "| 1 | 0 | 1 | 0 | 1 |" in markdown
    assert "| Real application bug | 2 |" in markdown
    assert "| Environment failure | 1 |" in markdown
    assert "8 of 10 endpoints (80%)" in markdown


def test_no_defects_says_so() -> None:
    assert "No defects were found." in render_markdown(_result(_outcome("ok", None)))


def test_hostile_values_cannot_inject_markdown_structure() -> None:
    hostile = "x\n# Injected heading\n```\n<script>alert(1)</script> [link](http://evil)"
    outcome = _outcome(hostile, "high", actual=f"HTTP 500 - {hostile} ``` `")
    outcome["bug"]["root_cause"] = hostile
    outcome["response"] = {"status": 500, "body_text": "```\n# Also injected"}

    markdown = render_markdown(_result(outcome))

    assert "\n# Injected heading" not in markdown
    # prose is escaped; the response excerpt sits inside a code span, where
    # Markdown renders a tag as literal text
    lines = markdown.splitlines()
    root_cause = next(line for line in lines if line.startswith("**Root cause:**"))
    assert "\\<script\\>" in root_cause
    assert "\\[link\\]" in root_cause
    actual = next(line for line in lines if line.startswith("**Actual:**"))
    assert actual.removeprefix("**Actual:** ").startswith("````")
    # the evidence block's fence is longer than any backtick run inside it
    evidence = markdown.split("Response:")[1]
    opening = evidence.strip().splitlines()[0]
    assert opening.startswith("```") and "```" in evidence.replace(opening, "", 1)


def test_cli_prints_to_stdout_and_writes_a_file(tmp_path: Path) -> None:
    scan = tmp_path / "scan.json"
    scan.write_text(json.dumps(_result(_outcome("crash", "high"))), encoding="utf-8")

    printed = runner.invoke(app, ["report", "--json", str(scan), "--title", "nightly"])
    assert printed.exit_code == 0, printed.output
    assert printed.output.startswith("# nightly")

    target = tmp_path / "report.md"
    written = runner.invoke(app, ["report", "--json", str(scan), "--out", str(target)])
    assert written.exit_code == 0
    assert "BUG-1: crash" in target.read_text(encoding="utf-8")


def test_cli_rejects_a_file_that_is_not_a_scan_result(tmp_path: Path) -> None:
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"hello": "world"}), encoding="utf-8")

    result = runner.invoke(app, ["report", "--json", str(other)])

    assert result.exit_code == 2


def test_test_is_the_scan_command_under_its_section_19_name() -> None:
    from qagent.cli import scan

    registered = {c.name or c.callback.__name__: c.callback for c in app.registered_commands}
    assert registered["test"] is scan
    assert registered["scan"] is scan
