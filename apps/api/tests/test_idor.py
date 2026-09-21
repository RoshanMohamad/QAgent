"""The IDOR probe: the one seeded defect the rule set could not reach.

The negative tests carry the weight here. An access-control scanner that cries
wolf gets switched off, and this project's primary metric is false-positive rate
(ADR-0003) - so every case where a second identity gets *a* response but not
*the owner's* response must come back clean.
"""

from __future__ import annotations

from qagent.modules.discovery.openapi import EndpointSpec
from qagent.modules.security.idor import (
    collection_path,
    extract_ids,
    probe_endpoints,
)


def _endpoint(path: str = "/tasks/{task_id}", method: str = "GET", auth: bool = True):
    return EndpointSpec(method=method, path=path, requires_auth=auth)


class _Api:
    """A scripted application. `owner_only` decides whether it has the bug."""

    def __init__(self, *, leaks: bool, listing=None, body: str = '{"id":1,"owner":"ada"}'):
        self.leaks = leaks
        self.listing = listing if listing is not None else [{"id": 1}, {"id": 2}]
        self.body = body
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, method: str, path: str, identity: str):
        self.calls.append((method, path, identity))
        if path == "/tasks":
            return 200, "", self.listing
        if identity == "primary":
            return 200, self.body, None
        return (200, self.body, None) if self.leaks else (404, '{"detail":"not found"}', None)


# ------------------------------------------------------------------ detection


def test_a_leaking_endpoint_is_reported() -> None:
    api = _Api(leaks=True)

    result = probe_endpoints([_endpoint()], request=api)

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.severity == "high"
    assert finding.cwe == ["CWE-639"]
    assert "A01" in finding.owasp[0]


def test_a_correct_endpoint_is_not_reported() -> None:
    result = probe_endpoints([_endpoint()], request=_Api(leaks=False))

    assert result.findings == []


def test_the_probe_actually_used_two_identities() -> None:
    api = _Api(leaks=True)

    probe_endpoints([_endpoint()], request=api)

    identities = {identity for _, _, identity in api.calls}
    assert identities == {"primary", "secondary"}


def test_it_learns_an_identifier_from_the_collection_first() -> None:
    """Step one: a stateless rule has nowhere to put this, which is why the
    probe exists at all."""
    api = _Api(leaks=True)

    probe_endpoints([_endpoint()], request=api)

    assert api.calls[0] == ("GET", "/tasks", "primary")
    assert ("GET", "/tasks/1", "secondary") in api.calls


# ------------------------------------------------------------ false positives


def test_a_different_body_is_not_evidence() -> None:
    """B got something, but not A's row - a filtered or shared view."""

    class _Filtered(_Api):
        def __call__(self, method, path, identity):
            self.calls.append((method, path, identity))
            if path == "/tasks":
                return 200, "", self.listing
            if identity == "primary":
                return 200, '{"id":1,"owner":"ada","secret":"x"}', None
            return 200, '{"id":1,"public":true}', None

    result = probe_endpoints([_endpoint()], request=_Filtered(leaks=True))

    assert result.findings == []


def test_whitespace_differences_do_not_hide_a_real_leak() -> None:
    class _Reformatted(_Api):
        def __call__(self, method, path, identity):
            self.calls.append((method, path, identity))
            if path == "/tasks":
                return 200, "", self.listing
            if identity == "primary":
                return 200, '{"id": 1, "owner": "ada"}', None
            return 200, '{"id":1,\n  "owner":"ada"}', None

    result = probe_endpoints([_endpoint()], request=_Reformatted(leaks=True))

    assert len(result.findings) == 1


def test_an_empty_body_is_never_evidence() -> None:
    result = probe_endpoints([_endpoint()], request=_Api(leaks=True, body=""))

    assert result.findings == []


def test_a_public_endpoint_is_not_an_authorization_failure() -> None:
    result = probe_endpoints([_endpoint(auth=False)], request=_Api(leaks=True))

    assert result.findings == []
    assert result.probed == 0


def test_write_methods_are_never_probed() -> None:
    """Confirming that B can DELETE A's order requires deleting A's order."""
    api = _Api(leaks=True)

    result = probe_endpoints(
        [_endpoint(method="DELETE"), _endpoint(method="PUT"), _endpoint(method="POST")],
        request=api,
    )

    assert result.probed == 0
    assert api.calls == []


def test_an_endpoint_without_an_identifier_is_skipped() -> None:
    result = probe_endpoints([_endpoint(path="/tasks")], request=_Api(leaks=True))

    assert result.probed == 0


# ----------------------------------------------------------- inconclusive


def test_a_missing_collection_is_reported_as_inconclusive() -> None:
    """'We found no IDOR' and 'we could not check' are different claims."""
    result = probe_endpoints([_endpoint(path="/{task_id}")], request=_Api(leaks=True))

    assert result.findings == []
    assert "GET /{task_id}" in result.inconclusive


def test_an_unlistable_collection_is_reported_as_inconclusive() -> None:
    class _NoList(_Api):
        def __call__(self, method, path, identity):
            if path == "/tasks":
                return 403, "", None
            return 200, self.body, None

    result = probe_endpoints([_endpoint()], request=_NoList(leaks=True))

    assert "could not list" in result.inconclusive["GET /tasks/{task_id}"]


def test_a_collection_with_no_identifiers_is_inconclusive() -> None:
    result = probe_endpoints(
        [_endpoint()], request=_Api(leaks=True, listing=[{"name": "no id here"}])
    )

    assert "no identifiers" in result.inconclusive["GET /tasks/{task_id}"]


# -------------------------------------------------------------------- helpers


def test_collection_path_strips_the_identifier() -> None:
    assert collection_path("/tasks/{task_id}") == "/tasks"
    assert collection_path("/api/v1/orders/{id}") == "/api/v1/orders"
    assert collection_path("/api/v1/orders/:id") == "/api/v1/orders"
    # A trailing slash must not defeat it.
    assert collection_path("/api/v1/orders/{id}/") == "/api/v1/orders"
    # No identifier, or no parent to list.
    assert collection_path("/tasks") is None
    assert collection_path("/{id}") is None


def test_identifiers_are_found_in_both_common_shapes() -> None:
    assert extract_ids([{"id": 1}, {"id": 2}]) == ["1", "2"]
    assert extract_ids({"items": [{"uuid": "a"}]}) == ["a"]
    assert extract_ids({"data": [{"_id": "b"}]}) == ["b"]
    assert extract_ids({"nothing": 1}) == []
    assert extract_ids("not a collection") == []


def test_identifier_extraction_is_bounded() -> None:
    assert len(extract_ids([{"id": i} for i in range(50)])) == 3
