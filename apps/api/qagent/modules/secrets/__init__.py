"""QAgent secrets: resolve an environment's `secret_ref` into request headers
at execution time (ADR-0004, CLAUDE.md section 22)."""

from qagent.modules.secrets.store import (
    InvalidSecretRef,
    SecretNotFound,
    SecretStore,
    headers_from_secret,
    resolve_auth_headers,
    store_from_settings,
)

__all__ = [
    "InvalidSecretRef",
    "SecretNotFound",
    "SecretStore",
    "headers_from_secret",
    "resolve_auth_headers",
    "store_from_settings",
]
