"""Environment credentials resolve from a secret *reference* at run time
(modules/secrets/, ADR-0004) - no database, no network."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from qagent.modules.secrets.store import (
    EnvSecretStore,
    FileSecretStore,
    InvalidSecretRef,
    SecretNotFound,
    env_var_name,
    headers_from_secret,
    resolve_auth_headers,
    store_from_settings,
)

ORG = uuid.UUID("0123456789abcdef0123456789abcdef")
OTHER_ORG = uuid.UUID("fedcba9876543210fedcba9876543210")
ORG_VAR = "QAGENT_ENV_SECRET_0123456789ABCDEF0123456789ABCDEF_"


def _org_dir(root: Path) -> Path:
    directory = root / ORG.hex
    directory.mkdir(exist_ok=True)
    return directory


def test_no_ref_means_no_credentials() -> None:
    store = EnvSecretStore({})
    assert resolve_auth_headers(None, store, org_id=ORG) == {}
    assert resolve_auth_headers("", store, org_id=ORG) == {}


def test_bare_token_becomes_a_bearer_header() -> None:
    assert headers_from_secret("  abc123\n") == {"Authorization": "Bearer abc123"}


def test_json_object_is_used_as_headers_verbatim() -> None:
    value = '{"Authorization": "Basic Zm9vOmJhcg==", "X-Api-Key": "k"}'
    assert headers_from_secret(value) == {
        "Authorization": "Basic Zm9vOmJhcg==",
        "X-Api-Key": "k",
    }


@pytest.mark.parametrize("value", ['{"a": 1}', "{}", '{"broken"'])
def test_malformed_json_secret_is_rejected_without_echoing_it(value: str) -> None:
    with pytest.raises(ValueError) as caught:
        headers_from_secret(value)
    assert value not in str(caught.value)


def test_env_var_name_is_org_scoped_and_normalised() -> None:
    assert env_var_name("staging-api.token", org_id=ORG) == ORG_VAR + "STAGING_API_TOKEN"


def test_env_store_reads_the_org_scoped_variable() -> None:
    store = EnvSecretStore({ORG_VAR + "SHOP_STAGING": "tok"})
    assert resolve_auth_headers("shop-staging", store, org_id=ORG) == {
        "Authorization": "Bearer tok"
    }


def test_env_store_missing_secret_names_the_variable_not_a_value() -> None:
    with pytest.raises(SecretNotFound, match=r"\$QAGENT_ENV_SECRET_[0-9A-F]{32}_MISSING"):
        EnvSecretStore({}).get("missing", org_id=ORG)


def test_one_org_cannot_resolve_another_orgs_secret(tmp_path: Path) -> None:
    with pytest.raises(SecretNotFound):
        EnvSecretStore({ORG_VAR + "SHOP": "tok"}).get("shop", org_id=OTHER_ORG)

    (_org_dir(tmp_path) / "shop").write_text("tok", encoding="utf-8")
    with pytest.raises(SecretNotFound):
        FileSecretStore(tmp_path).get("shop", org_id=OTHER_ORG)


def test_the_platform_signing_key_is_not_reachable_as_a_ref() -> None:
    # The prefix must not overlap QAGENT_SECRET_KEY (config.py).
    store = EnvSecretStore({"QAGENT_SECRET_KEY": "platform-signing-key"})
    with pytest.raises(SecretNotFound):
        store.get("key", org_id=ORG)


def test_file_store_reads_and_strips(tmp_path: Path) -> None:
    (_org_dir(tmp_path) / "shop").write_text("tok\n", encoding="utf-8")
    assert FileSecretStore(tmp_path).get("shop", org_id=ORG) == "tok"


def test_file_store_missing_and_empty(tmp_path: Path) -> None:
    (_org_dir(tmp_path) / "empty").write_text("  \n", encoding="utf-8")
    store = FileSecretStore(tmp_path)
    with pytest.raises(SecretNotFound):
        store.get("absent", org_id=ORG)
    with pytest.raises(SecretNotFound):
        store.get("empty", org_id=ORG)


@pytest.mark.parametrize("ref", ["../etc/passwd", "a/b", ".hidden", "x" * 201, "a b"])
def test_refs_that_are_not_plain_names_are_refused(tmp_path: Path, ref: str) -> None:
    with pytest.raises(InvalidSecretRef):
        FileSecretStore(tmp_path).get(ref, org_id=ORG)
    with pytest.raises(InvalidSecretRef):
        EnvSecretStore({}).get(ref, org_id=ORG)


def test_store_from_settings_selects_backend(tmp_path: Path) -> None:
    file_settings = SimpleNamespace(qagent_secrets_backend="file", qagent_secrets_dir=tmp_path)
    env_settings = SimpleNamespace(qagent_secrets_backend="env", qagent_secrets_dir=tmp_path)
    assert isinstance(store_from_settings(file_settings), FileSecretStore)
    assert isinstance(store_from_settings(env_settings), EnvSecretStore)


def test_environment_payload_rejects_a_path_as_secret_ref() -> None:
    from pydantic import ValidationError

    from qagent.main import EnvironmentIn

    assert EnvironmentIn(base_url="http://x", secret_ref="shop").secret_ref == "shop"
    with pytest.raises(ValidationError):
        EnvironmentIn(base_url="http://x", secret_ref="../../etc/passwd")
