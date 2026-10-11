"""Sandboxing for compose mode (CLAUDE.md section 22).

Compose mode runs a repository's own ``docker-compose.yml`` - which is to say,
third-party code, configured by the same third party. Handed to ``docker compose
up`` unmodified, that file decides its own privileges: it can ask for
``privileged: true``, mount ``/var/run/docker.sock`` (root on the host, in one
line), join the host's network namespace, or publish a database on 0.0.0.0.

This module sits between the file and the daemon in two steps:

1. **Refuse.** ``check_policy`` reads the *resolved* configuration (``docker
   compose config --format json``, so ``extends``, ``include`` and variable
   interpolation are already applied - the text of the file is not what runs) and
   rejects anything that grants access to the host. These are refused rather
   than overridden because an override can't subtract a bind mount, and a stack
   that needs the Docker socket is not one QAgent can run safely at all.

2. **Constrain.** ``build_override`` produces a second compose file, layered on
   top with ``-f``, that caps CPU, memory and process count, drops every
   capability except the handful image entrypoints use to step down to an
   unprivileged user, sets ``no-new-privileges``, makes the root filesystem
   read-only (with tmpfs for the usual scratch paths), strips every published
   port, and moves every service onto an ``internal`` network with no route out.

The one deliberate hole: the target service is also attached to a second,
ordinary bridge network, because Docker does not forward published ports into an
``internal`` network and the pipeline has to reach the target from the host. So
the target container itself keeps egress; everything else in the stack - the
database, the cache, the worker - does not. Closing that last route needs
firewall rules on the host, which belong to the operator's infrastructure, not
to a compose file. The port it publishes is bound to 127.0.0.1 only.

Replacing (rather than merging with) the repository's own ``ports``,
``networks``, ``cap_add`` and ``security_opt`` uses the ``!override`` / ``!reset``
YAML tags, which Docker Compose supports from 2.24.4. ``require_compose_version``
checks that before anything runs, because an older Compose would silently
*merge* those lists - and a sandbox that fails open is worse than none.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

#: Compose added ``!override`` / ``!reset`` in this release.
MIN_COMPOSE_VERSION = (2, 24, 4)

#: Networks the override creates. Prefixed so they can't collide with the repo's.
INTERNAL_NETWORK = "qagent_sandbox"
INGRESS_NETWORK = "qagent_ingress"

#: What image entrypoints (postgres, redis, nginx...) need to chown their data
#: directory and drop from root to their service user. Everything else is dropped.
DEFAULT_CAPABILITIES = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID", "NET_BIND_SERVICE")

#: Scratch paths common images write to at startup; tmpfs keeps them writable
#: under a read-only root without persisting anything.
DEFAULT_TMPFS = ("/tmp", "/run", "/var/run", "/var/cache")  # noqa: S108 - container paths

#: Host namespaces a service may not join.
_HOST_NAMESPACE_KEYS = ("pid", "ipc", "userns_mode", "uts", "cgroup")

_DOCKER_SOCKETS = ("/var/run/docker.sock", "/run/docker.sock", "//./pipe/docker_engine")


class SandboxViolation(ValueError):
    """The compose file asks for something the sandbox will not grant."""

    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__(
            "compose file rejected by sandbox policy:\n  - " + "\n  - ".join(violations)
        )


@dataclass
class SandboxPolicy:
    """Per-service limits. Defaults are sized for a test target, not production."""

    cpus: float = 1.0
    memory: str = "1g"
    pids_limit: int = 512
    read_only: bool = True
    capabilities: tuple[str, ...] = DEFAULT_CAPABILITIES
    tmpfs: tuple[str, ...] = DEFAULT_TMPFS

    def describe(self) -> str:
        return (
            f"cpus={self.cpus} memory={self.memory} pids={self.pids_limit} "
            f"read_only={self.read_only} caps={','.join(self.capabilities) or 'none'} "
            f"egress=target-only"
        )


def parse_compose_version(text: str) -> tuple[int, int, int] | None:
    """``docker compose version --short`` prints e.g. ``2.29.1`` or ``v5.5.1``."""
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def require_compose_version(text: str) -> None:
    version = parse_compose_version(text)
    if version is None or version < MIN_COMPOSE_VERSION:
        needed = ".".join(map(str, MIN_COMPOSE_VERSION))
        raise SandboxViolation(
            [
                f"Docker Compose {needed}+ is required to sandbox a stack "
                f"(found {text.strip() or 'unknown'}); older versions merge the "
                "override instead of replacing it"
            ]
        )


def _is_within(path: str, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def check_policy(resolved: dict[str, Any], *, repo_root: Path, target_service: str) -> None:
    """Raise ``SandboxViolation`` listing every problem, not just the first."""
    services: dict[str, Any] = resolved.get("services") or {}
    violations: list[str] = []

    if target_service not in services:
        violations.append(
            f"target service {target_service!r} is not defined "
            f"(services: {', '.join(sorted(services)) or 'none'})"
        )

    for name, svc in sorted(services.items()):
        svc = svc or {}
        if svc.get("privileged"):
            violations.append(f"{name}: privileged mode")

        network_mode = str(svc.get("network_mode") or "")
        if network_mode == "host" or network_mode.startswith("container:"):
            violations.append(f"{name}: network_mode {network_mode!r}")
        if name == target_service and network_mode:
            violations.append(
                f"{name}: the target service can't use network_mode, it must publish a port"
            )

        for key in _HOST_NAMESPACE_KEYS:
            if str(svc.get(key) or "") == "host":
                violations.append(f"{name}: {key}: host")

        if svc.get("cap_add"):
            violations.append(f"{name}: cap_add {sorted(svc['cap_add'])}")
        if svc.get("devices"):
            violations.append(f"{name}: host devices")
        for opt in svc.get("security_opt") or []:
            if "unconfined" in str(opt):
                violations.append(f"{name}: security_opt {opt!r}")

        for volume in svc.get("volumes") or []:
            if not isinstance(volume, dict) or volume.get("type") != "bind":
                continue
            source = str(volume.get("source") or "")
            if any(source.replace("\\", "/").endswith(sock) for sock in _DOCKER_SOCKETS):
                violations.append(f"{name}: mounts the Docker socket ({source})")
            elif not _is_within(source, repo_root):
                violations.append(f"{name}: bind mount outside the repository ({source})")

    if violations:
        raise SandboxViolation(violations)


class _Tagged:
    def __init__(self, tag: str, value: Any):
        self.tag = tag
        self.value = value


class _Dumper(yaml.SafeDumper):
    pass


def _represent_tagged(dumper: yaml.SafeDumper, data: _Tagged) -> yaml.Node:
    if isinstance(data.value, dict):
        return dumper.represent_mapping(data.tag, data.value)
    return dumper.represent_sequence(data.tag, data.value)


_Dumper.add_representer(_Tagged, _represent_tagged)


def _override(value: Any) -> _Tagged:
    return _Tagged("!override", value)


def build_override(
    resolved: dict[str, Any],
    *,
    target_service: str,
    target_port: int,
    policy: SandboxPolicy,
) -> dict[str, Any]:
    """The hardening layer, as a compose document (dump it with ``dump_override``)."""
    services_out: dict[str, Any] = {}
    for name, svc in (resolved.get("services") or {}).items():
        svc = svc or {}
        is_target = name == target_service
        hardened: dict[str, Any] = {
            "cpus": policy.cpus,
            "mem_limit": policy.memory,
            # Equal to mem_limit: no swap, so the memory cap is actually a cap.
            "memswap_limit": policy.memory,
            "pids_limit": policy.pids_limit,
            "read_only": policy.read_only,
            "cap_drop": _override(["ALL"]),
            "cap_add": _override(list(policy.capabilities)),
            "security_opt": _override(["no-new-privileges:true"]),
            "ports": _override([f"127.0.0.1::{target_port}"] if is_target else []),
            "labels": {"dev.qagent.sandbox": "true"},
        }
        if policy.read_only:
            hardened["tmpfs"] = list(policy.tmpfs)
        # A service sharing another's network namespace (network_mode: service:x)
        # can't also declare networks; it inherits the sandboxed one.
        if not svc.get("network_mode"):
            networks = [INTERNAL_NETWORK, INGRESS_NETWORK] if is_target else [INTERNAL_NETWORK]
            hardened["networks"] = _override(networks)
        services_out[name] = hardened

    return {
        "services": services_out,
        "networks": {
            INTERNAL_NETWORK: {"internal": True},
            INGRESS_NETWORK: {},
        },
    }


def dump_override(document: dict[str, Any]) -> str:
    return yaml.dump(document, Dumper=_Dumper, sort_keys=False)


def write_override(document: dict[str, Any], path: Path) -> Path:
    path.write_text(dump_override(document), encoding="utf-8")
    return path
