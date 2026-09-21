"""Broken object-level authorization, a.k.a. IDOR (CLAUDE.md section 12).

The one seeded defect in `packages/fixtures/task-tracker` the rule set could not
catch, and the README has said so in writing rather than quietly dropping the
fixture. This closes it.

**Why it could not be a generator rule.** Every rule in `generator/rules.py`
builds one self-contained request from an endpoint's shape. IDOR is not a
property of one request: it is the difference between what *two identities* can
reach, and finding it requires state - you have to learn an identifier that
belongs to user A before asking whether user B can read it. A stateless rule has
nowhere to put that first step.

So this is a probe, not a rule:

    1. As identity A, list a collection and take an identifier from it.
    2. As identity B, request that identifier directly.
    3. If B gets it, compare bodies. Same content = A's data leaked to B.

**Step 3 is what keeps the false-positive rate honest.** A 200 alone proves
nothing: plenty of APIs legitimately return a shared or filtered resource to
anyone authenticated. Only an *equal* response - compared as parsed JSON, so
formatting cannot hide a leak nor fake one - is evidence that B read A's row.
Anything weaker is dropped, and anything unverifiable is reported as
inconclusive rather than as a clean result.

The probe never runs unless a second identity is configured, and it never
mutates: only safe methods are probed, because confirming a write-side IDOR
means actually writing to someone else's data.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from qagent.modules.security.base import SecurityFinding

logger = logging.getLogger(__name__)

#: Only ever probed with safe methods. Confirming that B can DELETE A's order
#: requires deleting A's order, which is not a thing a QA tool may do to find
#: out whether it was allowed to.
SAFE_METHODS = {"GET", "HEAD"}

#: Keys whose values look like resource identifiers in a collection response.
_ID_KEYS = ("id", "uuid", "_id", "pk", "identifier")

#: How many identifiers to try per endpoint. One is usually enough; a couple
#: guards against the first row happening to be shared or public.
MAX_IDS_PER_ENDPOINT = 3

_PATH_PARAM = re.compile(r"\{[^}]+\}|:[A-Za-z_]\w*")


@dataclass
class IdorProbeResult:
    findings: list[SecurityFinding] = field(default_factory=list)
    #: Endpoints that were probed but produced no verdict, with the reason.
    #: Carried because "we found no IDOR" and "we could not check" are
    #: different claims and a security report must not conflate them.
    inconclusive: dict[str, str] = field(default_factory=dict)
    probed: int = 0

    def to_dict(self) -> dict:
        return {
            "findings": [f.to_dict() for f in self.findings],
            "inconclusive": self.inconclusive,
            "probed": self.probed,
        }


def collection_path(path: str) -> str | None:
    """``/tasks/{task_id}`` -> ``/tasks``. None when there is no parent."""
    parts = [p for p in path.split("/") if p]
    if not parts or not _PATH_PARAM.fullmatch(parts[-1]):
        return None
    parent = "/" + "/".join(parts[:-1])
    return parent if parent != "/" else None


def extract_ids(payload: Any, limit: int = MAX_IDS_PER_ENDPOINT) -> list[str]:
    """Pull plausible resource identifiers out of a collection response.

    Handles the two shapes that cover almost everything: a bare list of
    objects, and an envelope (`{"items": [...]}`, `{"data": [...]}`).
    """
    items: list = []
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        for key in ("items", "data", "results", "records"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break

    found: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in _ID_KEYS:
            value = item.get(key)
            if isinstance(value, str | int) and str(value):
                found.append(str(value))
                break
        if len(found) >= limit:
            break
    return found


def _bodies_match(first: str, second: str) -> bool:
    """Whether two responses carry the same resource.

    Compared as parsed JSON when both sides parse, so serialisation differences
    - key order, indentation, separator spacing - cannot hide a real leak. A
    server has no obligation to byte-match its own output across two requests,
    and requiring it to would make this probe miss the defect it exists to find.

    Falls back to whitespace-collapsed text for non-JSON. Deliberately strict
    either way: a partial overlap is not evidence, and treating it as evidence
    is how an access-control scanner starts crying wolf and gets switched off.
    """
    if not first or not second:
        return False

    try:
        return json.loads(first) == json.loads(second)
    except (json.JSONDecodeError, TypeError):
        return " ".join(first.split()) == " ".join(second.split())


def probe_endpoints(
    endpoints: list,
    *,
    request,
    min_body_chars: int = 2,
) -> IdorProbeResult:
    """Run the three-step probe over every eligible endpoint.

    ``request`` is a callable ``(method, path, identity) -> (status, body_text,
    body_json)``. Injected rather than importing the runner so this is testable
    without a live target, and so the caller keeps control of egress policy.
    """
    result = IdorProbeResult()

    for endpoint in endpoints:
        method = str(getattr(endpoint, "method", "GET")).upper()
        path = str(getattr(endpoint, "path", ""))
        key = f"{method} {path}"

        if method not in SAFE_METHODS or not _PATH_PARAM.search(path):
            continue
        if not getattr(endpoint, "requires_auth", False):
            # An endpoint that never claimed to be protected is not an
            # authorization failure; it is a public endpoint.
            continue

        parent = collection_path(path)
        if parent is None:
            result.inconclusive[key] = "no collection endpoint to learn an identifier from"
            continue

        result.probed += 1

        status, _, listing = request("GET", parent, "primary")
        if status is None or not (200 <= status < 300):
            result.inconclusive[key] = f"could not list {parent} as the first identity"
            continue

        ids = extract_ids(listing)
        if not ids:
            result.inconclusive[key] = f"no identifiers found in {parent}"
            continue

        confirmed = False
        for resource_id in ids:
            concrete = _PATH_PARAM.sub(str(resource_id), path, count=1)

            owner_status, owner_body, _ = request("GET", concrete, "primary")
            if owner_status is None or not (200 <= owner_status < 300):
                continue
            if len(owner_body or "") < min_body_chars:
                continue

            other_status, other_body, _ = request("GET", concrete, "secondary")
            if other_status is None or not (200 <= other_status < 300):
                # The expected, correct outcome: 403 or 404 for someone else's
                # resource. Nothing to report.
                continue

            if not _bodies_match(owner_body or "", other_body or ""):
                # B got *something*, but not A's row - a filtered or shared
                # view. Not evidence, and reporting it would be the kind of
                # false positive that gets a scanner switched off.
                continue

            result.findings.append(
                SecurityFinding(
                    rule_id="qagent-idor",
                    title="Broken object-level authorization (IDOR)",
                    severity="high",
                    path=key,
                    line=0,
                    message=(
                        f"A second authenticated identity received the identical response "
                        f"for {concrete} as the identity that owns it (HTTP "
                        f"{other_status}). The handler authenticates the caller but never "
                        f"checks that the caller owns the requested resource."
                    ),
                    confidence="high",
                    cwe=["CWE-639"],
                    owasp=["A01:2021 - Broken Access Control"],
                    scanner="qagent-idor",
                )
            )
            confirmed = True
            break

        if not confirmed and key not in result.inconclusive:
            logger.debug("no IDOR evidence for %s", key)

    logger.info(
        "IDOR probe: %d endpoint(s) probed, %d finding(s), %d inconclusive",
        result.probed,
        len(result.findings),
        len(result.inconclusive),
    )
    return result
