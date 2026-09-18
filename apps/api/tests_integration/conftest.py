"""Integration tests against a real Postgres and Redis (ADR-0007).

Kept out of `tests/` -- and therefore out of `testpaths` -- on purpose. Everything
under `tests/` runs green with no database (README: "204 unit tests ... run green
without a database"), and several `qagent` modules cache their `Settings`/`Engine`
at *import* time (`qagent/db.py`'s module-level `_settings = get_settings()`,
which `qagent/main.py` and half the test suite import transitively). If this
directory lived under `tests/`, whichever module pytest happened to import first
during collection would freeze the database URL for the whole process, and
whether that was the real one or the unreachable default would depend on
collection order rather than on anything this file controls. A separate
top-level directory, invoked as its own `pytest tests_integration -q`, guarantees
a fresh interpreter that has seen the right environment variables before it
imports anything of ours.

Bring a real database up and run these with:

    POSTGRES_PORT=55432 REDIS_PORT=56379 docker compose up -d postgres redis
    export ADMIN_DATABASE_URL=postgresql+psycopg://qagent:qagent@localhost:55432/qagent
    export DATABASE_URL=postgresql+psycopg://qagent_app:qagent_app@localhost:55432/qagent
    export REDIS_URL=redis://localhost:56379/0
    export CELERY_BROKER_URL=redis://localhost:56379/1
    export CELERY_RESULT_BACKEND=redis://localhost:56379/2
    cd apps/api && pytest tests_integration -q
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, text

_ADMIN_URL = os.environ.get("ADMIN_DATABASE_URL")

collect_ignore_glob = [] if _ADMIN_URL else ["test_*.py"]

_API_ROOT = Path(__file__).resolve().parents[1]  # apps/api
_REPO_ROOT = _API_ROOT.parents[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_or_fail(proc: subprocess.Popen, ready: bool, what: str, log_path: Path) -> None:
    if ready:
        return
    proc.terminate()
    output = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    pytest.fail(f"{what} did not become ready in time:\n{output}")


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def admin_engine():
    """The superuser/bootstrap connection -- schema and role creation only.

    Never what a test asserts application behaviour through; that's `app_engine`.
    """
    engine = create_engine(_ADMIN_URL, future=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def _bootstrap_schema(admin_engine):
    """Runs the exact bootstrap `docker compose up`'s api service runs on every
    container start: idempotent by construction (ADR-0007), so re-running it
    against a database that already has the schema is the normal case, not a
    special one.
    """
    from qagent import db_init

    db_init.create_schema(admin_engine)
    db_init.ensure_app_role(admin_engine)
    db_init.apply_rls(admin_engine)


@pytest.fixture
def app_engine():
    """The same engine `qagent/db.py` builds for the API/worker at runtime --
    bound to the unprivileged role `_bootstrap_schema` just created, so a test
    using this fixture is exercising RLS exactly as a real request would."""
    from qagent.db import engine

    return engine


@pytest.fixture(scope="session")
def fixture_app_url(tmp_path_factory):
    """A real, running copy of the buggy-shop fixture app on an ephemeral port --
    a subprocess, not an ASGI transport, because `run_pipeline` makes real HTTP
    calls and the point of this suite is to prove the real network path works."""
    root = _REPO_ROOT / "packages" / "fixtures" / "buggy-shop"
    port = _free_port()
    log_path = tmp_path_factory.mktemp("logs") / "fixture-app.log"
    with open(log_path, "w", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app:app", "--port", str(port)],
            cwd=str(root),
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    ready = False
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/health", timeout=1.0).status_code == 200:
                ready = True
                break
        except httpx.HTTPError:
            pass
        if proc.poll() is not None:
            break
        time.sleep(0.5)

    _wait_or_fail(proc, ready, "fixture app", log_path)
    yield base_url
    _stop(proc)


@pytest.fixture(scope="session")
def live_worker(tmp_path_factory):
    """A real `celery worker` subprocess consuming the real Redis broker -- the
    actual entrypoint docker-compose.yml runs, not `.apply()`'s in-process
    shortcut. This is the other half of ADR-0007's verification: RLS isolation
    is proven against the app role directly; this proves the queued, distributed
    path (API -> Redis -> a separate worker process -> Postgres) actually works.
    """
    log_path = tmp_path_factory.mktemp("logs") / "worker.log"
    with open(log_path, "w", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "celery",
                "-A", "qagent.worker.tasks.celery_app",
                "worker", "--loglevel=info", "-Q", "qagent.scan,qagent.performance",
                "--concurrency=1", "--pool=solo",
            ],
            cwd=str(_API_ROOT),
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )

    from qagent.worker.tasks import celery_app

    deadline = time.monotonic() + 30
    ready = False
    while time.monotonic() < deadline:
        if celery_app.control.ping(timeout=1.0):
            ready = True
            break
        if proc.poll() is not None:
            break
        time.sleep(1.0)

    _wait_or_fail(proc, ready, "celery worker", log_path)
    proc.log_path = log_path  # type: ignore[attr-defined]
    yield proc
    _stop(proc)


@pytest.fixture(autouse=True)
def _clean_tenant_tables(admin_engine):
    """Every test starts from an empty tenant tree. `organizations` cascades to
    everything else via ON DELETE CASCADE (models.py). Truncation runs on the
    admin connection because the app role deliberately has no TRUNCATE grant
    (only SELECT/INSERT/UPDATE/DELETE - see `ensure_app_role`)."""
    with admin_engine.begin() as connection:
        connection.execute(text("TRUNCATE organizations CASCADE"))
    yield
    with admin_engine.begin() as connection:
        connection.execute(text("TRUNCATE organizations CASCADE"))


@pytest.fixture(autouse=True)
def _clean_rate_limits():
    """main.py's rate limiter and Celery's broker/result backend are different
    logical Redis databases (REDIS_URL's db 0 vs. CELERY_BROKER_URL's db 1 and
    CELERY_RESULT_BACKEND's db 2 - see .env.example), so flushing this one never
    touches a queued or in-flight task. Without this, every test in this session
    that calls register/login shares one counter (TestClient's client host is
    the fixed string "testclient", not a real, distinct IP per test), and
    register's 5/min limit (main.py) would trip well before the suite finishes.
    """
    import redis as redis_lib

    from qagent.config import get_settings

    client = redis_lib.Redis.from_url(get_settings().redis_url)
    client.flushdb()
    yield
