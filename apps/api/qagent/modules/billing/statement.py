"""Usage, priced into an invoice-shaped statement (CLAUDE.md section 23).

[ADR-0008](../../../../docs/decisions/ADR-0008-phase-6-scope.md) said the
metering was done and the settlement was not, and that the seam between them was
"exactly where it should be cut". This is that seam, made concrete - and it
stops short of the same line, deliberately.

**What this does:** takes what `GET /api/v1/usage` already meters and prices it
against a declared rate card, producing line items, a subtotal and a total that
an accounts-receivable system or a payment provider can consume.

**What this does not do:** talk to Stripe, hold a card, issue a refund, handle
tax, or decide a currency. Those are not a missing afternoon of work - they are
a decision about a business model that does not exist yet, plus PCI scope. A
Stripe integration written against an imagined pricing page would be worse than
none: it would look finished, and the first real pricing decision would discard
it.

**Prices are declared, never inferred.** `PRICE_PER_MTOK` in the LLM client is
empty by default for the same reason this rate card is explicit: a hardcoded
price that drifts silently produces confidently wrong money, which is the one
category of wrong number nobody forgives.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

logger = logging.getLogger(__name__)

#: Money is Decimal everywhere in this module. A float subtotal is how an
#: invoice ends up a cent off from its own line items, and the first person to
#: notice is a customer.
CENT = Decimal("0.01")


@dataclass(frozen=True)
class RateCard:
    """What a deployment charges. No default prices - see the module docstring.

    An operator supplies these; QAgent does not have a pricing page and will
    not invent one. Zero everywhere means a statement that meters accurately
    and charges nothing, which is the correct behaviour for a deployment that
    has not decided.
    """

    currency: str = "USD"
    per_run: Decimal = Decimal("0")
    per_defect: Decimal = Decimal("0")
    #: Multiplier on LLM spend, which is already real money the operator paid.
    #: **Zero means not charged**, and it is the default on purpose: a
    #: deployment that has not configured pricing must produce a statement with
    #: no amounts on it, not one that quietly bills pass-through cost because a
    #: multiplier happened to default to 1. Use 1 for at-cost pass-through and
    #: 1.2 for a 20% margin.
    llm_markup: Decimal = Decimal("0")
    included_runs: int = 0

    def to_dict(self) -> dict:
        return {
            "currency": self.currency,
            "per_run": str(self.per_run),
            "per_defect": str(self.per_defect),
            "llm_markup": str(self.llm_markup),
            "included_runs": self.included_runs,
        }


@dataclass
class LineItem:
    description: str
    quantity: int
    unit_price: Decimal
    amount: Decimal

    def to_dict(self) -> dict:
        return {
            "description": self.description,
            "quantity": self.quantity,
            "unit_price": str(self.unit_price),
            "amount": str(self.amount),
        }


@dataclass
class Statement:
    org_id: str
    period_start: datetime
    period_end: datetime
    currency: str
    lines: list[LineItem] = field(default_factory=list)
    #: Metered but not charged, so a reader can see what a plan covered.
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return sum((line.amount for line in self.lines), Decimal("0")).quantize(
            CENT, rounding=ROUND_HALF_UP
        )

    def to_dict(self) -> dict:
        return {
            "org_id": self.org_id,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "currency": self.currency,
            "lines": [line.to_dict() for line in self.lines],
            "total": str(self.total),
            "notes": self.notes,
            "settles": False,
            "explains": (
                "Metered usage priced against the configured rate card. QAgent "
                "does not charge anyone: this is the input to a billing system, "
                "not a transaction."
            ),
        }


def _money(value) -> Decimal:
    """Coerce to Decimal without going through float.

    `Decimal(0.1)` is 0.1000000000000000055511151231257827, and an invoice built
    from those is off by cents nobody can explain. Going via `str` is what keeps
    the arithmetic exact.
    """
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))


def build_statement(usage: dict, *, rates: RateCard, period_end: datetime) -> Statement:
    """Price one period's metered usage.

    ``usage`` is the payload `GET /api/v1/usage` already returns, so there is
    exactly one definition of what happened in a period and billing reads the
    same numbers the dashboard shows.
    """
    period_start = usage.get("period_start")
    if isinstance(period_start, str):
        period_start = datetime.fromisoformat(period_start)

    statement = Statement(
        org_id=str(usage.get("org_id", "unknown")),
        period_start=period_start or period_end,
        period_end=period_end,
        currency=rates.currency,
    )

    runs = sum((usage.get("runs") or {}).values())
    billable_runs = max(0, runs - rates.included_runs)
    if rates.included_runs:
        statement.notes.append(
            f"{min(runs, rates.included_runs)} of {runs} run(s) included in the plan"
        )
    if billable_runs and rates.per_run:
        statement.lines.append(
            LineItem(
                description="QA runs",
                quantity=billable_runs,
                unit_price=rates.per_run,
                amount=(rates.per_run * billable_runs).quantize(CENT, rounding=ROUND_HALF_UP),
            )
        )

    defects = sum((usage.get("defects") or {}).values())
    if defects and rates.per_defect:
        statement.lines.append(
            LineItem(
                description="Defects reported",
                quantity=defects,
                unit_price=rates.per_defect,
                amount=(rates.per_defect * defects).quantize(CENT, rounding=ROUND_HALF_UP),
            )
        )

    spend = _money((usage.get("llm") or {}).get("spend_usd"))
    # Both conditions matter: no spend means nothing to pass through, and no
    # markup means the operator chose not to charge for it. Either way a
    # zero-amount line is noise on an invoice, not information.
    if spend and rates.llm_markup:
        amount = (spend * rates.llm_markup).quantize(CENT, rounding=ROUND_HALF_UP)
        statement.lines.append(
            LineItem(
                description="AI analysis (pass-through)",
                quantity=int((usage.get("llm") or {}).get("calls", 0)),
                unit_price=amount,
                amount=amount,
            )
        )

    if not statement.lines:
        # Two different situations produce no line items and they must not be
        # reported as one. "We charge nothing" is a configuration; "nothing
        # happened" is a quiet month. Reading the first when it is really the
        # second is how someone concludes their metering is broken.
        priced = bool(rates.per_run or rates.per_defect or rates.llm_markup)
        metered_anything = bool(runs or defects or spend)

        if not priced:
            statement.notes.append(
                "No rate card configured, so nothing is priced. Usage was still metered."
            )
        elif not metered_anything:
            statement.notes.append("No billable usage in this period.")
        else:
            # Priced, and there was usage, but none of it matched a rate - for
            # example every run fell inside the plan's included allowance.
            statement.notes.append(
                "Usage in this period was fully covered by the plan's allowances."
            )

    logger.info(
        "statement for %s: %d line(s), total %s %s",
        statement.org_id,
        len(statement.lines),
        statement.total,
        statement.currency,
    )
    return statement


def render(statement: Statement) -> str:
    """A plain-text statement, for a CLI or an email body."""
    lines = [
        f"Statement for {statement.org_id}",
        f"{statement.period_start:%Y-%m-%d} to {statement.period_end:%Y-%m-%d}",
        "",
    ]
    for item in statement.lines:
        lines.append(
            f"  {item.description:<28} {item.quantity:>6}  {statement.currency} {item.amount:>10}"
        )
    if not statement.lines:
        lines.append("  (nothing priced)")

    lines.append("")
    lines.append(f"  {'Total':<28} {'':>6}  {statement.currency} {statement.total:>10}")
    for note in statement.notes:
        lines.append(f"\n  note: {note}")
    return "\n".join(lines)
