"""Compose mode (ADR-0001): lifecycle and health-wait logic, with docker and the
network mocked out — these tests don't need a Docker daemon.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from qagent.modules.provisioning.compose import (
    ComposeConfig,
    ComposeError,
    ComposeStack,
    run_pipeline_from_compose,
)


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _config(tmp_path: Path) -> ComposeConfig:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    return ComposeConfig(
        compose_file=compose_file,
        project_name="qagent-test",
        target_service="api",
        target_port=8080,
        poll_interval_seconds=0.0,
    )


def test_up_raises_on_nonzero_exit(tmp_path: Path) -> None:
    stack = ComposeStack(_config(tmp_path))
    with patch("qagent.modules.provisioning.compose._run", return_value=_completed(1, stderr="boom")):
        with pytest.raises(ComposeError, match="boom"):
            stack.up()
    # even a failed `up` may have started containers, so down() must still run
    assert stack._up is True


def test_down_is_a_noop_before_up(tmp_path: Path) -> None:
    stack = ComposeStack(_config(tmp_path))
    with patch("qagent.modules.provisioning.compose._run") as mocked:
        stack.down()
    mocked.assert_not_called()


def test_context_manager_tears_down_on_exception(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(args, cwd):  # noqa: ANN001
        calls.append(args)
        return _completed(0)

    with patch("qagent.modules.provisioning.compose._run", side_effect=fake_run):
        with pytest.raises(RuntimeError), ComposeStack(_config(tmp_path)):
            raise RuntimeError("pipeline blew up")

    assert any("up" in c for c in calls)
    assert any("down" in c for c in calls)


def test_published_port_parses_host_port(tmp_path: Path) -> None:
    stack = ComposeStack(_config(tmp_path))
    with patch(
        "qagent.modules.provisioning.compose._run",
        return_value=_completed(0, stdout="0.0.0.0:54321\n"),
    ):
        assert stack.published_port("api", 8080) == 54321


def test_published_port_raises_when_unresolvable(tmp_path: Path) -> None:
    stack = ComposeStack(_config(tmp_path))
    with patch("qagent.modules.provisioning.compose._run", return_value=_completed(1, stderr="nope")):
        with pytest.raises(ComposeError):
            stack.published_port("api", 8080)


def test_wait_healthy_returns_base_url_once_reachable(tmp_path: Path) -> None:
    stack = ComposeStack(_config(tmp_path))
    responses = [httpx.ConnectError("refused"), MagicMock(status_code=200)]

    def fake_get(url, timeout):  # noqa: ANN001
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    with patch.object(stack, "base_url", return_value="http://127.0.0.1:54321"):
        with patch("qagent.modules.provisioning.compose.httpx.get", side_effect=fake_get):
            assert stack.wait_healthy() == "http://127.0.0.1:54321"


def test_wait_healthy_raises_after_timeout(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.startup_timeout_seconds = 0.0
    stack = ComposeStack(config)
    with patch.object(stack, "base_url", return_value="http://127.0.0.1:54321"):
        with patch(
            "qagent.modules.provisioning.compose.httpx.get",
            side_effect=httpx.ConnectError("refused"),
        ):
            with pytest.raises(ComposeError, match="never became healthy"):
                stack.wait_healthy()


def test_run_pipeline_from_compose_tears_down_even_when_pipeline_fails(tmp_path: Path) -> None:
    invocations: list[list[str]] = []

    def fake_run(args, cwd):  # noqa: ANN001
        invocations.append(args)
        if "port" in args:
            return _completed(0, stdout="0.0.0.0:54321\n")
        return _completed(0)

    with (
        patch("qagent.modules.provisioning.compose._run", side_effect=fake_run),
        patch("qagent.modules.provisioning.compose.httpx.get", return_value=MagicMock(status_code=200)),
        patch("qagent.pipeline.run_pipeline", side_effect=RuntimeError("pipeline exploded")),
        pytest.raises(RuntimeError, match="pipeline exploded"),
    ):
        run_pipeline_from_compose(
            compose_file=tmp_path / "docker-compose.yml",
            target_service="api",
            target_port=8080,
        )

    subcommands = [args[6] for args in invocations]  # ["up", "port", "down"]
    assert "up" in subcommands
    assert "down" in subcommands
