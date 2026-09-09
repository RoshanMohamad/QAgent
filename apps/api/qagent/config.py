"""Runtime configuration.

Every value is environment-driven; nothing is read from disk at import time so the
same image runs as API, worker or CLI without modification.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False, extra="ignore")

    env: str = "development"
    secret_key: str = "change-me-in-production"  # noqa: S105 - placeholder, not a secret

    database_url: str = "postgresql+psycopg://qagent:qagent@localhost:5432/qagent"
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

    # --- Budgets, enforced per agent run (see modules/llm/budget.py) ---
    qagent_max_llm_calls: int = 40
    qagent_max_llm_tokens: int = 200_000
    qagent_max_usd: float = 2.00

    # --- Runner sandbox ---
    qagent_runner_timeout_seconds: int = 120
    qagent_runner_max_concurrency: int = 4
    qagent_egress_allowlist: str = ""

    @property
    def egress_allowlist(self) -> list[str]:
        return [h.strip() for h in self.qagent_egress_allowlist.split(",") if h.strip()]

    @property
    def is_production(self) -> bool:
        return self.env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
