"""HTTP test execution.

The runner is the component that reaches out to a user-supplied address, which makes
it the platform's server-side request forgery surface. ``guard_target`` is therefore
not optional decoration: without it, a project whose base URL is
http://169.254.169.254/ turns QAgent into a credential exfiltration tool for whatever
cloud it runs on.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlsplit

import httpx

from qagent.modules.llm.safety import scrub
from qagent.modules.runner.assertions import evaluate_all

logger = logging.getLogger(__name__)

MAX_BODY_CHARS = 20_000

#: Addresses that must never be reachable from a test run in a hosted deployment.
_BLOCKED_NETS = [
    ipaddress.ip_network("169.254.0.0/16"),  # link-local, cloud metadata
    ipaddress.ip_network("127.0.0.0/8"),  # loopback
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


class TargetRejected(RuntimeError):
    """The requested target is not permitted by the egress policy."""


def guard_target(url: str, *, allow_private: bool, allowlist: list[str] | None = None) -> None:
    """Reject targets that egress policy forbids.

    ``allow_private`` is true only in local development, where the system under test
    genuinely is on localhost. In a hosted deployment it must be false and the
    allowlist carries the customer's approved hosts.
    """
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise TargetRejected(f"target has no host: {url!r}")

    if parsed.scheme not in {"http", "https"}:
        raise TargetRejected(f"unsupported scheme '{parsed.scheme}'")

    if allowlist and host not in allowlist:
        raise TargetRejected(f"host '{host}' is not in the egress allowlist")

    if allow_private:
        return

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise TargetRejected(f"cannot resolve '{host}': {exc}") from exc

    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        for net in _BLOCKED_NETS:
            if address.version == net.version and address in net:
                raise TargetRejected(
                    f"'{host}' resolves to {address}, which is inside blocked range {net}"
                )


@dataclass
class RunnerConfig:
    base_url: str
    default_headers: dict[str, str] = field(default_factory=dict)
    auth_headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 30.0
    allow_private: bool = True
    allowlist: list[str] = field(default_factory=list)


@dataclass
class ExecutionOutcome:
    passed: bool
    status: str  # passed | failed | error
    duration_ms: int
    request: dict
    response: dict
    assertions: list[dict]
    failure_message: str | None = None


def _build_path(spec_request: dict) -> str:
    path = spec_request.get("path", "/")
    for name, value in (spec_request.get("path_params") or {}).items():
        path = path.replace("{" + name + "}", str(value))
    return path


def _capture_response(response: httpx.Response, duration_ms: int) -> dict:
    text = response.text or ""
    truncated = len(text) > MAX_BODY_CHARS
    body_text = scrub(text[:MAX_BODY_CHARS])

    body_json = None
    if "json" in response.headers.get("content-type", "").lower():
        try:
            body_json = response.json()
        except ValueError:
            body_json = None

    return {
        "status": response.status_code,
        "headers": {k: scrub(v) for k, v in response.headers.items()},
        "body_text": body_text,
        "body_json": body_json,
        "truncated": truncated,
        "duration_ms": duration_ms,
    }


class ApiTestRunner:
    """Executes declarative API test specs against one environment."""

    def __init__(self, config: RunnerConfig) -> None:
        self.config = config
        guard_target(
            config.base_url,
            allow_private=config.allow_private,
            allowlist=config.allowlist or None,
        )
        self._client = httpx.Client(
            base_url=config.base_url.rstrip("/"),
            timeout=config.timeout_seconds,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ApiTestRunner:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _headers(self, spec_request: dict) -> dict[str, str]:
        headers = dict(self.config.default_headers)
        if spec_request.get("auth", "default") != "none":
            headers.update(self.config.auth_headers)
        headers.update(spec_request.get("headers") or {})
        headers.setdefault("user-agent", "QAgent/0.1 (+automated quality checks)")
        return headers

    def execute(self, spec: dict) -> ExecutionOutcome:
        spec_request = spec.get("request", {})
        path = _build_path(spec_request)
        method = spec_request.get("method", "GET").upper()

        # Re-guard per request: a path could otherwise be an absolute URL.
        if urlsplit(path).scheme:
            try:
                guard_target(
                    path,
                    allow_private=self.config.allow_private,
                    allowlist=self.config.allowlist or None,
                )
            except TargetRejected as exc:
                return ExecutionOutcome(
                    False,
                    "error",
                    0,
                    {"method": method, "path": path},
                    {},
                    [],
                    f"target rejected: {exc}",
                )

        recorded_request = {
            "method": method,
            "path": path,
            "query": spec_request.get("query") or {},
            "json": spec_request.get("json"),
            "auth": spec_request.get("auth", "default"),
        }

        started = time.perf_counter()
        try:
            response = self._client.request(
                method,
                path,
                params=spec_request.get("query") or None,
                json=spec_request.get("json"),
                headers=self._headers(spec_request),
            )
        except httpx.TimeoutException as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            return ExecutionOutcome(
                False,
                "error",
                duration_ms,
                recorded_request,
                {"error": "timeout", "duration_ms": duration_ms},
                [],
                f"request timed out after {self.config.timeout_seconds}s: {exc}",
            )
        except httpx.HTTPError as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            return ExecutionOutcome(
                False,
                "error",
                duration_ms,
                recorded_request,
                {"error": type(exc).__name__, "duration_ms": duration_ms},
                [],
                f"transport error: {exc}",
            )

        duration_ms = int((time.perf_counter() - started) * 1000)
        captured = _capture_response(response, duration_ms)
        passed, outcomes = evaluate_all(spec.get("assertions", []), captured)

        failure_message = None
        if not passed:
            failed = [o for o in outcomes if not o["passed"]]
            failure_message = "; ".join(o["message"] for o in failed)

        return ExecutionOutcome(
            passed=passed,
            status="passed" if passed else "failed",
            duration_ms=duration_ms,
            request=recorded_request,
            response=captured,
            assertions=outcomes,
            failure_message=failure_message,
        )
