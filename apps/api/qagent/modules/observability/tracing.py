"""OpenTelemetry tracing (CLAUDE.md section 21).

[ADR-0008](../../../../docs/decisions/ADR-0008-phase-6-scope.md) deferred this
on the grounds that distributed tracing needs a real deployment to size
against. That argument was right about the *sampling and backend* decisions and
wrong about the instrumentation: a span per pipeline stage is worth having
before there is any deployment at all, because it answers a question the
existing Prometheus counters cannot.

Those counters say a scan took 40 seconds. They cannot say whether it was 38
seconds of discovery waiting on a slow OpenAPI fetch or 38 seconds of triage
waiting on a model, and that is the difference between two completely different
fixes. A span per stage answers it directly.

**Off by default, and free when off.** `QAGENT_TRACING_ENABLED=false` means
`span()` returns a null context manager and nothing in `qagent` imports
`opentelemetry` at all - the packages are an optional extra
(`pip install qagent[otel]`). This matters more than usual here: the CLI and the
eval harness run the same pipeline, and neither should acquire a tracing
dependency to score a fixture.

What ADR-0008 deferred and this does *not* decide: sampling rates, retention,
or which backend to run. Those still need a real deployment, and OTLP means the
choice stays the operator's.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

#: Set once by `setup_tracing`. None means tracing is off, which is the default
#: and the state every code path has to work in.
_tracer: Any = None
_enabled = False


def is_enabled() -> bool:
    return _enabled


def setup_tracing(settings=None, *, app: Any = None) -> bool:
    """Install the OTLP exporter and instrument what is present.

    Returns whether tracing actually came up. Never raises: a misconfigured
    exporter must not stop the API from serving, because an observability
    dependency that can take down the thing it observes is a liability.
    """
    global _tracer, _enabled

    if settings is None:
        from qagent.config import get_settings

        settings = get_settings()

    if not settings.qagent_tracing_enabled:
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "QAGENT_TRACING_ENABLED is set but the OpenTelemetry packages are missing; "
            "install them with: pip install 'qagent[otel]'"
        )
        return False

    try:
        resource = Resource.create(
            {
                "service.name": settings.qagent_tracing_service_name,
                "service.version": _version(),
                "deployment.environment": settings.qagent_env,
            }
        )
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.qagent_tracing_endpoint))
        )
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("qagent")
        _enabled = True
    except Exception as exc:  # noqa: BLE001 - never let telemetry break the process
        logger.warning("tracing setup failed, continuing untraced: %s", exc)
        return False

    _instrument(app)
    logger.info("tracing enabled, exporting to %s", settings.qagent_tracing_endpoint)
    return True


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("qagent")
    except Exception:  # noqa: BLE001 - a missing version is not worth failing over
        return "unknown"


def _instrument(app: Any) -> None:
    """Auto-instrument whichever libraries are installed.

    Each one is attempted independently: httpx instrumentation missing is not a
    reason to lose FastAPI spans. This is also why every block swallows its own
    error - a partial trace is far more useful than none.
    """
    if app is not None:
        _try_instrument(
            "fastapi",
            lambda: __import__(
                "opentelemetry.instrumentation.fastapi", fromlist=["FastAPIInstrumentor"]
            ).FastAPIInstrumentor.instrument_app(app),
        )

    # httpx is how QAgent reaches the application under test, so these spans are
    # the ones that show a slow target rather than a slow QAgent.
    _try_instrument(
        "httpx",
        lambda: __import__(
            "opentelemetry.instrumentation.httpx", fromlist=["HTTPXClientInstrumentor"]
        ).HTTPXClientInstrumentor().instrument(),
    )
    _try_instrument(
        "sqlalchemy",
        lambda: __import__(
            "opentelemetry.instrumentation.sqlalchemy", fromlist=["SQLAlchemyInstrumentor"]
        ).SQLAlchemyInstrumentor().instrument(),
    )
    _try_instrument(
        "celery",
        lambda: __import__(
            "opentelemetry.instrumentation.celery", fromlist=["CeleryInstrumentor"]
        ).CeleryInstrumentor().instrument(),
    )


def _try_instrument(name: str, install) -> None:
    try:
        install()
        logger.debug("instrumented %s", name)
    except ImportError:
        logger.debug("%s instrumentation not installed, skipping", name)
    except Exception as exc:  # noqa: BLE001 - partial tracing beats none
        logger.warning("could not instrument %s: %s", name, exc)


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """A span, or nothing at all.

    The no-op path is the default and is deliberately cheap: one boolean check
    and an empty generator, no imports, no allocation of a span object. The
    pipeline wraps every stage in one of these, so this runs on every scan
    whether or not anyone is collecting traces.
    """
    if not _enabled or _tracer is None:
        yield None
        return

    with _tracer.start_as_current_span(name) as active:
        for key, value in attributes.items():
            if value is not None:
                active.set_attribute(f"qagent.{key}", value)
        yield active


def set_attributes(active: Any, **attributes: Any) -> None:
    """Attach attributes discovered *during* a stage rather than before it.

    Counts like "how many endpoints were found" are only known at the end, and
    they are exactly what makes a trace readable - a span that says
    `discover 1.2s` is far less useful than one that says `discover 1.2s,
    47 endpoints`.
    """
    if active is None:
        return
    for key, value in attributes.items():
        if value is not None:
            try:
                active.set_attribute(f"qagent.{key}", value)
            except Exception as exc:  # noqa: BLE001 - telemetry never breaks the caller
                logger.debug("could not set span attribute %s: %s", key, exc)


def reset_for_tests() -> None:
    """Return the module to its default state. Used only by the test suite."""
    global _tracer, _enabled
    _tracer = None
    _enabled = False
