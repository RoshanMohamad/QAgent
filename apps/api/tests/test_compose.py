"""Compose mode (ADR-0001): lifecycle and health-wait logic, with docker and the
network mocked out — these tests don't need a Docker daemon.
"""

from __future__ import annotations

import json
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


_RESOLVED = json.dumps({"services": {"api": {"image": "x"}, "db": {"image": "postgres"}}})


def _docker(args: list[str], *, up: subprocess.CompletedProcess | None = None):  # noqa: ANN202
    """Answer the calls `up` makes before it reaches `docker compose up` itself."""
    if "version" in args:
        return _completed(0, stdout="2.29.1\n")
    if "config" in args:
        return _completed(0, stdout=_RESOLVED)
    if "port" in args:
        return _completed(0, stdout="0.0.0.0:54321\n")
    if "up" in args and up is not None:
        return up
    return _completed(0)


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
    failing_up = _completed(1, stderr="boom")
    with patch(
        "qagent.modules.provisioning.compose._run",
        side_effect=lambda args, cwd: _docker(args, up=failing_up),
    ):
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
        return _docker(args)

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
        return _docker(args)

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

    subcommands = {sub for args in invocations for sub in ("up", "down") if sub in args}
    assert subcommands == {"up", "down"}


def test_up_layers_the_sandbox_override_and_down_removes_it(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(args, cwd):  # noqa: ANN001
        calls.append(args)
        return _docker(args)

    stack = ComposeStack(_config(tmp_path))
    with patch("qagent.modules.provisioning.compose._run", side_effect=fake_run):
        stack.up()
        up_call = next(c for c in calls if "up" in c)
        override = Path(up_call[up_call.index("-f", 3) + 1])
        assert override.name == "qagent.sandbox.yml"
        assert override.exists()
        # never written into the repository being tested
        assert tmp_path not in override.parents
        stack.down()

    assert not override.exists()


def test_up_refuses_a_policy_violation_before_starting_anything(tmp_path: Path) -> None:
    hostile = json.dumps({"services": {"api": {"image": "x", "privileged": True}}})
    calls: list[list[str]] = []

    def fake_run(args, cwd):  # noqa: ANN001
        calls.append(args)
        if "config" in args:
            return _completed(0, stdout=hostile)
        return _docker(args)

    stack = ComposeStack(_config(tmp_path))
    with patch("qagent.modules.provisioning.compose._run", side_effect=fake_run):
        with pytest.raises(ComposeError, match="privileged"):
            stack.up()

    assert not any("up" in c for c in calls)
    assert stack._up is False


def test_up_refuses_a_compose_too_old_to_override(tmp_path: Path) -> None:
    def fake_run(args, cwd):  # noqa: ANN001
        if "version" in args:
            return _completed(0, stdout="2.20.0\n")
        return _docker(args)

    with patch("qagent.modules.provisioning.compose._run", side_effect=fake_run):
        with pytest.raises(ComposeError, match="2.24.4"):
            ComposeStack(_config(tmp_path)).up()


def test_sandbox_none_runs_the_file_as_written(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(args, cwd):  # noqa: ANN001
        calls.append(args)
        return _docker(args)

    config = _config(tmp_path)
    config.sandbox = None
    with patch("qagent.modules.provisioning.compose._run", side_effect=fake_run):
        ComposeStack(config).up()

    assert len(calls) == 1 and "up" in calls[0]  # no version/config probe
    assert calls[0].count("-f") == 1
