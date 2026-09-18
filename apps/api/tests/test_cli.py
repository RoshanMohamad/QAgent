"""CLI wiring: `explore --interact` should call the interactive explorer with
the right policy, not the plain crawler, and reflect defects in the exit code.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from qagent.cli import app
from qagent.modules.explorer.actions import Action, ActionOutcome, ActionType, ElementRef
from qagent.modules.explorer.crawler import StateGraph
from qagent.modules.explorer.interact import InteractionGraph, InteractionNode

runner = CliRunner()


def _ref() -> ElementRef:
    return ElementRef(
        selector='button[data-testid="add-btn"]', tag="button", role=None, input_type=None,
        text="Add to cart", testid="add-btn",
    )


def test_explore_without_interact_calls_plain_crawler(monkeypatch) -> None:
    calls: dict = {}

    def fake_explore(**kwargs):
        calls.update(kwargs)
        return StateGraph(root="http://x/")

    monkeypatch.setattr("qagent.modules.explorer.crawler.explore", fake_explore)

    result = runner.invoke(app, ["explore", "--url", "http://x/"])

    assert result.exit_code == 0
    assert calls["base_url"] == "http://x/"


def test_explore_interact_calls_interactive_explorer_with_policy(monkeypatch) -> None:
    calls: dict = {}

    def fake_explore_interactive(**kwargs):
        calls.update(kwargs)
        return InteractionGraph(root="http://x/")

    monkeypatch.setattr(
        "qagent.modules.explorer.interact.explore_interactive", fake_explore_interactive
    )

    result = runner.invoke(
        app, ["explore", "--url", "http://x/", "--interact", "--max-actions", "10"]
    )

    assert result.exit_code == 0
    assert calls["base_url"] == "http://x/"
    assert calls["max_total_actions"] == 10
    assert calls["policy"].allow_destructive is False


def test_explore_interact_allow_destructive_flag_propagates(monkeypatch) -> None:
    calls: dict = {}

    def fake_explore_interactive(**kwargs):
        calls.update(kwargs)
        return InteractionGraph(root="http://x/")

    monkeypatch.setattr(
        "qagent.modules.explorer.interact.explore_interactive", fake_explore_interactive
    )

    runner.invoke(app, ["explore", "--url", "http://x/", "--interact", "--allow-destructive"])

    assert calls["policy"].allow_destructive is True


def test_explore_interact_exits_nonzero_when_action_finds_a_defect(monkeypatch) -> None:
    graph = InteractionGraph(root="http://x/")
    node = InteractionNode(url="http://x/", depth=0, dom_fingerprint="abc")
    node.actions_taken = [
        ActionOutcome(
            action=Action(type=ActionType.CLICK, target=_ref()),
            ok=True,
            resulting_url="http://x/",
            page_errors=["TypeError: boom"],
        )
    ]
    graph.add_node(node)

    monkeypatch.setattr(
        "qagent.modules.explorer.interact.explore_interactive", lambda **kwargs: graph
    )

    result = runner.invoke(app, ["explore", "--url", "http://x/", "--interact"])

    assert result.exit_code == 1


def test_explore_interact_and_check_both_run(monkeypatch) -> None:
    """Regression guard: --interact used to return before --check's page-load
    checks ever ran, silently dropping the flag when both were passed."""
    graph = InteractionGraph(root="http://x/")
    graph.add_node(InteractionNode(url="http://x/", depth=0, dom_fingerprint="abc"))

    monkeypatch.setattr(
        "qagent.modules.explorer.interact.explore_interactive", lambda **kwargs: graph
    )

    check_calls: dict = {}

    class _FakeCheckResult:
        failed: list = []
        checks: list = []

        def summary(self):
            return {"base_url": "http://x/", "total": 0, "passed": 0, "failed": 0, "duration_s": 0}

    def fake_run_browser_checks(**kwargs):
        check_calls.update(kwargs)
        return _FakeCheckResult()

    monkeypatch.setattr(
        "qagent.modules.browser.runner.run_browser_checks", fake_run_browser_checks
    )

    result = runner.invoke(app, ["explore", "--url", "http://x/", "--interact", "--check"])

    assert result.exit_code == 0
    assert check_calls  # run_browser_checks was actually invoked, not skipped


def test_explore_interact_no_fail_on_defect_exits_zero(monkeypatch) -> None:
    graph = InteractionGraph(root="http://x/")
    node = InteractionNode(url="http://x/", depth=0, dom_fingerprint="abc")
    node.actions_taken = [
        ActionOutcome(
            action=Action(type=ActionType.CLICK, target=_ref()),
            ok=True,
            resulting_url="http://x/",
            page_errors=["TypeError: boom"],
        )
    ]
    graph.add_node(node)

    monkeypatch.setattr(
        "qagent.modules.explorer.interact.explore_interactive", lambda **kwargs: graph
    )

    result = runner.invoke(
        app, ["explore", "--url", "http://x/", "--interact", "--no-fail-on-defect"]
    )

    assert result.exit_code == 0


def test_analyze_rejects_non_directory(tmp_path) -> None:
    missing = tmp_path / "does-not-exist"

    result = runner.invoke(app, ["analyze", "--repo", str(missing)])

    assert result.exit_code == 2


def test_analyze_writes_json_report(tmp_path) -> None:
    (tmp_path / "package.json").write_text('{"dependencies": {"react": "19.0.0"}}', encoding="utf-8")
    (tmp_path / "app.py").write_text(
        '@app.get("/orders")\ndef list_orders():\n    ...\n', encoding="utf-8"
    )
    output = tmp_path / "analysis.json"

    result = runner.invoke(app, ["analyze", "--repo", str(tmp_path), "--json", str(output)])

    assert result.exit_code == 0
    assert output.exists()
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert "React" in [t["name"] for t in payload["stack"]["technologies"]]
    assert payload["endpoint_source"] == "route_parser"


def test_analyze_uses_openapi_discovery_when_spec_given(tmp_path, monkeypatch) -> None:
    from qagent.modules.discovery.openapi import EndpointSpec

    (tmp_path / "app.py").write_text(
        '@app.get("/should-not-be-used")\ndef ignored():\n    ...\n', encoding="utf-8"
    )

    def fake_discover(base_url, openapi_url, repo_path):
        return [EndpointSpec(method="GET", path="/orders", risk_score=0.1)], "explicit"

    monkeypatch.setattr("qagent.pipeline.discover", fake_discover)

    result = runner.invoke(
        app, ["analyze", "--repo", str(tmp_path), "--spec", "http://x/openapi.json"]
    )

    assert result.exit_code == 0
    assert "should-not-be-used" not in result.output
    assert "orders" in result.output
