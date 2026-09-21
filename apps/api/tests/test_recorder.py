"""Converting a recorded browser session into tests.

The session file comes from an extension running on a page QAgent does not
control, so the parser is treated as a trust boundary: every test here that
feeds it something malformed is asserting that a hostile or broken document
degrades rather than propagates.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from qagent.cli import app
from qagent.modules.recorder.session import (
    MAX_ACTIONS,
    parse_session,
    to_api_cases,
    to_ui_flow,
)

runner = CliRunner()


def _session(**overrides) -> dict:
    document = {
        "version": 1,
        "start_url": "http://shop.test/login",
        "actions": [
            {
                "type": "fill",
                "target": {"selector": "[data-testid='email']", "tag": "input"},
                "value": "ada@example.com",
                "url": "http://shop.test/login",
            },
            {
                "type": "fill",
                "target": {"selector": "[data-testid='password']", "tag": "input"},
                "value": None,
                "redacted": True,
            },
            {
                "type": "click",
                "target": {"selector": "button:has-text(\"Sign in\")", "tag": "button"},
            },
        ],
        "requests": [
            {"method": "POST", "url": "http://shop.test/api/login", "status": 200},
            {"method": "POST", "url": "http://shop.test/api/orders", "status": 201},
            {"method": "GET", "url": "https://analytics.example.com/t", "status": 200},
        ],
    }
    document.update(overrides)
    return document


# ------------------------------------------------------------------ parsing


def test_a_normal_session_parses() -> None:
    session = parse_session(_session())

    assert session.start_url == "http://shop.test/login"
    assert len(session.actions) == 3
    assert len(session.requests) == 3


def test_a_password_field_records_the_action_but_not_the_value() -> None:
    """A recorded password replayed in CI is a credential in a log, an
    artifact and a bug report."""
    session = parse_session(_session())

    password = next(a for a in session.actions if a.redacted)
    assert password.value is None
    assert password.selector == "[data-testid='password']"


def test_console_errors_become_diagnostics_not_steps() -> None:
    """They are evidence of a defect, not something to replay."""
    document = _session()
    document["actions"].append({"type": "console_error", "message": "TypeError: x is undefined"})

    session = parse_session(document)

    assert len(session.actions) == 3
    assert any("TypeError" in d for d in session.diagnostics)


def test_an_unknown_action_type_is_dropped_with_a_warning() -> None:
    """A hostile document must not introduce a step the emitter never saw."""
    document = _session()
    document["actions"].append({"type": "exec", "target": {"selector": "x"}})

    session = parse_session(document)

    assert all(a.type != "exec" for a in session.actions)
    assert any("exec" in w for w in session.warnings)


def test_an_action_without_a_selector_is_dropped() -> None:
    document = _session()
    document["actions"].append({"type": "click", "target": {}})

    session = parse_session(document)

    assert len(session.actions) == 3
    assert any("no selector" in w for w in session.warnings)


def test_a_positional_selector_is_flagged_as_fragile() -> None:
    document = _session(
        actions=[
            {
                "type": "click",
                "target": {"selector": "div > div > button:nth-of-type(2)", "fragile": True},
            }
        ]
    )

    session = parse_session(document)

    assert session.fragile_selectors == 1
    assert any("data-testid" in w for w in session.warnings)


def test_oversized_action_lists_are_truncated() -> None:
    document = _session(
        actions=[{"type": "click", "target": {"selector": f"#b{i}"}} for i in range(MAX_ACTIONS + 50)]
    )

    session = parse_session(document)

    assert len(session.actions) == MAX_ACTIONS
    assert any("truncated" in w for w in session.warnings)


def test_a_non_object_document_is_refused_cleanly() -> None:
    for junk in ([], "nope", 42, None):
        session = parse_session(junk)
        assert session.actions == []
        assert session.warnings


def test_junk_entries_do_not_lose_the_rest_of_the_recording() -> None:
    """Ten minutes of recording should not be lost to one bad entry."""
    document = _session()
    document["actions"].insert(0, "not a dict")
    document["requests"].insert(0, 12345)

    session = parse_session(document)

    assert len(session.actions) == 3
    assert len(session.requests) == 3


# ---------------------------------------------------------------- api cases


def test_observed_requests_become_runnable_checks() -> None:
    session = parse_session(_session())

    cases = to_api_cases(session, base_url="http://shop.test")
    paths = {c.spec["request"]["path"] for c in cases}

    assert "/api/orders" in paths
    assert all(c.generated_by == "recording" for c in cases)


def test_third_party_traffic_is_excluded() -> None:
    """Generating checks against someone else's analytics endpoint is both
    useless and rude."""
    session = parse_session(_session())

    cases = to_api_cases(session, base_url="http://shop.test")

    assert all("analytics" not in c.spec["request"]["path"] for c in cases)


def test_the_observed_status_is_asserted_when_the_body_was_recorded() -> None:
    document = _session(
        requests=[
            {
                "method": "POST",
                "url": "http://shop.test/api/orders",
                "status": 201,
                "body": {"product_id": 7, "quantity": 1},
            }
        ]
    )
    session = parse_session(document)

    orders = to_api_cases(session, base_url="http://shop.test")[0]

    assert orders.spec["assertions"][0]["type"] == "status_in"
    assert 201 in orders.spec["assertions"][0]["value"]
    assert orders.spec["request"]["json"] == {"product_id": 7, "quantity": 1}


def test_a_write_without_a_recorded_body_does_not_assert_success() -> None:
    """Replayed with no body the app correctly answers 422, so asserting 2xx
    would fail every run while the application works - a false positive, which
    is the one thing this project optimises against (ADR-0003)."""
    session = parse_session(_session())

    orders = next(
        c for c in to_api_cases(session, base_url="http://shop.test")
        if c.spec["request"]["path"] == "/api/orders"
    )

    assert {a["type"] for a in orders.spec["assertions"]} == {
        "status_not_in",
        "body_not_matches",
    }
    assert "never 5xxs" in orders.name


def test_a_get_is_replayable_without_a_recorded_body() -> None:
    """A GET carries its inputs in the URL, so replaying it is faithful even
    when no body was captured - unlike a POST."""
    document = _session(
        requests=[{"method": "GET", "url": "http://shop.test/api/cart", "status": 200}]
    )
    session = parse_session(document)

    case = to_api_cases(session, base_url="http://shop.test")[0]

    assert case.spec["assertions"][0]["type"] == "status_in"
    assert "still works" in case.name


def test_secret_fields_are_redacted_out_of_a_recorded_body() -> None:
    """The first request in almost every recorded session is a login."""
    document = _session(
        requests=[
            {
                "method": "POST",
                "url": "http://shop.test/api/login",
                "status": 200,
                "body": {
                    "email": "ada@example.com",
                    "password": "hunter2",
                    "nested": {"api_key": "sk-live-123"},
                },
            }
        ]
    )

    body = parse_session(document).requests[0].body

    assert body["email"] == "ada@example.com"
    assert body["password"] == "[redacted]"
    assert body["nested"]["api_key"] == "[redacted]"


def test_a_non_json_body_is_not_recorded() -> None:
    """A multipart upload cannot be replayed by the declarative runner, and
    storing it would generate a check that sends the wrong content type."""
    document = _session(
        requests=[
            {"method": "POST", "url": "http://shop.test/api/upload", "status": 201, "body": "----form"}
        ]
    )

    assert parse_session(document).requests[0].body is None


def test_deeply_nested_bodies_do_not_blow_the_stack() -> None:
    deep: dict = {"a": 1}
    for _ in range(200):
        deep = {"n": deep}
    document = _session(
        requests=[{"method": "POST", "url": "http://shop.test/api/x", "status": 200, "body": deep}]
    )

    assert parse_session(document).requests[0].body is not None


def test_query_parameters_are_kept() -> None:
    document = _session(
        requests=[{"method": "GET", "url": "http://shop.test/api/search?q=shoes", "status": 200}]
    )

    assert parse_session(document).requests[0].query == {"q": "shoes"}


def test_every_recorded_check_still_forbids_a_server_error() -> None:
    session = parse_session(_session())

    for case in to_api_cases(session, base_url="http://shop.test"):
        types = {a["type"] for a in case.spec["assertions"]}
        assert "status_not_in" in types


def test_logout_is_never_replayed() -> None:
    """Replaying it mid-suite invalidates the session every later check needs,
    and the failure then looks like an auth bug."""
    document = _session(
        requests=[
            {"method": "POST", "url": "http://shop.test/api/logout", "status": 200},
            {"method": "POST", "url": "http://shop.test/api/orders", "status": 201},
        ]
    )
    session = parse_session(document)

    paths = {c.spec["request"]["path"] for c in to_api_cases(session, base_url="http://shop.test")}

    assert "/api/logout" not in paths
    assert "/api/orders" in paths


def test_the_same_endpoint_is_only_checked_once() -> None:
    document = _session(
        requests=[
            {"method": "GET", "url": "http://shop.test/api/cart", "status": 200},
            {"method": "GET", "url": "http://shop.test/api/cart", "status": 200},
        ]
    )
    session = parse_session(document)

    assert len(to_api_cases(session, base_url="http://shop.test")) == 1


# ------------------------------------------------------------------ ui flow


def test_the_ui_flow_keeps_the_recorded_order() -> None:
    flow = to_ui_flow(parse_session(_session()))

    assert [s["type"] for s in flow["steps"]] == ["fill", "fill", "click"]


def test_the_flow_names_the_fields_needing_a_secret() -> None:
    flow = to_ui_flow(parse_session(_session()))

    assert flow["requires_secrets"] == ["[data-testid='password']"]


# ---------------------------------------------------------------------- cli


def test_record_import_converts_a_session(tmp_path) -> None:
    path = tmp_path / "session.json"
    path.write_text(json.dumps(_session()), encoding="utf-8")

    result = runner.invoke(
        app, ["record-import", str(path), "--url", "http://shop.test"]
    )

    assert result.exit_code == 0
    assert "/api/orders" in result.output


def test_record_import_emits_runnable_files(tmp_path) -> None:
    path = tmp_path / "session.json"
    path.write_text(json.dumps(_session()), encoding="utf-8")
    out = tmp_path / "generated"

    result = runner.invoke(
        app, ["record-import", str(path), "--url", "http://shop.test", "--out", str(out)]
    )

    assert result.exit_code == 0
    written = list(out.glob("*.py"))
    assert written
    # The emitted file must be valid Python, not merely present.
    import ast

    for file in written:
        ast.parse(file.read_text(encoding="utf-8"))


def test_record_import_reports_an_observed_console_error(tmp_path) -> None:
    document = _session()
    document["actions"].append({"type": "console_error", "message": "TypeError: boom"})
    path = tmp_path / "session.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    result = runner.invoke(app, ["record-import", str(path)])

    assert "TypeError" in result.output


def test_record_import_rejects_an_unreadable_file(tmp_path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    assert runner.invoke(app, ["record-import", str(path)]).exit_code == 2


def test_record_import_exits_nonzero_on_an_empty_session(tmp_path) -> None:
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"actions": [], "requests": []}), encoding="utf-8")

    assert runner.invoke(app, ["record-import", str(path)]).exit_code == 2
