"""Deliver a notification, and record whether it actually arrived.

Reference image 05 ends with an arrow to Slack, and CLAUDE.md section 23 lists
notifications and webhooks in Phase 5. The part worth engineering is not the
HTTP POST - it is the bookkeeping around it.

**Delivery is recorded, never assumed.** "We told the team" is a claim a Slack
outage silently falsifies. A notifier that cannot tell you it failed converts a
loud problem into a quiet one, which is strictly worse than having no notifier:
the team stops watching the dashboard *because* they expect to be paged.

**Sending is never allowed to fail the thing that triggered it.** A blocked
quality gate whose Slack webhook is down is still a blocked quality gate. Every
send here returns a status rather than raising, and the caller persists it.

**The target is validated against the same SSRF guard as everything else.** A
webhook URL is user-supplied, and a notification target pointing at
`169.254.169.254` is a credential-exfiltration primitive, not a mistake.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

logger = logging.getLogger(__name__)

#: Kept short on purpose. A notification is time-sensitive by definition, and a
#: caller blocked for 30s waiting on a dead webhook is a caller whose run now
#: takes 30s longer for no benefit.
TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class DeliveryResult:
    delivered: bool
    status_code: int | None = None
    error: str | None = None
    delivered_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "delivered": self.delivered,
            "status_code": self.status_code,
            "error": self.error,
            "delivered_at": self.delivered_at.isoformat() if self.delivered_at else None,
        }


def slack_payload(*, event: str, title: str, lines: list[str], url: str | None = None) -> dict:
    """A Slack message with the summary in `text` as well as in blocks.

    The duplication is deliberate: `text` is what Slack shows in a notification
    preview and what screen readers announce, and a blocks-only message reads as
    empty in both.
    """
    summary = f"{title} - {lines[0]}" if lines else title
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": title[:150]}},
    ]
    if lines:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:2900]}}
        )
    if url:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f"<{url}|Open in QAgent>"}]}
        )
    return {"text": summary[:3000], "blocks": blocks, "qagent_event": event}


def webhook_payload(*, event: str, title: str, lines: list[str], data: dict) -> dict:
    """A generic JSON webhook body.

    Structured rather than pre-rendered, because the receiver is a program: it
    wants the severity counts, not a sentence about them.
    """
    return {
        "event": event,
        "title": title,
        "summary": lines,
        "data": data,
        "sent_at": datetime.now(UTC).isoformat(),
    }


def _reject_unsafe_target(target: str, *, allow_private: bool) -> str | None:
    """Reuse the runner's egress policy rather than inventing a second one.

    Two SSRF guards in one codebase means two chances to get it wrong and one
    of them will drift. Returns a reason string when the target is refused.
    """
    from qagent.modules.runner.executor import TargetRejected, guard_target

    try:
        guard_target(target, allow_private=allow_private, allowlist=[])
    except TargetRejected as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001 - a guard that errors must refuse
        return f"target could not be validated: {exc}"
    return None


def send(
    *,
    channel: str,
    target: str,
    payload: dict,
    allow_private: bool = False,
    client: httpx.Client | None = None,
) -> DeliveryResult:
    """POST the payload. Returns a result; never raises.

    ``allow_private`` exists for local development, where a webhook really does
    point at localhost. It defaults to False so a production deployment cannot
    be talked into calling its own metadata service by a value someone typed
    into a settings form.
    """
    refusal = _reject_unsafe_target(target, allow_private=allow_private)
    if refusal:
        logger.warning("refusing to notify %s: %s", channel, refusal)
        return DeliveryResult(delivered=False, error=f"target rejected: {refusal}")

    owned = client is None
    http = client or httpx.Client(timeout=TIMEOUT_SECONDS)

    try:
        response = http.post(target, json=payload)
        # Slack answers 200 with a body of "ok"; a generic webhook may answer
        # 201 or 204. Anything 2xx counts as delivered.
        if 200 <= response.status_code < 300:
            return DeliveryResult(
                delivered=True,
                status_code=response.status_code,
                delivered_at=datetime.now(UTC),
            )
        return DeliveryResult(
            delivered=False,
            status_code=response.status_code,
            error=response.text[:300] or f"HTTP {response.status_code}",
        )
    except Exception as exc:  # noqa: BLE001 - a dead webhook must not fail a run
        logger.warning("notification to %s failed: %s", channel, exc)
        return DeliveryResult(delivered=False, error=str(exc)[:300])
    finally:
        if owned:
            http.close()


def render(event: str, data: dict) -> tuple[str, list[str]]:
    """Turn an event into a title and summary lines.

    One place that decides what each event *says*, so Slack and a webhook never
    describe the same thing differently.
    """
    if event == "gate_blocked":
        reasons = data.get("reasons") or []
        return (
            f"Quality gate blocked: {data.get('project', 'a project')}",
            reasons or ["The gate blocked this change."],
        )

    if event == "critical_defect":
        return (
            f"{data.get('severity', 'critical').title()} defect: {data.get('title', 'untitled')}",
            [
                f"*{data.get('reference', 'BUG')}* in {data.get('project', 'a project')}",
                str(data.get("root_cause") or "")[:500],
            ],
        )

    if event == "run_finished":
        return (
            f"QA run finished: {data.get('project', 'a project')}",
            [
                f"{data.get('passed', 0)} passed, {data.get('failed', 0)} failed, "
                f"{data.get('bugs', 0)} defect(s)"
            ],
        )

    if event == "security_finding":
        return (
            f"Security finding: {data.get('title', 'untitled')}",
            [f"{data.get('severity', 'unknown')} - {data.get('path', 'unknown location')}"],
        )

    # An unknown event still gets delivered rather than dropped: a caller that
    # added one and forgot to teach this function about it should see a plain
    # message, not silence.
    return (f"QAgent: {event}", [str(data)[:500]])
