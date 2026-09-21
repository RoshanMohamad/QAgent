"""Runtime configuration.

Every value is environment-driven; nothing is read from disk at import time so the
same image runs as API, worker or CLI without modification.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False, extra="ignore")

    qagent_env: str = "development"
    qagent_secret_key: str = "change-me-in-production"  # noqa: S105 - placeholder, not a secret

    database_url: str = "postgresql+psycopg://qagent:qagent@localhost:5432/qagent"
    # Bootstrap-only: schema creation, application-role creation, and RLS policy
    # installation (qagent/db_init.py). Never used at request/task time - only
    # `database_url` is. Left unset, db_init falls back to `database_url` itself
    # and logs a loud warning, since that means whatever role the app connects as
    # is also the one bootstrapping its own restrictions (ADR-0007).
    admin_database_url: str | None = None
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    # --- LLM ---
    # "null" is a deterministic offline provider. It keeps CI and the eval harness
    # reproducible and lets the whole pipeline run with no API key configured.
    qagent_llm_provider: Literal["anthropic", "openai_compatible", "null"] = "null"
    anthropic_api_key: str | None = None
    qagent_llm_model: str = "claude-sonnet-5"
    qagent_llm_base_url: str | None = None

    # --- Repository RAG (modules/rag/) ---
    # "null" means lexical-only retrieval, which is a real retriever and the
    # default: BM25 over code is strong precisely because the identifiers being
    # searched for appear verbatim in the code that implements them. Embeddings
    # are an upgrade for the paraphrase cases, not a prerequisite.
    qagent_embedding_provider: Literal["openai_compatible", "null"] = "null"
    qagent_embedding_base_url: str | None = None
    qagent_embedding_api_key: str | None = None
    qagent_embedding_model: str = "text-embedding-3-small"
    qagent_embedding_dimensions: int = 1536
    # How vectors are stored. "json" works on stock postgres:16-alpine - the
    # image this project's own compose file runs - and searches in Python, which
    # at a few thousand chunks per project is milliseconds. "pgvector" needs
    # both the `pgvector` Python package (pip install qagent[rag]) and the
    # `vector` extension in the database, and gives native ANN search.
    #
    # This is a declared choice rather than an automatic upgrade on purpose. The
    # physical column type is fixed when SQLAlchemy defines the model, so a
    # runtime ALTER leaves the ORM still binding JSON into a vector column and
    # every insert fails. Either the deployment opts in and both prerequisites
    # are checked at init, or it does not and nothing mutates underneath it.
    qagent_vector_backend: Literal["json", "pgvector"] = "json"
    #: Chunks of repository source attached to a bug report as "affected code".
    #: Small on purpose: the point is to name the function, not to paste a file.
    qagent_rag_top_k: int = 4

    # --- Tracing (CLAUDE.md section 21) ---
    # Off by default and genuinely free when off: `span()` is a null context
    # manager and nothing imports opentelemetry. ADR-0008 deferred tracing
    # because sampling and backend choices need a real deployment - those are
    # still the operator's, which is what OTLP buys. Needs qagent[otel].
    qagent_tracing_enabled: bool = False
    qagent_tracing_endpoint: str = "http://localhost:4318/v1/traces"
    qagent_tracing_service_name: str = "qagent"

    # --- Billing (CLAUDE.md section 23) ---
    # QAgent meters and prices; it never settles. Every rate defaults to zero,
    # so an unconfigured deployment produces a statement with no amounts on it
    # rather than quietly billing something nobody decided
    # (modules/billing/statement.py, ADR-0008).
    qagent_billing_currency: str = "USD"
    qagent_billing_per_run: str = "0"
    qagent_billing_per_defect: str = "0"
    #: 0 means AI spend is not charged on; 1 is at-cost pass-through.
    qagent_billing_llm_markup: str = "0"
    qagent_billing_included_runs: int = 0

    # --- Budgets, enforced per agent run (see modules/llm/budget.py) ---
    qagent_max_llm_calls: int = 40
    qagent_max_llm_tokens: int = 200_000
    qagent_max_usd: float = 2.00

    # --- Runner sandbox ---
    qagent_runner_timeout_seconds: int = 120
    qagent_runner_max_concurrency: int = 4
    qagent_egress_allowlist: str = ""

    # --- Evidence artifacts (CLAUDE.md section 15) ---
    # "local" is a directory on disk and needs nothing. "s3" is any
    # S3-compatible service - S3 itself, Cloudflare R2, MinIO - and needs
    # `pip install qagent[s3]` plus a bucket. Both share one key layout
    # (modules/storage/base.py), so moving between them is a recursive copy.
    qagent_storage_backend: Literal["local", "s3"] = "local"
    qagent_artifact_root: str = "./data/artifacts"
    qagent_s3_bucket: str | None = None
    #: Unset for AWS; set for R2, MinIO, or any other S3-compatible endpoint.
    qagent_s3_endpoint_url: str | None = None
    qagent_s3_region: str | None = None
    #: Leave unset on AWS so boto3's own credential chain (environment, shared
    #: config, instance role) applies - that is what a real deployment should
    #: use. These exist for MinIO and local development.
    qagent_s3_access_key: str | None = None
    qagent_s3_secret_key: str | None = None

    @property
    def egress_allowlist(self) -> list[str]:
        return [h.strip() for h in self.qagent_egress_allowlist.split(",") if h.strip()]

    @property
    def is_production(self) -> bool:
        return self.qagent_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
