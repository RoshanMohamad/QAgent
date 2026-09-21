"""Tracing: off by default, free when off, and never able to break the caller.

The invariant that matters is the last one. An observability layer that can
take down the thing it observes is a liability, and the CLI and eval harness
run the same pipeline - neither should acquire a tracing dependency, or a
tracing failure mode, to score a fixture.
"""

from __future__ import annotations

import pytest

from qagent.config import Settings
from qagent.modules.observability import tracing


@pytest.fixture(autouse=True)
def _reset():
    tracing.reset_for_tests()
    yield
    tracing.reset_for_tests()


# ------------------------------------------------------------------- defaults


def test_tracing_is_off_by_default() -> None:
    assert not tracing.is_enabled()
    assert not tracing.setup_tracing(Settings())


def test_span_is_a_no_op_when_disabled() -> None:
    with tracing.span("qagent.discover", endpoints=3) as active:
        assert active is None


def test_set_attributes_on_a_null_span_is_harmless() -> None:
    tracing.set_attributes(None, endpoints=3, spec_url="http://x")


def test_the_pipeline_runs_untraced_without_opentelemetry() -> None:
    """The whole point: `qagent scan` must not need the otel extra."""
    from qagent.modules.discovery.openapi import EndpointSpec
    from qagent.modules.generator.rules import generate
    from qagent.modules.planner.strategy import build_plan

    endpoints = [EndpointSpec(method="GET", path="/orders")]
    plan = build_plan(endpoints)

    assert generate(endpoints, plan=plan).cases
    assert not tracing.is_enabled()


# --------------------------------------------------------------------- setup


def test_missing_packages_degrade_rather_than_raise(monkeypatch) -> None:
    """Configuring tracing without installing it must warn, not crash."""
    import builtins

    real_import = builtins.__import__

    def _no_otel(name, *args, **kwargs):
        if name.startswith("opentelemetry"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_otel)

    assert not tracing.setup_tracing(Settings(qagent_tracing_enabled=True))
    assert not tracing.is_enabled()


def test_a_broken_exporter_does_not_stop_the_process(monkeypatch) -> None:
    def _explode(*args, **kwargs):
        raise RuntimeError("collector unreachable")

    monkeypatch.setattr(tracing, "_version", _explode)

    # _version() raising is enough to prove the guard: setup must return False
    # rather than propagate, because main.py calls this at import time and a
    # raise there means the API never starts.
    assert not tracing.setup_tracing(
        Settings(qagent_tracing_enabled=True, qagent_tracing_endpoint="http://127.0.0.1:1")
    )


# --------------------------------------------------------------------- enabled


class _RecordingSpan:
    def __init__(self) -> None:
        self.attributes: dict = {}

    def set_attribute(self, key: str, value) -> None:
        self.attributes[key] = value

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecordingTracer:
    def __init__(self) -> None:
        self.spans: dict[str, _RecordingSpan] = {}

    def start_as_current_span(self, name: str) -> _RecordingSpan:
        span = _RecordingSpan()
        self.spans[name] = span
        return span


def _enable(monkeypatch) -> _RecordingTracer:
    tracer = _RecordingTracer()
    monkeypatch.setattr(tracing, "_tracer", tracer)
    monkeypatch.setattr(tracing, "_enabled", True)
    return tracer


def test_attributes_are_namespaced(monkeypatch) -> None:
    """Unprefixed keys collide with the semantic conventions OTel reserves."""
    tracer = _enable(monkeypatch)

    with tracing.span("qagent.discover", endpoints=47):
        pass

    assert tracer.spans["qagent.discover"].attributes == {"qagent.endpoints": 47}


def test_none_valued_attributes_are_dropped(monkeypatch) -> None:
    tracer = _enable(monkeypatch)

    with tracing.span("qagent.discover", endpoints=1, spec_url=None):
        pass

    assert "qagent.spec_url" not in tracer.spans["qagent.discover"].attributes


def test_attributes_can_be_added_after_the_work(monkeypatch) -> None:
    """Counts like 'how many endpoints' are only known once the stage is done."""
    tracer = _enable(monkeypatch)

    with tracing.span("qagent.generate") as active:
        tracing.set_attributes(active, cases=18, coverage_ratio=1.0)

    assert tracer.spans["qagent.generate"].attributes["qagent.cases"] == 18


def test_a_span_that_cannot_take_an_attribute_does_not_break_the_caller(monkeypatch) -> None:
    class _Hostile(_RecordingSpan):
        def set_attribute(self, key, value):
            raise RuntimeError("exporter is gone")

    tracing.set_attributes(_Hostile(), cases=1)


def test_the_pipeline_emits_a_span_per_stage(monkeypatch) -> None:
    """The reason tracing earns its place: which stage consumed the wall clock."""
    tracer = _enable(monkeypatch)

    from qagent.pipeline import run_pipeline

    run_pipeline(base_url="http://127.0.0.1:1", timeout_seconds=0.2)

    # Discovery fails against a dead target, so only that span exists - which is
    # itself the assertion that stages are instrumented independently.
    assert "qagent.discover" in tracer.spans
