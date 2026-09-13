"""Compose mode (ADR-0001, Phase 2).

``target_url`` mode assumes the application is already running somewhere. Compose
mode removes that assumption for the one case that's actually tractable: a
repository that ships its own ``docker-compose.yml``. QAgent brings that stack up,
waits for the target service to answer, runs the identical pipeline used by
``target_url`` mode, and tears the stack down — success or failure.

This module owns process/lifecycle concerns only. It knows nothing about
discovery, generation or triage; it hands ``run_pipeline`` a base URL and gets a
``PipelineResult`` back, so the thing this module adds is provisioning, not a
second code path to keep in sync with the first.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class ComposeError(RuntimeError):
    """The stack could not be brought up, or never became healthy in time."""


@dataclass
class ComposeConfig:
    compose_file: Path
    project_name: str
    target_service: str
    target_port: int
    health_path: str = "/"
    startup_timeout_seconds: float = 120.0
    poll_interval_seconds: float = 2.0


def _run(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    logger.info("compose: %s", " ".join(args))
    # No shell=True and no untrusted string interpolation: args is always a fixed
    # list of literals plus config values that flow into argv positions, never a
    # shell command line, so a hostile service/project name can't break out.
    return subprocess.run(  # noqa: S603 - fixed argv, not a shell
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


class ComposeStack:
    """Context manager: ``up`` on enter, ``down -v`` on exit, no matter what happened."""

    def __init__(self, config: ComposeConfig):
        self.config = config
        self._base_dir = config.compose_file.parent
        self._up = False

    def __enter__(self) -> ComposeStack:
        self.up()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.down()

    def _compose_args(self, *sub: str) -> list[str]:
        return [
            "docker",
            "compose",
            "-f",
            str(self.config.compose_file),
            "-p",
            self.config.project_name,
            *sub,
        ]

    def up(self) -> None:
        result = _run(self._compose_args("up", "-d", "--build", "--wait"), cwd=self._base_dir)
        # Tear down even a partial failure: some containers may already be running.
        self._up = True
        if result.returncode != 0:
            raise ComposeError(f"docker compose up failed: {result.stderr.strip()}")

    def down(self) -> None:
        if not self._up:
            return
        result = _run(
            self._compose_args("down", "-v", "--remove-orphans"), cwd=self._base_dir
        )
        if result.returncode != 0:
            logger.warning("docker compose down failed: %s", result.stderr.strip())
        self._up = False

    def published_port(self, service: str, container_port: int) -> int:
        result = _run(self._compose_args("port", service, str(container_port)), cwd=self._base_dir)
        if result.returncode != 0 or not result.stdout.strip():
            raise ComposeError(
                f"could not resolve published port for {service}:{container_port}: "
                f"{result.stderr.strip()}"
            )
        # stdout is "<host>:<port>", possibly with a trailing newline.
        return int(result.stdout.strip().rsplit(":", 1)[-1])

    def base_url(self) -> str:
        port = self.published_port(self.config.target_service, self.config.target_port)
        return f"http://127.0.0.1:{port}"

    def wait_healthy(self) -> str:
        """Poll the target service until it answers, and return its base URL."""
        base_url = self.base_url()
        url = base_url.rstrip("/") + self.config.health_path
        deadline = time.monotonic() + self.config.startup_timeout_seconds
        last_error: Exception | None = None

        while time.monotonic() < deadline:
            try:
                response = httpx.get(url, timeout=5.0)
                if response.status_code < 500:
                    return base_url
                last_error = ComposeError(f"{url} returned {response.status_code}")
            except httpx.HTTPError as exc:
                last_error = exc
            time.sleep(self.config.poll_interval_seconds)

        raise ComposeError(
            f"{self.config.target_service} never became healthy at {url}: {last_error}"
        )


def run_pipeline_from_compose(
    *,
    compose_file: Path,
    target_service: str,
    target_port: int,
    project_name: str | None = None,
    health_path: str = "/",
    startup_timeout_seconds: float = 120.0,
    **pipeline_kwargs: Any,
) -> Any:
    """Bring the stack up, run the standard pipeline against it, tear it down.

    Imports ``run_pipeline`` lazily so this module (and its ``docker``/``httpx``
    process concerns) stays out of the import graph for callers that only ever
    use ``target_url`` mode.
    """
    from qagent.pipeline import run_pipeline

    config = ComposeConfig(
        compose_file=compose_file,
        project_name=project_name or f"qagent-{compose_file.resolve().parent.name}".lower(),
        target_service=target_service,
        target_port=target_port,
        health_path=health_path,
        startup_timeout_seconds=startup_timeout_seconds,
    )

    with ComposeStack(config) as stack:
        base_url = stack.wait_healthy()
        pipeline_kwargs.setdefault("allow_private", True)
        return run_pipeline(base_url=base_url, **pipeline_kwargs)
