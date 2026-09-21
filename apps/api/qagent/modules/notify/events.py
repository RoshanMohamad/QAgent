"""Decide when to notify, build the row, and send it.

`dispatch.py` knows how to deliver a payload. This knows *whether to*, which is
the harder half: a notifier that fires on everything gets muted within a week,
and a muted notifier is worse than none because the team stops watching the
dashboard expecting to be paged instead.

So the default event set is deliberately short - a blocked gate and a critical
or high defect. "A run finished" is available and off by default, because a run
finishing is not news.

Every send is persisted first and updated after, so a delivery that fails
leaves a row saying so. `retry_failed` is what turns that row back into an
attempt.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from qagent import models
from qagent.modules.notify.dispatch import render, send, slack_payload, webhook_payload

logger = logging.getLogger(__name__)

#: Fired unless a project says otherwise. Short on purpose - see the module
#: docstring on why an over-eager notifier ends up muted.
DEFAULT_EVENTS = ("gate_blocked", "critical_defect")

#: Severities that justify interrupting someone.
PAGING_SEVERITIES = {"critical", "high"}

#: A failed delivery is retried this many times before it is left alone. Small,
#: because a webhook that has rejected a message five times is misconfigured,
#: not busy, and retrying forever just fills the table.
MAX_ATTEMPTS = 5


def _config(project: models.Project | None) -> dict | None:
    """The project's notification settings, or None when it has none."""
    if project is None:
        return None
    config = project.notify or {}
    target = str(config.get("target") or "").strip()
    channel = str(config.get("channel") or "").strip()
    if not target or channel not in {"slack", "webhook"}:
        return None
    return {
        "channel": channel,
        "target": target,
        "events": tuple(config.get("events") or DEFAULT_EVENTS),
    }


def _build_payload(channel: str, event: str, data: dict) -> dict:
    title, lines = render(event, data)
    if channel == "slack":
        return slack_payload(event=event, title=title, lines=lines)
    return webhook_payload(event=event, title=title, lines=lines, data=data)


def notify(
    session: Session,
    *,
    org_id: UUID,
    project_id: UUID | None,
    event: str,
    data: dict,
    allow_private: bool = False,
) -> models.Notification | None:
    """Record and attempt one notification. Returns None when not configured.

    Never raises. This is called from the worker after a scan and from the API
    after a gate decision, and neither should fail because a Slack webhook is
    down - the scan still ran, the gate still blocked.
    """
    project = session.get(models.Project, project_id) if project_id else None
    config = _config(project)
    if config is None or event not in config["events"]:
        return None

    payload = _build_payload(config["channel"], event, data)
    notification = models.Notification(
        org_id=org_id,
        project_id=project_id,
        event=event,
        channel=config["channel"],
        target=config["target"],
        payload=payload,
        status="pending",
    )
    session.add(notification)
    session.flush()

    result = send(
        channel=config["channel"],
        target=config["target"],
        payload=payload,
        allow_private=allow_private,
    )

    notification.attempts += 1
    notification.status = "delivered" if result.delivered else "failed"
    notification.last_error = result.error
    notification.delivered_at = result.delivered_at

    logger.info(
        "notification %s for %s: %s", notification.status, event, result.error or "ok"
    )
    return notification


def notify_run_finished(
    session: Session,
    *,
    org_id: UUID,
    project_id: UUID,
    run: models.TestRun,
    result,
    allow_private: bool = False,
) -> list[models.Notification]:
    """Fire whatever a finished scan warrants.

    One notification per *severity class*, not per defect: a run that finds
    eleven high-severity defects should produce one message naming the worst,
    not eleven messages nobody reads to the end.
    """
    sent: list[models.Notification] = []

    worst = None
    for outcome in result.bugs:
        severity = str((outcome.bug or {}).get("severity", "")).lower()
        if severity in PAGING_SEVERITIES and (worst is None or severity == "critical"):
            worst = outcome
            if severity == "critical":
                break

    if worst is not None:
        bug = worst.bug or {}
        notification = notify(
            session,
            org_id=org_id,
            project_id=project_id,
            event="critical_defect",
            data={
                "severity": bug.get("severity"),
                "title": bug.get("title"),
                "root_cause": bug.get("root_cause"),
                "project": str(project_id),
                "reference": "",
                "total_defects": len(result.bugs),
            },
            allow_private=allow_private,
        )
        if notification:
            sent.append(notification)

    notification = notify(
        session,
        org_id=org_id,
        project_id=project_id,
        event="run_finished",
        data={
            "project": str(project_id),
            "passed": result.passed,
            "failed": result.failed,
            "bugs": len(result.bugs),
        },
        allow_private=allow_private,
    )
    if notification:
        sent.append(notification)

    return sent


def retry_failed(session: Session, *, limit: int = 50, allow_private: bool = False) -> dict:
    """Re-attempt deliveries that failed, up to `MAX_ATTEMPTS`.

    The `attempts` and `last_error` columns existed from the start and nothing
    ever read them, which made a failed notification a permanent dead row. This
    is what makes them mean something.
    """
    rows = session.execute(
        select(models.Notification)
        .where(
            models.Notification.status == "failed",
            models.Notification.attempts < MAX_ATTEMPTS,
        )
        .order_by(models.Notification.created_at)
        .limit(limit)
    ).scalars().all()

    delivered = 0
    for notification in rows:
        result = send(
            channel=notification.channel,
            target=notification.target,
            payload=notification.payload,
            allow_private=allow_private,
        )
        notification.attempts += 1
        notification.last_error = result.error
        if result.delivered:
            notification.status = "delivered"
            notification.delivered_at = result.delivered_at
            delivered += 1
        elif notification.attempts >= MAX_ATTEMPTS:
            # Terminal, and labelled as such: a row stuck at "failed" forever
            # is indistinguishable from one still waiting for its next attempt.
            notification.status = "abandoned"

    logger.info("notification retry: %d/%d delivered", delivered, len(rows))
    return {"attempted": len(rows), "delivered": delivered}
