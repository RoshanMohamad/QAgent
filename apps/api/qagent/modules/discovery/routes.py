"""Static route discovery: find endpoints in a repository that has no OpenAPI
document, by reading source instead of asking the running app.

This is deliberately best-effort and deliberately narrow. A framework's own
routing table is the ground truth; static parsing recovers only what's
expressible as a literal route string in a recognised call shape. Anything
computed — a dynamically built path, a route table loaded from a database or
config file, a decorator factory — is invisible here and always will be.
When parsing finds nothing, the caller reports that honestly rather than
silently proceeding as if an empty endpoint list meant an app with no API.

Supported today: FastAPI/Flask-style Python decorators (``@app.get("/x")``,
``@router.route("/x", methods=["POST"])``) and Express-style JS/TS calls
(``app.post("/x", ...)``, ``router.get("/x", ...)``).
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

from qagent.modules.discovery.openapi import EndpointSpec, score_risk

logger = logging.getLogger(__name__)

_SKIP_DIRS = {
    "node_modules",
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    "site-packages",
}

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}

_JS_ROUTE_RE = re.compile(
    r"""\b(?:app|router)\s*\.\s*(get|post|put|patch|delete|head|options)\s*\(\s*
        ['"`]([^'"`]+)['"`]""",
    re.VERBOSE,
)

_PATH_PARAM_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

#: Param names that read as identifiers, so the generator's negative cases
#: (a malformed id, an absent-but-well-formed id) exercise int-shaped values.
_ID_LIKE = re.compile(r"(^id$|_id$|Id$)")


def _iter_source_files(repo_dir: Path, extensions: set[str]) -> list[Path]:
    files = []
    for path in repo_dir.rglob("*"):
        if path.suffix not in extensions or not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        files.append(path)
    return files


def _normalize_path(raw_path: str) -> str:
    """Express's ``:id`` becomes OpenAPI-style ``{id}``, the only convention the
    generator and risk scorer understand."""
    return re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", r"{\1}", raw_path)


def _path_params(path: str) -> list[dict]:
    params = []
    for name in _PATH_PARAM_RE.findall(path):
        schema = {"type": "integer"} if _ID_LIKE.search(name) else {"type": "string"}
        params.append({"name": name, "in": "path", "required": True, "schema": schema})
    return params


def _make_endpoint(method: str, raw_path: str, operation_id: str | None) -> EndpointSpec:
    path = _normalize_path(raw_path)
    return EndpointSpec(
        method=method.upper(),
        path=path,
        operation_id=operation_id,
        parameters=_path_params(path),
        source="route_parser",
        # requires_auth can't be inferred reliably from a decorator alone (a
        # dependency, a middleware, a decorator further up the chain could add
        # it) — false is the safer default: it under-generates auth checks
        # rather than asserting a security guarantee the source doesn't show.
        requires_auth=False,
        risk_score=score_risk(method, path, requires_auth=False),
    )


def _decorator_call(decorator: ast.expr) -> ast.Call | None:
    return decorator if isinstance(decorator, ast.Call) else None


def _string_const(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _parse_python_file(path: Path) -> list[EndpointSpec]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError) as exc:
        logger.debug("skipping unparsable python file %s: %s", path, exc)
        return []

    found: list[EndpointSpec] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue

        for decorator in node.decorator_list:
            call = _decorator_call(decorator)
            if call is None or not isinstance(call.func, ast.Attribute):
                continue

            attr_method = call.func.attr.lower()
            route_path = _string_const(call.args[0] if call.args else None)
            if route_path is None:
                continue

            if attr_method == "route":
                # Flask-style: @app.route("/x", methods=["POST", "GET"])
                methods = ["get"]
                for kw in call.keywords:
                    if kw.arg == "methods" and isinstance(kw.value, ast.List):
                        methods = [
                            m.lower()
                            for elt in kw.value.elts
                            if (m := _string_const(elt)) is not None
                        ] or methods
                for method in methods:
                    if method in _HTTP_METHODS:
                        found.append(_make_endpoint(method, route_path, node.name))
            elif attr_method in _HTTP_METHODS:
                found.append(_make_endpoint(attr_method, route_path, node.name))

    return found


def _parse_js_file(path: Path) -> list[EndpointSpec]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return [_make_endpoint(method, route, None) for method, route in _JS_ROUTE_RE.findall(text)]


def parse_routes(repo_dir: Path) -> list[EndpointSpec]:
    """Best-effort static route discovery across a repository's source tree."""
    if not repo_dir.is_dir():
        return []

    endpoints: list[EndpointSpec] = []
    for path in _iter_source_files(repo_dir, {".py"}):
        endpoints.extend(_parse_python_file(path))
    for path in _iter_source_files(repo_dir, {".js", ".ts", ".mjs", ".cjs"}):
        endpoints.extend(_parse_js_file(path))

    # The same route can legitimately appear more than once while a file is mid
    # rename, or in both a TS source file and its build output; keep one.
    deduped: dict[str, EndpointSpec] = {}
    for endpoint in endpoints:
        deduped.setdefault(endpoint.key(), endpoint)

    result = sorted(deduped.values(), key=lambda e: (-e.risk_score, e.path, e.method))
    logger.info("route parser found %d endpoint(s) under %s", len(result), repo_dir)
    return result
