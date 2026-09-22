"""Check that this environment matches what the project declares.

Written after the integration suite spent hours failing for reasons that were
all environmental and none of which the failure message named. The symptom was
always the same - a timeout deep inside a client library - and it was variously
blamed on Python 3.14, on pytest, and on a `redis-py` version that did in fact
violate this project's own pin while satisfying the import (`pip check` stays
quiet about that when the editable install's metadata is stale).

The pin really was violated and is worth catching. It was not, on its own, the
cause: the timeouts persisted after correcting it, and the actual driver turned
out to be Docker Desktop's Windows port proxy degrading under sustained load.

That is the general lesson this module exists for. When a client library times
out, the useful question is not "which of my libraries is broken" but "is my
environment what I think it is" - and answering it by hand takes far longer than
it should. So this checks the boring things first, together, in one command:

* installed versions against the constraints the project declares
* whether Postgres and Redis are actually reachable
* which optional external tools are present, and which features are therefore
  unavailable

Everything here is read-only. It installs nothing and changes nothing - a
diagnostic that repairs things is a diagnostic nobody can trust the output of.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version

logger = logging.getLogger(__name__)

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    #: What to do about it. Empty when there is nothing to do.
    remedy: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "remedy": self.remedy,
        }


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    @property
    def healthy(self) -> bool:
        return not self.failed

    def to_dict(self) -> dict:
        return {
            "healthy": self.healthy,
            "checks": [c.to_dict() for c in self.checks],
            "failures": len(self.failed),
            "warnings": len(self.warnings),
        }


def _installed(package: str) -> str | None:
    try:
        return version(package)
    except PackageNotFoundError:
        return None


def check_pins() -> list[Check]:
    """Installed versions against the constraints this project declares.

    Read from the package metadata rather than hardcoded, so the check cannot
    drift away from `pyproject.toml` the moment someone edits one and not the
    other.
    """
    checks: list[Check] = []

    try:
        from importlib.metadata import requires

        from packaging.requirements import Requirement
        from packaging.version import Version

        declared = requires("qagent") or []
    except Exception as exc:  # noqa: BLE001 - a diagnostic must not crash
        return [
            Check(
                "dependency pins",
                WARN,
                f"could not read qagent's declared requirements: {exc}",
                "pip install -e ./apps/api",
            )
        ]

    for raw in declared:
        try:
            requirement = Requirement(raw)
        except Exception as exc:  # noqa: BLE001 - one bad entry must not stop the rest
            logger.debug("skipping unparseable requirement %r: %s", raw, exc)
            continue
        # Extras (playwright, semgrep, boto3, ...) are checked separately as
        # optional tools; a missing one is not a broken environment.
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        if not requirement.specifier:
            continue

        found = _installed(requirement.name)
        if found is None:
            continue

        try:
            satisfied = Version(found) in requirement.specifier
        except Exception as exc:  # noqa: BLE001 - an exotic version string is not a fault
            logger.debug("skipping %s: cannot compare %r: %s", requirement.name, found, exc)
            continue

        if satisfied:
            checks.append(Check(f"{requirement.name} {found}", OK, str(requirement.specifier)))
        else:
            checks.append(
                Check(
                    f"{requirement.name} {found}",
                    FAIL,
                    f"violates the declared constraint {requirement.specifier}",
                    # `--force-reinstall` because the offending copy is often in
                    # user site-packages, where a plain install is a no-op.
                    f'pip install --force-reinstall "{requirement.name}{requirement.specifier}"',
                )
            )

    return checks


def check_services(settings=None) -> list[Check]:
    """Whether Postgres and Redis are actually reachable.

    Short timeouts on purpose: a diagnostic that hangs is the thing it is
    supposed to diagnose.
    """
    if settings is None:
        from qagent.config import get_settings

        settings = get_settings()

    checks: list[Check] = []

    try:
        import redis as redis_lib

        client = redis_lib.Redis.from_url(
            settings.redis_url, socket_timeout=3, socket_connect_timeout=3
        )
        client.ping()
        client.close()
        checks.append(Check("redis", OK, settings.redis_url))
    except Exception as exc:  # noqa: BLE001 - reporting the failure is the point
        message = str(exc)
        # "Connection closed by server" and WinError 10053 mean something
        # accepted the TCP connection and then killed it - which a stopped
        # container cannot do, because nothing would be listening at all. On
        # Windows that is Docker Desktop's port proxy, and telling someone to
        # start a container that is already running sends them the wrong way
        # for an hour. This exact failure is why this module exists.
        aborted = "closed by server" in message or "10053" in message
        remedy = (
            "the port is listening but the connection is being killed - "
            "on Windows this is usually Docker Desktop's port proxy: "
            "restart Docker Desktop, then re-create the container"
            if aborted
            else "docker compose up -d redis"
        )
        checks.append(Check("redis", FAIL, f"{settings.redis_url}: {message[:120]}", remedy))

    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(settings.database_url, connect_args={"connect_timeout": 3})
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        engine.dispose()
        checks.append(Check("postgres", OK, "reachable as the application role"))
    except Exception as exc:  # noqa: BLE001
        checks.append(
            Check(
                "postgres",
                # A warning, not a failure: the CLI and the eval harness run
                # with no database at all, which is a supported configuration.
                WARN,
                f"not reachable: {str(exc)[:120]}",
                "docker compose up -d postgres && python -m qagent.db_init",
            )
        )

    return checks


#: External binaries, and what stops working without each. Reported as warnings,
#: never failures: the default install is expected to have none of them, and a
#: doctor that shouts about optional tools teaches people to ignore it.
OPTIONAL_TOOLS = {
    "semgrep": "static analysis (`qagent security`)",
    "trivy": "dependency CVE scanning (`qagent security`)",
    "k6": "load testing (`qagent perf`)",
    "jmeter": "load testing (`qagent perf --tool jmeter`)",
}

OPTIONAL_PACKAGES = {
    "playwright": "browser E2E, exploration and the recorder",
    "boto3": "S3-compatible artifact storage",
    "pgvector": "native vector search",
    "opentelemetry-sdk": "distributed tracing",
}


def check_optional() -> list[Check]:
    import shutil

    checks: list[Check] = []

    for binary, feature in OPTIONAL_TOOLS.items():
        found = shutil.which(binary)
        checks.append(
            Check(binary, OK, feature) if found else Check(binary, WARN, f"absent: {feature}")
        )

    for package, feature in OPTIONAL_PACKAGES.items():
        found = _installed(package)
        checks.append(
            Check(package, OK, f"{found} - {feature}")
            if found
            else Check(package, WARN, f"absent: {feature}")
        )

    return checks


def run(settings=None) -> Report:
    report = Report()
    report.checks.extend(check_pins())
    report.checks.extend(check_services(settings))
    report.checks.extend(check_optional())

    logger.info(
        "doctor: %d check(s), %d failure(s), %d warning(s)",
        len(report.checks),
        len(report.failed),
        len(report.warnings),
    )
    return report
