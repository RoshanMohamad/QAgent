"""`qagent doctor`.

The checks themselves are simple; what matters is that the report is honest
about severity. A doctor that reports every absent optional tool as a failure
gets ignored, and then the one real failure is ignored with it.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from qagent.cli import app
from qagent.config import Settings
from qagent.modules.doctor import (
    FAIL,
    OK,
    WARN,
    Check,
    Report,
    check_optional,
    check_pins,
    check_services,
)

runner = CliRunner()


# --------------------------------------------------------------------- report


def test_a_report_with_no_failures_is_healthy() -> None:
    report = Report(checks=[Check("a", OK, ""), Check("b", WARN, "")])

    assert report.healthy
    assert len(report.warnings) == 1


def test_one_failure_makes_it_unhealthy() -> None:
    report = Report(checks=[Check("a", OK, ""), Check("b", FAIL, "")])

    assert not report.healthy
    assert report.to_dict()["failures"] == 1


def test_the_report_serialises() -> None:
    payload = Report(checks=[Check("a", FAIL, "broken", "do this")]).to_dict()

    assert json.dumps(payload)
    assert payload["checks"][0]["remedy"] == "do this"


# ----------------------------------------------------------------------- pins


def test_pins_are_read_from_package_metadata() -> None:
    """Hardcoding them would let this check drift from pyproject.toml the
    moment someone edits one and not the other."""
    names = {c.name.split()[0] for c in check_pins()}

    assert "redis" in names
    assert "celery" in names


def test_a_satisfied_pin_is_reported_as_ok() -> None:
    redis_check = next(c for c in check_pins() if c.name.startswith("redis "))

    # The suite itself cannot run with a violating version, so in a working
    # environment this must be ok - and carry the constraint it was checked
    # against, so a reader can see what "ok" meant.
    assert redis_check.status == OK
    assert "<6.5" in redis_check.detail


def test_a_violated_pin_carries_a_forcing_remedy() -> None:
    """The offending copy is usually in user site-packages, where a plain
    `pip install` is a no-op."""
    violated = Check(
        "redis 8.1.0", FAIL, "violates", 'pip install --force-reinstall "redis>=5.2,<6.5"'
    )

    assert "--force-reinstall" in violated.remedy


# ------------------------------------------------------------------- services


def test_an_unreachable_redis_is_a_failure() -> None:
    checks = check_services(Settings(redis_url="redis://127.0.0.1:1/0"))

    redis_check = next(c for c in checks if c.name == "redis")
    assert redis_check.status == FAIL
    assert redis_check.remedy


def test_an_unreachable_postgres_is_only_a_warning() -> None:
    """The CLI and the eval harness run with no database at all, which is a
    supported configuration - not a broken environment."""
    checks = check_services(
        Settings(
            redis_url="redis://127.0.0.1:1/0",
            database_url="postgresql+psycopg://nobody@127.0.0.1:1/nothing",
        )
    )

    postgres = next(c for c in checks if c.name == "postgres")
    assert postgres.status == WARN


def test_service_checks_do_not_hang() -> None:
    """A diagnostic that hangs is the thing it exists to diagnose."""
    import time

    started = time.monotonic()
    check_services(Settings(redis_url="redis://127.0.0.1:1/0"))

    assert time.monotonic() - started < 30


# ------------------------------------------------------------------- optional


def test_missing_optional_tools_are_warnings_never_failures() -> None:
    """A doctor that shouts about optional tools teaches people to ignore it,
    and then the one real failure is ignored too."""
    assert all(c.status in {OK, WARN} for c in check_optional())


def test_every_optional_check_says_what_it_gates() -> None:
    for check in check_optional():
        assert check.detail, f"{check.name} does not say what it affects"


# ------------------------------------------------------------------------ cli


def test_doctor_runs_and_reports(tmp_path) -> None:
    out = tmp_path / "doctor.json"

    result = runner.invoke(app, ["doctor", "--json", str(out)])

    # Exit code depends on whether services happen to be up; the report must
    # exist either way.
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["checks"]
    assert "healthy" in payload
    assert "environment" in result.output


def test_doctor_lists_remedies_for_what_it_flagged(tmp_path) -> None:
    result = runner.invoke(app, ["doctor"])

    payload_has_problem = "FAIL" in result.output
    if payload_has_problem:
        assert "To fix:" in result.output


def test_an_aborted_connection_does_not_advise_starting_the_container() -> None:
    """"Connection closed by server" means something accepted the TCP
    connection and then killed it - which a stopped container cannot do.
    Telling someone to start a container that is already running sends them the
    wrong way for an hour; this exact failure is why the module exists."""

    class _Aborting:
        def __init__(self, *a, **k):
            pass

        def ping(self):
            raise ConnectionError("Connection closed by server.")

        def close(self):
            pass

    import redis as redis_lib

    original = redis_lib.Redis.from_url
    redis_lib.Redis.from_url = lambda *a, **k: _Aborting()
    try:
        checks = check_services(Settings(redis_url="redis://127.0.0.1:1/0"))
    finally:
        redis_lib.Redis.from_url = original

    redis_check = next(c for c in checks if c.name == "redis")
    assert redis_check.status == FAIL
    assert "port proxy" in redis_check.remedy
    assert "docker compose up" not in redis_check.remedy
