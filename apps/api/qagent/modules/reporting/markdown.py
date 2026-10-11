"""Render a ``qagent scan --json`` result as a Markdown defect report.

CLAUDE.md section 15 describes the bug report a developer should receive:
title, severity, reproduction steps, expected, actual, root cause and evidence.
The scan's JSON already carries every one of those fields per defect; this
lays them out in that order, for a CI artifact, a PR comment or an email.

Reads only the saved document - no network, no database - so it works on a
result produced anywhere, including a CI run that has since finished. Every
value in the document came from a third-party application's responses, so
nothing is emitted as raw Markdown: text is escaped, and code-ish values go
in fenced blocks whose fence is longer than any backtick run inside them.
"""

from __future__ import annotations

import json
import re
from typing import Any

_SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"]

_CLASS_LABELS = {
    "real_bug": "Real application bug",
    "flaky_test": "Flaky test",
    "environment": "Environment failure",
    "network": "Network failure",
    "dependency": "Dependency failure",
    "test_data": "Test data problem",
    "bad_assertion": "Test issue (bad assertion)",
    "unknown": "Unknown",
}

_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]<>()#+!|])")


def _text(value: Any) -> str:
    """Escape a value for inline Markdown, collapsing it onto one line."""
    if value is None or value == "":
        return "-"
    return _MD_SPECIAL.sub(r"\\\1", " ".join(str(value).split()))


def _code(value: Any) -> str:
    """Inline code span that a backtick in the value can't close early."""
    text = " ".join(str(value).split())
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _block(value: Any, language: str = "") -> list[str]:
    text = value if isinstance(value, str) else json.dumps(value, indent=2, default=str)
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"{fence}{language}", text, fence]


def _severity_rank(bug: dict) -> int:
    severity = str(bug.get("severity", "")).lower()
    return _SEVERITY_ORDER.index(severity) if severity in _SEVERITY_ORDER else len(_SEVERITY_ORDER)


def render_markdown(result: dict, *, title: str = "QAgent report") -> str:
    summary = result.get("summary") or {}
    outcomes = [o for o in result.get("outcomes") or [] if isinstance(o, dict)]
    defects = sorted((o for o in outcomes if o.get("bug")), key=lambda o: _severity_rank(o["bug"]))

    lines = [f"# {_text(title)}", ""]
    target = summary.get("base_url")
    if target:
        lines.append(f"**Target:** {_code(target)}")
    if summary.get("spec_url"):
        lines.append(f"**API description:** {_code(summary['spec_url'])}")
    if summary.get("duration_s") is not None:
        lines.append(f"**Duration:** {summary['duration_s']}s")
    lines.append("")

    lines += [
        "| Checks | Passed | Failed | Errored | Defects |",
        "|---:|---:|---:|---:|---:|",
        "| {total} | {passed} | {failed} | {errored} | {bugs} |".format(
            total=summary.get("total", len(outcomes)),
            passed=summary.get("passed", "-"),
            failed=summary.get("failed", "-"),
            errored=summary.get("errored", "-"),
            bugs=len(defects),
        ),
        "",
    ]

    classifications = summary.get("classifications") or {}
    if classifications:
        lines += ["## Failure analysis", "", "| Classification | Count |", "|---|---:|"]
        for key, count in sorted(classifications.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {_text(_CLASS_LABELS.get(key, key))} | {count} |")
        lines += [
            "",
            "Only real application bugs are reported as defects below; the rest are "
            "test-side or environment failures.",
            "",
        ]

    surface = summary.get("surface") or {}
    if surface.get("endpoints_total"):
        lines += [
            f"**API surface exercised:** {surface.get('endpoints_covered')} of "
            f"{surface.get('endpoints_total')} endpoints ({surface.get('endpoint_percent')}%)",
            "",
        ]

    lines += ["## Defects", ""]
    if not defects:
        lines += ["No defects were found.", ""]
    for index, outcome in enumerate(defects, start=1):
        lines += _render_defect(index, outcome)

    return "\n".join(lines).rstrip() + "\n"


def _render_defect(index: int, outcome: dict) -> list[str]:
    bug = outcome["bug"]
    verdict = outcome.get("verdict") or {}
    lines = [
        f"### BUG-{index}: {_text(bug.get('title') or outcome.get('name'))}",
        "",
        f"**Severity:** {_text(str(bug.get('severity', 'unknown')).upper())}",
    ]
    if outcome.get("endpoint"):
        lines.append(f"**Endpoint:** {_code(outcome['endpoint'])}")
    if verdict.get("confidence") is not None:
        lines.append(f"**Triage confidence:** {round(float(verdict['confidence']) * 100)}%")
    lines.append("")

    steps = bug.get("steps") or []
    if steps:
        lines.append("**Steps to reproduce**")
        lines.append("")
        lines += [f"{n}. {_text(step)}" for n, step in enumerate(steps, start=1)]
        lines.append("")

    lines += [
        f"**Expected:** {_text(bug.get('expected'))}",
        "",
        # A response excerpt, not prose: shown verbatim rather than escaped.
        f"**Actual:** {_code(bug['actual']) if bug.get('actual') else '-'}",
        "",
        f"**Root cause:** {_text(bug.get('root_cause'))}",
        "",
        f"**Suggested fix:** {_text(bug.get('suggested_fix'))}",
        "",
    ]

    request = outcome.get("request") or {}
    response = outcome.get("response") or {}
    if request or response:
        lines += ["<details><summary>Evidence</summary>", ""]
        if request:
            lines += ["Request:", "", *_block(request, "json"), ""]
        if response:
            lines += ["Response:", "", *_block(response, "json"), ""]
        if outcome.get("failure_message"):
            lines += ["Failure:", "", *_block(str(outcome["failure_message"])), ""]
        lines += ["</details>", ""]
    return lines
