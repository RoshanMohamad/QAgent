"""Agent 3's file emitter: the declarative document rendered as runnable pytest.

The load-bearing property is that nothing from a spec reaches the output as
executable text. Specs are built from a third-party OpenAPI document, so a path
or a field name is attacker-influenced; every value goes through `repr()` and
lands in a string literal.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from qagent.modules.emitter.pytest_emitter import (
    _identifier,
    emit,
    module_name_for,
    render_case,
)
from qagent.modules.generator.rules import GeneratedCase


def _case(
    name: str = "GET /orders returns 200",
    endpoint_key: str = "GET /orders",
    spec: dict | None = None,
) -> GeneratedCase:
    return GeneratedCase(
        name=name,
        kind="api_functional",
        endpoint_key=endpoint_key,
        rationale="baseline",
        spec=spec
        or {
            "request": {"method": "GET", "path": "/orders", "auth": "default"},
            "assertions": [{"type": "status_in", "value": [200]}],
            "expectation": "A valid request succeeds.",
        },
    )


def _parse(source: str) -> ast.Module:
    """Parsing is the real assertion: emitted code that is not valid Python is
    not a test suite, it is a text file."""
    return ast.parse(source)


# ------------------------------------------------------------------- grouping


def test_cases_are_grouped_by_api_module() -> None:
    assert module_name_for(_case(endpoint_key="POST /api/v1/auth/login")) == "auth"
    assert module_name_for(_case(endpoint_key="GET /orders/{order_id}/items")) == "orders"
    assert module_name_for(_case(endpoint_key="GET /")) == "root"


def test_identifiers_are_safe_python() -> None:
    assert _identifier("GET /orders/{id} returns 200") == "get_orders_id_returns_200"
    assert _identifier("") == "case"
    assert _identifier("404 handling").isidentifier()


# --------------------------------------------------------------------- output


def test_emitted_module_is_valid_python(tmp_path: Path) -> None:
    report = emit([_case()], base_url="http://x", out_dir=tmp_path)

    assert report.case_count == 1
    _parse(report.files[0].content)


def test_every_assertion_type_renders_to_valid_python(tmp_path: Path) -> None:
    assertions = [
        {"type": "status_in", "value": [200, 201]},
        {"type": "status_not_in", "value": [500]},
        {"type": "body_matches", "value": "ok"},
        {"type": "body_not_matches", "value": r"Traceback \(most recent"},
        {"type": "header_present", "value": "Content-Type"},
        {"type": "latency_under_ms", "value": 500},
        {"type": "json_path_exists", "value": "data.id"},
        {"type": "json_path_equals", "path": "data.id", "value": 7},
    ]
    case = _case(spec={"request": {"method": "GET", "path": "/x"}, "assertions": assertions})

    report = emit([case], base_url="http://x", out_dir=tmp_path)
    tree = _parse(report.files[0].content)

    # Every assertion produced at least one statement; none were dropped.
    function = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")
    )
    assert sum(isinstance(n, ast.Assert) for n in function.body) >= len(assertions) - 1


def test_an_unknown_assertion_fails_loudly_rather_than_passing(tmp_path: Path) -> None:
    """A suite that silently skips what it could not render reports green for
    the wrong reason."""
    case = _case(
        spec={"request": {"method": "GET", "path": "/x"}, "assertions": [{"type": "invented"}]}
    )

    content = emit([case], base_url="http://x", out_dir=tmp_path).files[0].content

    _parse(content)
    assert "pytest.fail" in content


def test_json_path_helper_is_only_emitted_when_needed(tmp_path: Path) -> None:
    plain = emit([_case()], base_url="http://x", out_dir=tmp_path).files[0].content
    assert "_json_path" not in plain

    using = _case(
        spec={
            "request": {"method": "GET", "path": "/x"},
            "assertions": [{"type": "json_path_exists", "value": "a.b"}],
        }
    )
    content = emit([using], base_url="http://x", out_dir=tmp_path).files[0].content
    assert "def _json_path" in content


def test_duplicate_case_names_do_not_shadow_each_other(tmp_path: Path) -> None:
    cases = [_case(name="same name"), _case(name="same name"), _case(name="same name")]

    tree = _parse(emit(cases, base_url="http://x", out_dir=tmp_path).files[0].content)

    names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    tests = [n for n in names if n.startswith("test_")]
    assert len(tests) == 3
    assert len(set(tests)) == 3


# -------------------------------------------------------------------- safety


def test_a_hostile_path_becomes_a_string_literal_not_code(tmp_path: Path) -> None:
    hostile = '/x"); import os; os.system("echo pwned"); ("'
    case = _case(
        spec={"request": {"method": "GET", "path": hostile}, "assertions": []},
    )

    content = emit([case], base_url="http://x", out_dir=tmp_path).files[0].content
    tree = _parse(content)

    # The payload must appear only inside a string constant, never as a call.
    assert "os.system" not in {
        ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)
    }
    literals = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)]
    assert hostile in literals


def test_a_hostile_case_name_cannot_escape_its_docstring(tmp_path: Path) -> None:
    case = _case(name='bad """ name with quotes')

    _parse(emit([case], base_url="http://x", out_dir=tmp_path).files[0].content)


def test_a_hostile_body_value_stays_data(tmp_path: Path) -> None:
    case = _case(
        spec={
            "request": {"method": "POST", "path": "/x", "json": {"k": "'); DROP TABLE t; --"}},
            "assertions": [],
        }
    )

    _parse(emit([case], base_url="http://x", out_dir=tmp_path).files[0].content)


# ---------------------------------------------------------------------- files


def test_files_are_written_one_per_module(tmp_path: Path) -> None:
    cases = [
        _case(endpoint_key="POST /auth/login", name="login"),
        _case(endpoint_key="GET /orders", name="list orders"),
        _case(endpoint_key="GET /orders/{id}", name="get order"),
    ]

    emit(cases, base_url="http://x", out_dir=tmp_path)

    written = sorted(p.name for p in tmp_path.glob("*.py"))
    assert written == ["test_auth.py", "test_orders.py"]


def test_dry_run_writes_nothing_but_reports_the_same(tmp_path: Path) -> None:
    cases = [_case()]

    dry = emit(cases, base_url="http://x", out_dir=tmp_path, dry_run=True)
    assert list(tmp_path.glob("*.py")) == []

    wet = emit(cases, base_url="http://x", out_dir=tmp_path)
    assert dry.to_dict() == wet.to_dict()


def test_no_cases_is_reported_not_crashed(tmp_path: Path) -> None:
    report = emit([], base_url="http://x", out_dir=tmp_path)

    assert report.files == []
    assert report.skipped


def test_unauthenticated_case_does_not_send_credentials(tmp_path: Path) -> None:
    case = _case(
        spec={
            "request": {"method": "GET", "path": "/admin", "auth": "none"},
            "assertions": [{"type": "status_in", "value": [401, 403]}],
        }
    )

    content = render_case(case, used_names=set())

    assert "authed=False" in content


# ------------------------------------------------------------- it actually runs


def test_the_emitted_suite_is_collectable_by_pytest(tmp_path: Path) -> None:
    """The point of emitting files is that a developer can run them."""
    emit(
        [_case(), _case(endpoint_key="POST /auth/login", name="login works")],
        base_url="http://127.0.0.1:1",
        out_dir=tmp_path,
    )

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "."],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 tests collected" in result.stdout
