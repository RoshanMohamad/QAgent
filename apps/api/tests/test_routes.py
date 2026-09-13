"""Static route discovery: parsing endpoints out of source when a project has no
OpenAPI document.
"""

from __future__ import annotations

from pathlib import Path

from qagent.modules.discovery.routes import parse_routes
from qagent.pipeline import discover


def test_parses_fastapi_style_decorators(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text(
        """
from fastapi import FastAPI
app = FastAPI()

@app.get("/users/{user_id}")
async def get_user(user_id: int):
    ...

@app.post("/users")
def create_user():
    ...
""",
        encoding="utf-8",
    )

    endpoints = parse_routes(tmp_path)
    keys = {e.key() for e in endpoints}
    assert "GET /users/{user_id}" in keys
    assert "POST /users" in keys

    user = next(e for e in endpoints if e.key() == "GET /users/{user_id}")
    assert user.source == "route_parser"
    assert user.path_params[0]["name"] == "user_id"
    assert user.path_params[0]["schema"]["type"] == "integer"


def test_parses_flask_style_route_decorator(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        """
@app.route("/orders/<int:order_id>")
def legacy_style():
    ...

@app.route("/orders", methods=["POST", "GET"])
def orders():
    ...
""",
        encoding="utf-8",
    )

    endpoints = parse_routes(tmp_path)
    keys = {e.key() for e in endpoints}
    assert "POST /orders" in keys
    assert "GET /orders" in keys


def test_parses_express_style_calls(tmp_path: Path) -> None:
    (tmp_path / "routes.js").write_text(
        """
const router = require('express').Router();

router.get('/products/:id', (req, res) => {});
router.post("/products", (req, res) => {});
app.delete(`/products/:id`, handler);
""",
        encoding="utf-8",
    )

    endpoints = parse_routes(tmp_path)
    keys = {e.key() for e in endpoints}
    assert "GET /products/{id}" in keys
    assert "POST /products" in keys
    assert "DELETE /products/{id}" in keys


def test_ignores_skipped_directories(tmp_path: Path) -> None:
    skipped = tmp_path / "node_modules" / "some-dep"
    skipped.mkdir(parents=True)
    (skipped / "index.js").write_text("app.get('/internal', h);", encoding="utf-8")

    (tmp_path / "index.js").write_text("app.get('/real', h);", encoding="utf-8")

    endpoints = parse_routes(tmp_path)
    keys = {e.key() for e in endpoints}
    assert "GET /real" in keys
    assert "GET /internal" not in keys


def test_deduplicates_repeated_routes(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text('@app.get("/x")\ndef a(): ...\n', encoding="utf-8")
    (tmp_path / "b.py").write_text('@app.get("/x")\ndef b(): ...\n', encoding="utf-8")

    endpoints = parse_routes(tmp_path)
    assert len([e for e in endpoints if e.key() == "GET /x"]) == 1


def test_returns_empty_for_missing_directory(tmp_path: Path) -> None:
    assert parse_routes(tmp_path / "does-not-exist") == []


def test_skips_unparsable_python_file(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text("def broken(:\n    pass", encoding="utf-8")
    assert parse_routes(tmp_path) == []


def test_discover_falls_back_to_route_parser_when_no_spec(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "main.py").write_text('@app.get("/health")\ndef health(): ...\n', encoding="utf-8")

    monkeypatch.setattr(
        "qagent.pipeline.fetch_spec", lambda base_url, openapi_url, timeout=15.0: ({}, None)
    )

    endpoints, source = discover("http://example.test", None, tmp_path)
    assert source == f"route-parser:{tmp_path}"
    assert any(e.key() == "GET /health" for e in endpoints)


def test_discover_prefers_openapi_over_route_parser(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "main.py").write_text('@app.get("/from-source")\ndef h(): ...\n', encoding="utf-8")

    document = {"paths": {"/from-spec": {"get": {}}}}
    monkeypatch.setattr(
        "qagent.pipeline.fetch_spec",
        lambda base_url, openapi_url, timeout=15.0: (document, "http://example.test/openapi.json"),
    )

    endpoints, source = discover("http://example.test", None, tmp_path)
    assert source == "http://example.test/openapi.json"
    assert [e.path for e in endpoints] == ["/from-spec"]
