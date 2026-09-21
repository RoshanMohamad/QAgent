"""HAR 1.2 archives for defect evidence (CLAUDE.md section 15).

Until now only browser-E2E defects carried evidence: a screenshot and a console
log. An API defect - which is most of what this tool finds - carried nothing but
prose. "The handler returned 500" is a claim the reader has to take on faith,
and reproducing it means retyping the request by hand from a bug report.

A HAR fixes that, and the format choice is the whole point: HAR is what Chrome
DevTools, Firefox, Charles, Fiddler, Insomnia and Postman all import. A defect
that ships with one can be replayed by dropping the file into a tool the
developer already has open, with no QAgent involved.

**Everything written here is scrubbed first.** A HAR records headers verbatim,
which means `Authorization: Bearer ...` verbatim, and a bug report is pasted
into issue trackers and chat. `modules/llm/safety.scrub` already exists for
exactly this and is applied to every value, and credential-bearing headers are
replaced outright rather than pattern-matched.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from qagent.modules.llm.safety import scrub

logger = logging.getLogger(__name__)

HAR_VERSION = "1.2"
CREATOR = {"name": "QAgent", "version": "0.1.0"}

#: Replaced wholesale, not scrubbed by pattern. A scrubber works on recognisable
#: shapes; a session cookie or a bespoke API key header has no shape to
#: recognise, and guessing wrong here leaks a live credential into a file
#: designed to be shared.
_CREDENTIAL_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
        "x-csrf-token",
        "api-key",
    }
)

REDACTED = "[redacted]"

#: Bodies are truncated before they are stored. An endpoint that returns a
#: 40MB export would otherwise put 40MB into object storage per failing check.
MAX_BODY_CHARS = 200_000


def _headers(raw: dict[str, Any] | None) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for name, value in (raw or {}).items():
        lowered = str(name).lower()
        safe = REDACTED if lowered in _CREDENTIAL_HEADERS else scrub(str(value))
        out.append({"name": str(name), "value": safe})
    return out


def _query(url: str) -> list[dict[str, str]]:
    from urllib.parse import parse_qsl

    return [
        {"name": name, "value": scrub(value)}
        for name, value in parse_qsl(urlparse(url).query)[:50]
    ]


def _body_text(value: Any) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS] + "\n... (truncated by QAgent)"
    return scrub(text)


def build_entry(
    *,
    url: str,
    method: str,
    request_headers: dict | None = None,
    request_body: Any = None,
    status: int | None = None,
    response_headers: dict | None = None,
    response_body: Any = None,
    duration_ms: int = 0,
    started_at: datetime | None = None,
) -> dict:
    """One HAR entry. Split out so a caller can assemble several."""
    request_text = _body_text(request_body)
    response_text = _body_text(response_body)

    return {
        "startedDateTime": (started_at or datetime.now(UTC)).isoformat(),
        "time": duration_ms,
        "request": {
            "method": str(method or "GET").upper(),
            "url": url,
            "httpVersion": "HTTP/1.1",
            "headers": _headers(request_headers),
            "queryString": _query(url),
            "cookies": [],
            "headersSize": -1,
            "bodySize": len(request_text),
            **(
                {
                    "postData": {
                        "mimeType": "application/json",
                        "text": request_text,
                    }
                }
                if request_text
                else {}
            ),
        },
        "response": {
            # -1 rather than 0 when the request never completed: HAR's own
            # convention for "unknown", and a viewer renders it as such instead
            # of showing a misleading "0 OK".
            "status": status if status is not None else -1,
            "statusText": "",
            "httpVersion": "HTTP/1.1",
            "headers": _headers(response_headers),
            "cookies": [],
            "content": {
                "size": len(response_text),
                "mimeType": "application/json",
                "text": response_text,
            },
            "redirectURL": "",
            "headersSize": -1,
            "bodySize": len(response_text),
        },
        "cache": {},
        "timings": {"send": 0, "wait": duration_ms, "receive": 0},
    }


def build_har(entries: list[dict], *, page_url: str | None = None) -> dict:
    """Wrap entries in a HAR log a viewer will accept."""
    log: dict = {
        "version": HAR_VERSION,
        "creator": CREATOR,
        "entries": entries,
    }
    if page_url:
        log["pages"] = [
            {
                "startedDateTime": datetime.now(UTC).isoformat(),
                "id": "page_1",
                "title": page_url,
                "pageTimings": {},
            }
        ]
        for entry in entries:
            entry.setdefault("pageref", "page_1")
    return {"log": log}


def har_for_check(
    *,
    base_url: str,
    request: dict,
    response: dict,
    duration_ms: int = 0,
) -> bytes:
    """The HAR for one executed API check, ready to store as an artifact.

    Takes the runner's own `request`/`response` dicts rather than an httpx
    object, so this works for a check that never completed - a connection
    refused still produces a replayable request, which is exactly the evidence
    needed to tell an environment failure from a defect.
    """
    path = str(request.get("path") or "/")
    url = path if path.startswith("http") else f"{base_url.rstrip('/')}{path}"

    entry = build_entry(
        url=url,
        method=str(request.get("method") or "GET"),
        request_headers=request.get("headers"),
        request_body=request.get("json"),
        status=response.get("status"),
        response_headers=response.get("headers"),
        response_body=response.get("body_text") or response.get("body_json"),
        duration_ms=duration_ms,
    )
    document = build_har([entry])
    return json.dumps(document, indent=2).encode("utf-8")


def har_from_requests(entries: list[dict], *, page_url: str | None = None) -> bytes:
    """A HAR from a browser stage's recorded traffic."""
    built = [
        build_entry(
            url=str(item.get("url") or ""),
            method=str(item.get("method") or "GET"),
            request_headers=item.get("request_headers"),
            request_body=item.get("request_body"),
            status=item.get("status"),
            response_headers=item.get("response_headers"),
            response_body=item.get("response_body"),
            duration_ms=int(item.get("duration_ms") or 0),
        )
        for item in entries
    ]
    return json.dumps(build_har(built, page_url=page_url), indent=2).encode("utf-8")
