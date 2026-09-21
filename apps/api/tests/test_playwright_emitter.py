"""Rendering a recorded UI flow as a Playwright spec.

The safety assertions here matter more than the formatting ones. Selectors and
values come from a page QAgent does not control, so a test that only checked
"the output looks right" would miss the case that matters: a recording shaped
by a hostile page producing executable code rather than data.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from typer.testing import CliRunner

from qagent.cli import app
from qagent.modules.emitter.playwright_emitter import emit, render_flow

runner = CliRunner()


def _flow(**overrides) -> dict:
    flow = {
        "kind": "ui_flow",
        "start_url": "http://shop.test/login",
        "steps": [
            {"type": "fill", "selector": "[data-testid='email']", "value": "ada@example.com"},
            {"type": "fill", "selector": "input[type=password]", "value": None, "redacted": True},
            {"type": "click", "selector": 'button:has-text("Sign in")'},
        ],
        "diagnostics": [],
        "warnings": [],
        "requires_secrets": ["input[type=password]"],
    }
    flow.update(overrides)
    return flow


# ------------------------------------------------------------------ rendering


def test_each_action_renders_to_a_playwright_call() -> None:
    content = render_flow(_flow()).content

    assert 'page.locator("[data-testid=\'email\']").fill("ada@example.com")' in content
    assert '.click();' in content


def test_select_and_check_render_distinctly() -> None:
    flow = _flow(
        steps=[
            {"type": "select", "selector": "#country", "value": "NL"},
            {"type": "check", "selector": "#terms", "value": "true"},
            {"type": "check", "selector": "#news", "value": "false"},
        ]
    )

    content = render_flow(flow).content

    assert '.selectOption("NL")' in content
    assert '.check();' in content
    assert '.uncheck();' in content


def test_a_submit_presses_enter_rather_than_clicking_the_form() -> None:
    """The recorded element is the form; Playwright has no form.submit() that
    waits properly."""
    content = render_flow(_flow(steps=[{"type": "submit", "selector": "#login"}])).content

    assert '.press("Enter");' in content


def test_an_unsupported_step_is_commented_not_dropped() -> None:
    """A silently missing step makes the spec pass for the wrong reason."""
    content = render_flow(_flow(steps=[{"type": "drag", "selector": "#x"}])).content

    assert "Unsupported step" in content
    assert "drag" in content


# -------------------------------------------------------------------- secrets


def test_a_redacted_field_reads_from_the_environment() -> None:
    spec = render_flow(_flow())

    assert "QAGENT_SECRET_INPUT_TYPE_PASSWORD" in spec.content
    assert spec.required_env == ["QAGENT_SECRET_INPUT_TYPE_PASSWORD"]


def test_the_secret_helper_throws_rather_than_defaulting_to_empty() -> None:
    """A spec that silently submits an empty password reports an auth bug that
    does not exist."""
    content = render_flow(_flow()).content

    assert "throw new Error" in content
    assert 'value ?? ""' not in content


def test_a_flow_with_no_secrets_needs_no_environment() -> None:
    flow = _flow(steps=[{"type": "click", "selector": "#go"}], requires_secrets=[])

    assert render_flow(flow).required_env == []


# --------------------------------------------------------------------- safety


def test_a_hostile_selector_becomes_a_string_literal() -> None:
    """The selector came from a page QAgent does not control."""
    hostile = '#x"); process.exit(1); ("'
    content = render_flow(_flow(steps=[{"type": "click", "selector": hostile}])).content

    # The payload appears only inside a quoted literal, and the quote that
    # would have closed it is escaped.
    assert "process.exit(1)" in content
    assert '\\"' in content
    assert "\n  process.exit" not in content


def test_a_hostile_value_is_escaped() -> None:
    payload = 'x");\nawait page.close();//'
    content = render_flow(
        _flow(steps=[{"type": "fill", "selector": "#a", "value": payload}])
    ).content

    line = next(line for line in content.splitlines() if ".fill(" in line)

    # The whole payload lives on one physical line: a real newline would have
    # ended the string literal and left the rest as executable code.
    assert line.count("\n") == 0
    assert 'await page.close();//' in line
    # And it round-trips as data, not code: the quote that would have closed
    # the literal is escaped.
    literal = re.search(r"\.fill\((.*)\);$", line).group(1)
    assert json.loads(literal) == payload


def test_a_backslash_in_a_selector_survives() -> None:
    content = render_flow(_flow(steps=[{"type": "click", "selector": r"#a\.b"}])).content

    assert r"#a\\.b" in content


# ------------------------------------------------------------------ assertions


def test_every_spec_asserts_no_uncaught_exception() -> None:
    """A recording replayed with no assertions is a click script."""
    content = render_flow(_flow()).content

    assert 'page.on("pageerror"' in content
    assert "expect(pageErrors" in content


def test_errors_seen_while_recording_are_carried_into_the_spec() -> None:
    flow = _flow(diagnostics=["console_error: TypeError: cart is undefined"])

    content = render_flow(flow).content

    assert "Observed while recording" in content
    assert "cart is undefined" in content


def test_a_fragile_selector_is_flagged_in_the_file_itself() -> None:
    """Whoever debugs this in six months is reading the file, not a transcript."""
    flow = _flow(steps=[{"type": "click", "selector": "div > button", "fragile": True}])

    content = render_flow(flow).content

    assert "Fragile selector" in content
    assert "data-testid" in content


def test_the_base_url_is_overridable() -> None:
    content = render_flow(_flow()).content

    assert "process.env.QAGENT_BASE_URL" in content
    assert '"http://shop.test/login"' in content


def test_an_empty_flow_still_produces_a_valid_spec() -> None:
    spec = render_flow(_flow(steps=[]))

    assert spec.step_count == 0
    assert "test(" in spec.content
    assert "expect(pageErrors" in spec.content


# ------------------------------------------------------------------- writing


def test_emit_writes_the_spec(tmp_path: Path) -> None:
    spec = emit(_flow(), out_dir=tmp_path)

    written = (tmp_path / spec.path).read_text(encoding="utf-8")
    assert written == spec.content
    assert spec.path.endswith(".spec.ts")


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    emit(_flow(), out_dir=tmp_path, dry_run=True)

    assert list(tmp_path.glob("*.ts")) == []


# ----------------------------------------------------------------------- cli


def _session_file(tmp_path: Path) -> Path:
    document = {
        "start_url": "http://shop.test/login",
        "actions": [
            {"type": "fill", "target": {"selector": "[data-testid='email']"}, "value": "a@b.c"},
            {"type": "click", "target": {"selector": "#go"}},
        ],
        "requests": [],
    }
    path = tmp_path / "session.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_record_import_emits_a_playwright_spec(tmp_path: Path) -> None:
    out = tmp_path / "e2e"

    result = runner.invoke(
        app,
        ["record-import", str(_session_file(tmp_path)), "--playwright", str(out)],
    )

    assert result.exit_code == 0
    written = list(out.glob("*.spec.ts"))
    assert written
    assert "page.locator" in written[0].read_text(encoding="utf-8")


def test_record_import_names_the_secrets_the_spec_needs(tmp_path: Path) -> None:
    document = {
        "start_url": "http://shop.test/login",
        "actions": [
            {"type": "fill", "target": {"selector": "#pw"}, "value": None, "redacted": True}
        ],
        "requests": [],
    }
    path = tmp_path / "s.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    result = runner.invoke(
        app, ["record-import", str(path), "--playwright", str(tmp_path / "e2e")]
    )

    assert "set before running" in result.output
    assert "QAGENT_SECRET_PW" in result.output
