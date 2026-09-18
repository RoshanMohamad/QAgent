"""The Project Analyst agent: composes stack detection, endpoint discovery and
the module tree into the single report CLAUDE.md section 8 (agent 1) describes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qagent.modules.analyzer.analyst import analyze_repository
from qagent.modules.discovery.openapi import EndpointSpec


def _write(root: Path, rel: str, content: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_missing_directory_raises() -> None:
    with pytest.raises(NotADirectoryError):
        analyze_repository(Path("/definitely/not/a/real/path/xyz"))


def test_falls_back_to_route_parser_when_no_endpoints_supplied(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app.py",
        "from fastapi import FastAPI\napp = FastAPI()\n\n"
        '@app.get("/orders")\ndef list_orders():\n    ...\n',
    )

    analysis = analyze_repository(tmp_path)

    assert analysis.endpoint_source == "route_parser"
    assert any(m.name == "orders" for m in analysis.tree.backend)


def test_no_routes_found_records_warning(tmp_path: Path) -> None:
    _write(tmp_path, "README.md", "nothing to see here\n")

    analysis = analyze_repository(tmp_path)

    assert analysis.endpoint_source == "none"
    assert analysis.tree.backend == []
    assert any("route parsing" in w for w in analysis.warnings)


def test_explicit_endpoints_are_used_instead_of_parsing(tmp_path: Path) -> None:
    # A Python route decorator is on disk, but real endpoints are supplied --
    # the parser must not run, and only the supplied endpoints show up.
    _write(
        tmp_path,
        "app.py",
        '@app.get("/should-not-appear")\ndef ignored():\n    ...\n',
    )
    endpoints = [EndpointSpec(method="GET", path="/orders", risk_score=0.3)]

    analysis = analyze_repository(tmp_path, endpoints=endpoints)

    assert analysis.endpoint_source == "openapi"
    module_names = {m.name for m in analysis.tree.backend}
    assert module_names == {"orders"}


def test_no_recognised_manifest_records_warning(tmp_path: Path) -> None:
    _write(tmp_path, "README.md", "hello\n")

    analysis = analyze_repository(tmp_path)

    assert any("manifest" in w for w in analysis.warnings)


def test_summary_counts_match_tree(tmp_path: Path) -> None:
    _write(tmp_path, "app/page.tsx")
    endpoints = [
        EndpointSpec(method="GET", path="/auth/login", risk_score=0.9),
        EndpointSpec(method="GET", path="/products", risk_score=0.1),
    ]

    analysis = analyze_repository(tmp_path, endpoints=endpoints)
    summary = analysis.summary()

    assert summary["endpoint_count"] == 2
    assert summary["frontend_route_count"] == 1
    assert summary["risky_component_count"] == 1  # auth only


def test_to_dict_is_json_serialisable(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", json.dumps({"dependencies": {"react": "19.0.0"}}))
    endpoints = [EndpointSpec(method="GET", path="/health", risk_score=0.0)]

    analysis = analyze_repository(tmp_path, endpoints=endpoints)

    # Round-trips cleanly: this is exactly what gets persisted into Project.stack
    # and returned by the API/CLI, so nothing in it may need special encoding.
    json.dumps(analysis.to_dict())
