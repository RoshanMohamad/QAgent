"""Turn a secret *reference* into credentials, for the duration of one run.

An environment row stores `secret_ref` - a name - and never the credential
(ADR-0004). This module is the only place that name becomes a value, and it
does so inside the worker, immediately before the pipeline needs it. Nothing
here writes the value anywhere: the pipeline sends it as request headers, and
`modules/evidence/har.py` replaces credential headers wholesale before any
evidence is persisted.

Two backends, both of which every orchestrator already speaks:

- ``env``: the value lives in an environment variable,
  ``QAGENT_ENV_SECRET_<ORG>_<REF>`` (``<ORG>`` is the organization id as 32 hex
  digits; the ref is upper-cased with ``-``/``.`` mapped to ``_``). This is what
  a Kubernetes ``secretKeyRef``, a compose ``env_file`` or a CI secret produces.
- ``file``: the value is the file ``<QAGENT_SECRETS_DIR>/<org id>/<ref>``. This
  is what a Kubernetes secret *volume* or Docker ``/run/secrets`` produces, and
  it keeps the credential out of the process environment entirely.

**Every lookup is scoped to the organization.** A ref is chosen by a tenant, so
an unscoped ref would let one tenant name - and send to a URL it controls -
another tenant's credential. The org id comes from the run, never the request.
The env prefix is deliberately not ``QAGENT_SECRET_``: that would make the
platform's own ``QAGENT_SECRET_KEY`` reachable as the ref ``key``.

A vault client (HashiCorp Vault, AWS Secrets Manager) would be a third class
with the same one method. Neither of those is built because both are
deployment-specific, and the two above are what those tools inject into a
container anyway.

**Value format.** Either a JSON object of headers -
``{"Authorization": "Bearer ...", "X-Api-Key": "..."}`` - or a bare token,
which becomes ``Authorization: Bearer <token>``. The bare form exists because
it is overwhelmingly the common case and asking for JSON to hold one string
invites quoting mistakes in a secret nobody can read back.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

#: A ref is a name, not a path. Anything else is refused before any lookup, so
#: the file backend can never be walked out of its directory (``../../etc``).
_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")

ENV_PREFIX = "QAGENT_ENV_SECRET_"


class SecretNotFound(LookupError):
    """The reference is well-formed but no secret exists under that name.

    Never carries the value, and the message names only the reference - it ends
    up on a run's error and in logs.
    """


class InvalidSecretRef(ValueError):
    """The reference is not a plain name."""


class SecretStore(Protocol):
    def get(self, ref: str, *, org_id: UUID) -> str: ...


def validate_ref(ref: str) -> str:
    if not _REF_PATTERN.fullmatch(ref):
        raise InvalidSecretRef(
            "secret_ref must be 1-200 characters of letters, digits, '_', '-' or '.', "
            "starting with a letter or digit"
        )
    return ref


def env_var_name(ref: str, *, org_id: UUID) -> str:
    return f"{ENV_PREFIX}{org_id.hex.upper()}_" + re.sub(r"[-.]", "_", validate_ref(ref)).upper()


class EnvSecretStore:
    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = environ if environ is not None else os.environ

    def get(self, ref: str, *, org_id: UUID) -> str:
        name = env_var_name(ref, org_id=org_id)
        value = self._environ.get(name)
        if not value:
            raise SecretNotFound(f"secret {ref!r} not found (expected ${name})")
        return value


class FileSecretStore:
    def __init__(self, directory: Path | str) -> None:
        self._directory = Path(directory)

    def get(self, ref: str, *, org_id: UUID) -> str:
        path = self._directory / org_id.hex / validate_ref(ref)
        try:
            value = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            raise SecretNotFound(f"secret {ref!r} not found in secrets directory") from None
        if not value:
            raise SecretNotFound(f"secret {ref!r} is empty")
        return value


def headers_from_secret(value: str) -> dict[str, str]:
    """Parse a secret value into headers (see module docstring for the format)."""
    text = value.strip()
    if text.startswith("{"):
        try:
            parsed: Any = json.loads(text)
        except json.JSONDecodeError:
            # Deliberately no detail: a JSON error message quotes the input.
            raise ValueError("secret looks like JSON but does not parse") from None
        if not isinstance(parsed, dict) or not parsed:
            raise ValueError("a JSON secret must be a non-empty object of headers")
        headers: dict[str, str] = {}
        for name, header_value in parsed.items():
            if not isinstance(name, str) or not isinstance(header_value, str):
                raise ValueError("every header name and value in a secret must be a string")
            headers[name] = header_value
        return headers
    return {"Authorization": f"Bearer {text}"}


def store_from_settings(settings: Any) -> SecretStore:
    if settings.qagent_secrets_backend == "file":
        return FileSecretStore(settings.qagent_secrets_dir)
    return EnvSecretStore()


def resolve_auth_headers(
    ref: str | None, store: SecretStore, *, org_id: UUID
) -> dict[str, str]:
    """Headers for ``ref`` in ``org_id``'s namespace, or ``{}`` when the
    environment has no credentials."""
    if not ref:
        return {}
    return headers_from_secret(store.get(ref, org_id=org_id))
