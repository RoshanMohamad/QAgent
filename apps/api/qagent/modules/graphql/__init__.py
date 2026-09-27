"""GraphQL support (CLAUDE.md section 6: "REST APIs, GraphQL, OpenAPI").

A GraphQL API answers almost everything with ``200 OK`` - a resolver that
crashed and a resolver that correctly said "not found" look identical at the
HTTP layer, and the difference lives in the ``errors`` array. That is the
whole reason this is a module rather than a second OpenAPI parser: discovery
(``introspection.py``) is the easy half; reading an ``errors`` array the way
the triage classifier reads a status code (``errors.py``) is the half every
other stage depends on.
"""
