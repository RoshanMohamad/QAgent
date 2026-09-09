"""OpenAPI ingestion.

Deliberately not a validating parser. Real-world documents are frequently slightly
wrong, and refusing to onboard a project because its spec has a dangling $ref would
be the wrong trade. Anything unparseable is skipped and reported, never fatal.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}

#: Paths tried when a project supplies a base URL but no explicit spec location.
COMMON_SPEC_PATHS = [
    "/openapi.json",
    "/openapi.yaml",
    "/swagger.json",
    "/v3/api-docs",
    "/api/openapi.json",
    "/api-docs",
    "/docs/openapi.json",
]

#: Path fragments that raise an endpoint's risk score. Risk drives generation order
#: so that a truncated budget still covers what matters.
_SENSITIVE_FRAGMENTS = {
    "auth": 0.30,
    "login": 0.30,
    "token": 0.25,
    "session": 0.20,
    "password": 0.30,
    "admin": 0.30,
    "user": 0.15,
    "account": 0.15,
    "role": 0.20,
    "permission": 0.20,
    "payment": 0.35,
    "checkout": 0.30,
    "order": 0.20,
    "invoice": 0.20,
    "billing": 0.25,
    "upload": 0.20,
    "file": 0.15,
    "export": 0.15,
    "import": 0.15,
    "search": 0.10,
}

_WRITE_METHODS = {"post", "put", "patch", "delete"}


@dataclass
class EndpointSpec:
    """One discovered operation, independent of storage."""

    method: str
    path: str
    operation_id: str | None = None
    summary: str | None = None
    parameters: list[dict] = field(default_factory=list)
    request_schema: dict | None = None
    responses: dict = field(default_factory=dict)
    requires_auth: bool = False
    source: str = "openapi"
    risk_score: float = 0.0

    @property
    def declared_status_codes(self) -> list[int]:
        codes = []
        for key in self.responses:
            try:
                codes.append(int(key))
            except (TypeError, ValueError):
                continue
        return sorted(codes)

    @property
    def success_status(self) -> int:
        """The status a valid request should produce."""
        ok = [c for c in self.declared_status_codes if 200 <= c < 300]
        if ok:
            return ok[0]
        return 201 if self.method.lower() == "post" else 200

    @property
    def path_params(self) -> list[dict]:
        return [p for p in self.parameters if p.get("in") == "path"]

    @property
    def required_body_fields(self) -> list[str]:
        if not self.request_schema:
            return []
        return list(self.request_schema.get("required", []))

    def key(self) -> str:
        return f"{self.method.upper()} {self.path}"


def score_risk(method: str, path: str, requires_auth: bool) -> float:
    """Heuristic 0..1 priority. Cheap, explainable, and good enough to order work."""
    score = 0.1
    lowered = path.lower()

    for fragment, weight in _SENSITIVE_FRAGMENTS.items():
        if fragment in lowered:
            score += weight

    if method.lower() in _WRITE_METHODS:
        score += 0.15
    if method.lower() == "delete":
        score += 0.10
    # An endpoint taking an identifier is a candidate for broken object-level
    # authorisation, which is the most common serious API defect in practice.
    if "{" in path:
        score += 0.15
    if requires_auth:
        score += 0.10

    return round(min(score, 1.0), 3)


def _resolve_ref(ref: str, document: dict) -> dict:
    """Resolve a local $ref. Remote refs are not followed by design."""
    if not ref.startswith("#/"):
        return {}
    node: Any = document
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return {}
        node = node[part]
    return node if isinstance(node, dict) else {}


def _deref(node: Any, document: dict, depth: int = 0) -> Any:
    """Inline local $refs, with a depth cap so recursive schemas terminate."""
    if depth > 6:
        return {}
    if isinstance(node, dict):
        if "$ref" in node:
            return _deref(_resolve_ref(node["$ref"], document), document, depth + 1)
        return {k: _deref(v, document, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [_deref(item, document, depth + 1) for item in node]
    return node


def _request_body_schema(operation: dict, document: dict) -> dict | None:
    body = operation.get("requestBody")
    if not isinstance(body, dict):
        return None
    content = _deref(body, document).get("content", {})
    for media in ("application/json", "application/*+json"):
        if media in content:
            schema = content[media].get("schema")
            return schema if isinstance(schema, dict) else None
    return None


def _operation_requires_auth(operation: dict, document: dict) -> bool:
    if "security" in operation:
        return bool(operation["security"])
    return bool(document.get("security"))


def parse_openapi(document: dict) -> list[EndpointSpec]:
    """Flatten an OpenAPI document into endpoint specs."""
    endpoints: list[EndpointSpec] = []
    paths = document.get("paths")
    if not isinstance(paths, dict):
        logger.warning("openapi document has no usable paths object")
        return endpoints

    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        shared_params = _deref(item.get("parameters", []), document) or []

        for method, operation in item.items():
            if method.lower() not in HTTP_METHODS or not isinstance(operation, dict):
                continue

            try:
                params = list(shared_params) + list(
                    _deref(operation.get("parameters", []), document) or []
                )
                requires_auth = _operation_requires_auth(operation, document)
                endpoints.append(
                    EndpointSpec(
                        method=method.upper(),
                        path=path,
                        operation_id=operation.get("operationId"),
                        summary=operation.get("summary") or operation.get("description"),
                        parameters=[p for p in params if isinstance(p, dict)],
                        request_schema=_request_body_schema(operation, document),
                        responses=_deref(operation.get("responses", {}), document) or {},
                        requires_auth=requires_auth,
                        risk_score=score_risk(method, path, requires_auth),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one bad operation must not stop discovery
                logger.warning("skipping %s %s: %s", method, path, exc)

    endpoints.sort(key=lambda e: (-e.risk_score, e.path, e.method))
    return endpoints


def load_document(raw: str | bytes) -> dict:
    """Parse JSON, falling back to YAML when available."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        import yaml

        loaded = yaml.safe_load(text)
        return loaded if isinstance(loaded, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not parse openapi document: %s", exc)
        return {}


def fetch_spec(
    base_url: str, explicit_url: str | None = None, timeout: float = 15.0
) -> tuple[dict, str | None]:
    """Retrieve an OpenAPI document, probing common locations if needed.

    Returns the document and the URL it came from. An empty document means
    discovery must fall back to another source rather than that the project failed.
    """
    candidates = (
        [explicit_url] if explicit_url else [base_url.rstrip("/") + p for p in COMMON_SPEC_PATHS]
    )

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for url in candidates:
            if not url:
                continue
            try:
                response = client.get(url)
            except httpx.HTTPError as exc:
                logger.debug("spec probe failed %s: %s", url, exc)
                continue
            if response.status_code != 200:
                continue
            document = load_document(response.content)
            if document.get("paths"):
                logger.info("discovered openapi document at %s", url)
                return document, url

    return {}, None
