"""Notifications: delivery is recorded, failures never propagate, and a webhook
target gets the same SSRF treatment as any other user-supplied address.
"""

from __future__ import annotations

import httpx
import pytest

from qagent.modules.notify.dispatch import (
    DeliveryResult,
    render,
    send,
    slack_payload,
    webhook_payload,
)


class _FakeTransport(httpx.BaseTransport):
    def __init__(self, status: int = 200, body: str = "ok", raises: Exception | None = None):
        self.status = status
        self.body = body
        self.raises = raises
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        return httpx.Response(self.status, text=self.body)


def _client(transport: _FakeTransport) -> httpx.Client:
    return httpx.Client(transport=transport)


# ------------------------------------------------------------------- delivery


def test_a_2xx_counts_as_delivered() -> None:
    transport = _FakeTransport(200)

    result = send(
        channel="slack",
        target="http://127.0.0.1:9/hook",
        payload={"text": "hi"},
        allow_private=True,
        client=_client(transport),
    )

    assert result.delivered
    assert result.status_code == 200
    assert result.delivered_at is not None


def test_a_204_also_counts() -> None:
    """A generic webhook answers 204; only Slack answers 200 with a body."""
    result = send(
        channel="webhook",
        target="http://127.0.0.1:9/hook",
        payload={},
        allow_private=True,
        client=_client(_FakeTransport(204, "")),
    )

    assert result.delivered


def test_a_4xx_is_recorded_as_undelivered_with_the_reason() -> None:
    result = send(
        channel="slack",
        target="http://127.0.0.1:9/hook",
        payload={},
        allow_private=True,
        client=_client(_FakeTransport(404, "no_service")),
    )

    assert not result.delivered
    assert result.status_code == 404
    assert "no_service" in (result.error or "")


def test_a_dead_webhook_never_raises() -> None:
    """A blocked quality gate whose Slack is down is still a blocked gate."""
    transport = _FakeTransport(raises=httpx.ConnectError("refused"))

    result = send(
        channel="slack",
        target="http://127.0.0.1:9/hook",
        payload={},
        allow_private=True,
        client=_client(transport),
    )

    assert not result.delivered
    assert "refused" in (result.error or "")


def test_the_payload_actually_reaches_the_target() -> None:
    transport = _FakeTransport()

    send(
        channel="webhook",
        target="http://127.0.0.1:9/hook",
        payload={"event": "gate_blocked"},
        allow_private=True,
        client=_client(transport),
    )

    assert b"gate_blocked" in transport.requests[0].content


# ----------------------------------------------------------------------- ssrf


def test_a_link_local_target_is_refused() -> None:
    """A notification target is a credential-exfiltration primitive otherwise."""
    transport = _FakeTransport()

    result = send(
        channel="webhook",
        target="http://169.254.169.254/latest/meta-data/",
        payload={},
        allow_private=False,
        client=_client(transport),
    )

    assert not result.delivered
    assert "rejected" in (result.error or "")
    assert transport.requests == []


def test_private_targets_are_refused_by_default() -> None:
    transport = _FakeTransport()

    result = send(
        channel="webhook",
        target="http://127.0.0.1:9/hook",
        payload={},
        client=_client(transport),
    )

    assert not result.delivered
    assert transport.requests == []


def test_a_non_http_scheme_is_refused() -> None:
    result = send(
        channel="webhook",
        target="file:///etc/passwd",
        payload={},
        allow_private=True,
        client=_client(_FakeTransport()),
    )

    assert not result.delivered


# -------------------------------------------------------------------- payloads


def test_slack_payload_carries_the_summary_in_text_too() -> None:
    """Blocks-only messages read as empty in previews and screen readers."""
    payload = slack_payload(event="gate_blocked", title="Blocked", lines=["2 critical"])

    assert payload["text"]
    assert "2 critical" in payload["text"]
    assert payload["blocks"]


def test_slack_payload_survives_a_very_long_body() -> None:
    payload = slack_payload(event="x", title="t" * 500, lines=["y" * 6000])

    assert len(payload["text"]) <= 3000
    assert all(len(str(b)) < 4000 for b in payload["blocks"])


def test_slack_payload_links_back_when_a_url_is_given() -> None:
    payload = slack_payload(event="x", title="t", lines=["l"], url="http://qagent/runs/1")

    assert any("qagent/runs/1" in str(b) for b in payload["blocks"])


def test_webhook_payload_is_structured_not_prose() -> None:
    """The receiver is a program; it wants the counts, not a sentence."""
    payload = webhook_payload(
        event="run_finished", title="t", lines=["l"], data={"failed": 3}
    )

    assert payload["data"]["failed"] == 3
    assert payload["event"] == "run_finished"


# --------------------------------------------------------------------- render


@pytest.mark.parametrize(
    "event,data,expected",
    [
        ("gate_blocked", {"project": "shop", "reasons": ["2 critical"]}, "shop"),
        ("critical_defect", {"title": "crash", "reference": "BUG-1"}, "crash"),
        ("run_finished", {"project": "shop", "failed": 2}, "shop"),
        ("security_finding", {"title": "SQLi", "severity": "critical"}, "SQLi"),
    ],
)
def test_each_event_renders_a_useful_title(event, data, expected) -> None:
    title, lines = render(event, data)

    assert expected in title
    assert lines


def test_an_unknown_event_is_still_delivered() -> None:
    """A caller who added an event and forgot to teach render about it should
    see a plain message, not silence."""
    title, lines = render("something_new", {"a": 1})

    assert "something_new" in title
    assert lines


def test_gate_blocked_without_reasons_still_says_something() -> None:
    title, lines = render("gate_blocked", {"project": "shop"})

    assert lines and lines[0]


def test_delivery_result_serialises() -> None:
    assert DeliveryResult(delivered=False, error="x").to_dict()["delivered"] is False
