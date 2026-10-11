"""Compose sandbox policy (CLAUDE.md section 22): what is refused, and what the
hardening override actually says. Pure functions - no Docker daemon needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from qagent.modules.provisioning.sandbox import (
    INGRESS_NETWORK,
    INTERNAL_NETWORK,
    SandboxPolicy,
    SandboxViolation,
    build_override,
    check_policy,
    dump_override,
    parse_compose_version,
    require_compose_version,
)


def _resolved(**services: dict) -> dict:
    return {"services": services}


def _violations(resolved: dict, repo: Path, target: str = "api") -> list[str]:
    with pytest.raises(SandboxViolation) as info:
        check_policy(resolved, repo_root=repo, target_service=target)
    return info.value.violations


def test_ordinary_stack_passes(tmp_path: Path) -> None:
    check_policy(
        _resolved(
            api={
                "build": {"context": str(tmp_path)},
                "volumes": [{"type": "bind", "source": str(tmp_path / "src"), "target": "/app"}],
            },
            db={"image": "postgres:16", "volumes": [{"type": "volume", "source": "pgdata"}]},
        ),
        repo_root=tmp_path,
        target_service="api",
    )


@pytest.mark.parametrize(
    ("service", "expected"),
    [
        ({"privileged": True}, "privileged"),
        ({"network_mode": "host"}, "network_mode"),
        ({"network_mode": "container:abc"}, "network_mode"),
        ({"pid": "host"}, "pid: host"),
        ({"ipc": "host"}, "ipc: host"),
        ({"userns_mode": "host"}, "userns_mode: host"),
        ({"cap_add": ["SYS_ADMIN"]}, "cap_add"),
        ({"devices": ["/dev/kvm:/dev/kvm"]}, "devices"),
        ({"security_opt": ["seccomp:unconfined"]}, "security_opt"),
    ],
)
def test_host_access_is_refused(tmp_path: Path, service: dict, expected: str) -> None:
    violations = _violations(_resolved(api={"image": "x"}, worker={"image": "x", **service}), tmp_path)
    assert any(expected in v and v.startswith("worker") for v in violations)


def test_docker_socket_mount_is_refused(tmp_path: Path) -> None:
    sock = {"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"}
    violations = _violations(_resolved(api={"image": "x", "volumes": [sock]}), tmp_path)
    assert any("Docker socket" in v for v in violations)


def test_bind_mount_outside_repo_is_refused(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = {"type": "bind", "source": str(tmp_path / "secrets"), "target": "/s"}
    escape = {"type": "bind", "source": str(repo / ".." / "secrets"), "target": "/s"}
    for volume in (outside, escape):
        violations = _violations(_resolved(api={"image": "x", "volumes": [volume]}), repo)
        assert any("outside the repository" in v for v in violations)


def test_missing_target_service_is_reported(tmp_path: Path) -> None:
    violations = _violations(_resolved(web={"image": "x"}), tmp_path)
    assert any("'api' is not defined" in v for v in violations)


def test_every_violation_is_listed_not_just_the_first(tmp_path: Path) -> None:
    violations = _violations(
        _resolved(api={"image": "x", "privileged": True}, db={"image": "y", "pid": "host"}), tmp_path
    )
    assert len(violations) == 2


def test_override_caps_and_isolates_every_service() -> None:
    policy = SandboxPolicy(cpus=0.5, memory="512m", pids_limit=100)
    override = build_override(
        _resolved(api={"image": "x", "ports": ["0.0.0.0:8000:8000"]}, db={"image": "postgres"}),
        target_service="api",
        target_port=8000,
        policy=policy,
    )
    for name in ("api", "db"):
        svc = override["services"][name]
        assert svc["cpus"] == 0.5
        assert svc["mem_limit"] == svc["memswap_limit"] == "512m"
        assert svc["pids_limit"] == 100
        assert svc["read_only"] is True
        assert "/tmp" in svc["tmpfs"]  # noqa: S108
        assert svc["cap_drop"].value == ["ALL"]
        assert svc["security_opt"].value == ["no-new-privileges:true"]

    assert override["services"]["api"]["ports"].value == ["127.0.0.1::8000"]
    assert override["services"]["db"]["ports"].value == []
    assert override["services"]["api"]["networks"].value == [INTERNAL_NETWORK, INGRESS_NETWORK]
    assert override["services"]["db"]["networks"].value == [INTERNAL_NETWORK]
    assert override["networks"][INTERNAL_NETWORK] == {"internal": True}


def test_writable_rootfs_drops_read_only_and_tmpfs() -> None:
    override = build_override(
        _resolved(api={"image": "x"}),
        target_service="api",
        target_port=80,
        policy=SandboxPolicy(read_only=False),
    )
    assert override["services"]["api"]["read_only"] is False
    assert "tmpfs" not in override["services"]["api"]


def test_shared_network_namespace_gets_no_networks_key() -> None:
    override = build_override(
        _resolved(api={"image": "x"}, sidecar={"image": "y", "network_mode": "service:api"}),
        target_service="api",
        target_port=80,
        policy=SandboxPolicy(),
    )
    assert "networks" not in override["services"]["sidecar"]


def test_dumped_override_uses_replace_tags_not_merge() -> None:
    text = dump_override(
        build_override(
            _resolved(api={"image": "x"}), target_service="api", target_port=80, policy=SandboxPolicy()
        )
    )
    assert "ports: !override" in text
    assert "cap_add: !override" in text
    assert "networks: !override" in text

    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda loader, tag, node: loader.construct_sequence(node))
    loaded = yaml.load(text, Loader=_Loader)  # noqa: S506 - SafeLoader subclass
    assert loaded["services"]["api"]["ports"] == ["127.0.0.1::80"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [("2.29.1\n", (2, 29, 1)), ("v5.5.1", (5, 5, 1)), ("Docker Compose version v2.24.4", (2, 24, 4))],
)
def test_parse_compose_version(text: str, expected: tuple[int, int, int]) -> None:
    assert parse_compose_version(text) == expected


def test_old_or_unknown_compose_is_refused() -> None:
    require_compose_version("2.24.4")
    for text in ("2.24.3", "1.29.2", ""):
        with pytest.raises(SandboxViolation):
            require_compose_version(text)
