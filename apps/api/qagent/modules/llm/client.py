"""The single entry point through which every model call passes.

Centralising this is what makes the platform's AI layer auditable: budgets, cost
accounting, the untrusted-content system rule and the structured-output constraint
are applied here, once, rather than being remembered at forty call sites.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from qagent.config import Settings, get_settings
from qagent.modules.llm.budget import Budget, BudgetExceeded
from qagent.modules.llm.providers import LlmResult, Provider, build_provider
from qagent.modules.llm.safety import UNTRUSTED_SYSTEM_RULE

logger = logging.getLogger(__name__)

#: USD per million tokens, keyed by model. Left empty by default on purpose:
#: hardcoding prices that drift silently produces confidently wrong cost reports.
#: Populate from QAGENT_PRICING_JSON with values read off the current pricing page.
PRICE_PER_MTOK: dict[str, tuple[float, float]] = {}


def estimate_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    price = PRICE_PER_MTOK.get(model)
    if not price:
        return 0.0
    in_rate, out_rate = price
    return (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate


@dataclass
class CallRecord:
    """One model call, shaped for persistence into the llm_calls table."""

    purpose: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    usd: float
    latency_ms: int
    ok: bool


@dataclass
class LlmClient:
    settings: Settings
    provider: Provider
    budget: Budget
    records: list[CallRecord] = field(default_factory=list)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> LlmClient:
        settings = settings or get_settings()
        return cls(
            settings=settings,
            provider=build_provider(settings),
            budget=Budget(
                max_calls=settings.qagent_max_llm_calls,
                max_tokens=settings.qagent_max_llm_tokens,
                max_usd=settings.qagent_max_usd,
            ),
        )

    @property
    def available(self) -> bool:
        """False when running on the null provider.

        Callers branch on this to choose their rule-based path. Every feature in
        QAgent must produce a useful result with this set to False.
        """
        return self.provider.is_real

    def complete_json(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        schema: dict,
        max_tokens: int = 2048,
    ) -> dict[str, Any]:
        """Ask for one schema-constrained object.

        Raises BudgetExceeded before spending anything it cannot afford. Provider
        errors are swallowed and reported as an empty result, because a QA platform
        that crashes when its model provider has a bad minute is worse than one that
        falls back to rules.
        """
        self.budget.check()

        full_system = f"{system}\n\n{UNTRUSTED_SYSTEM_RULE}"
        model = self.settings.qagent_llm_model

        try:
            result: LlmResult = self.provider.complete_json(
                system=full_system, user=user, schema=schema, model=model, max_tokens=max_tokens
            )
            ok = True
        except Exception as exc:  # noqa: BLE001 - deliberate: degrade, never crash
            logger.warning("llm call failed purpose=%s error=%s", purpose, exc)
            self.records.append(
                CallRecord(purpose, self.provider.name, model, 0, 0, 0.0, 0, ok=False)
            )
            return {}

        usd = estimate_usd(result.model, result.input_tokens, result.output_tokens)
        self.budget.record(tokens=result.total_tokens, usd=usd)
        self.records.append(
            CallRecord(
                purpose=purpose,
                provider=result.provider,
                model=result.model,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                usd=usd,
                latency_ms=result.latency_ms,
                ok=ok,
            )
        )
        return result.data

    def try_complete_json(self, **kwargs: Any) -> dict[str, Any]:
        """complete_json that returns {} instead of raising when out of budget."""
        try:
            return self.complete_json(**kwargs)
        except BudgetExceeded as exc:
            logger.info("budget exhausted, falling back to rules: %s", exc)
            return {}

    def totals(self) -> dict:
        return {
            "calls": len(self.records),
            "tokens": sum(r.input_tokens + r.output_tokens for r in self.records),
            "usd": round(sum(r.usd for r in self.records), 6),
            "budget": self.budget.snapshot(),
        }
