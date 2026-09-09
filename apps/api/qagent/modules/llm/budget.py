"""Per-run spend limits.

An exploration or generation loop that is not explicitly bounded will happily spend
more on one repository than the platform earns in a month. The budget is checked
before every call and decremented after, so a run degrades (fewer tests generated,
triage falls back to rules) instead of running away.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class BudgetExceeded(RuntimeError):
    """Raised when a run has consumed its allowance.

    Callers are expected to catch this and degrade gracefully rather than fail the
    whole run: a partial result with rule-based fallbacks is worth more than nothing.
    """


@dataclass
class Budget:
    max_calls: int
    max_tokens: int
    max_usd: float

    calls: int = field(default=0)
    tokens: int = field(default=0)
    usd: float = field(default=0.0)

    def check(self) -> None:
        if self.calls >= self.max_calls:
            raise BudgetExceeded(f"call budget exhausted ({self.calls}/{self.max_calls})")
        if self.tokens >= self.max_tokens:
            raise BudgetExceeded(f"token budget exhausted ({self.tokens}/{self.max_tokens})")
        if self.usd >= self.max_usd:
            raise BudgetExceeded(f"cost budget exhausted (${self.usd:.4f}/${self.max_usd:.2f})")

    def record(self, *, tokens: int, usd: float) -> None:
        self.calls += 1
        self.tokens += tokens
        self.usd += usd

    @property
    def remaining_calls(self) -> int:
        return max(0, self.max_calls - self.calls)

    def snapshot(self) -> dict:
        return {
            "calls": self.calls,
            "tokens": self.tokens,
            "usd": round(self.usd, 6),
            "max_calls": self.max_calls,
            "max_tokens": self.max_tokens,
            "max_usd": self.max_usd,
        }
