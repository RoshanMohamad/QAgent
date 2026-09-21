"""Pricing metered usage into a statement.

Money is the one category of wrong number nobody forgives, so most of these
tests are about arithmetic exactness and about the module refusing to invent
prices it was not given.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from qagent.modules.billing.statement import RateCard, build_statement, render

PERIOD_END = datetime(2026, 2, 1, tzinfo=UTC)


def _usage(**overrides) -> dict:
    payload = {
        "org_id": "org-1",
        "period_start": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
        "runs": {"passed": 80, "failed": 20},
        "llm": {"calls": 40, "tokens": 120_000, "spend_usd": 3.50},
        "defects": {"high": 4, "low": 6},
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------- pricing


def test_runs_are_priced_against_the_rate_card() -> None:
    statement = build_statement(
        _usage(), rates=RateCard(per_run=Decimal("0.10")), period_end=PERIOD_END
    )

    runs = next(line for line in statement.lines if line.description == "QA runs")
    assert runs.quantity == 100
    assert runs.amount == Decimal("10.00")


def test_included_runs_are_deducted_and_explained() -> None:
    statement = build_statement(
        _usage(),
        rates=RateCard(per_run=Decimal("0.10"), included_runs=60),
        period_end=PERIOD_END,
    )

    runs = next(line for line in statement.lines if line.description == "QA runs")
    assert runs.quantity == 40
    assert any("included in the plan" in note for note in statement.notes)


def test_a_plan_covering_everything_charges_for_no_runs() -> None:
    statement = build_statement(
        _usage(),
        rates=RateCard(per_run=Decimal("0.10"), included_runs=500),
        period_end=PERIOD_END,
    )

    assert not any(line.description == "QA runs" for line in statement.lines)


def test_defects_are_priced_when_the_card_says_so() -> None:
    statement = build_statement(
        _usage(), rates=RateCard(per_defect=Decimal("1.25")), period_end=PERIOD_END
    )

    defects = next(line for line in statement.lines if "Defects" in line.description)
    assert defects.quantity == 10
    assert defects.amount == Decimal("12.50")


def test_llm_spend_carries_the_configured_markup() -> None:
    statement = build_statement(
        _usage(), rates=RateCard(llm_markup=Decimal("1.2")), period_end=PERIOD_END
    )

    line = next(line for line in statement.lines if "AI analysis" in line.description)
    assert line.amount == Decimal("4.20")


# ------------------------------------------------------- no invented prices


def test_an_empty_rate_card_prices_nothing_but_still_meters() -> None:
    """A deployment that has not decided on pricing is a legitimate state, and
    must produce a statement saying so rather than an empty document."""
    statement = build_statement(_usage(), rates=RateCard(), period_end=PERIOD_END)

    assert statement.lines == []
    assert statement.total == Decimal("0.00")
    assert any("No rate card configured" in note for note in statement.notes)


def test_the_statement_says_it_does_not_settle() -> None:
    """QAgent meters and prices; it does not charge anyone."""
    payload = build_statement(_usage(), rates=RateCard(), period_end=PERIOD_END).to_dict()

    assert payload["settles"] is False
    assert "not a transaction" in payload["explains"]


# ------------------------------------------------------------------ arithmetic


def test_the_total_equals_the_sum_of_its_lines() -> None:
    """An invoice a cent off from its own line items is noticed by a customer."""
    statement = build_statement(
        _usage(),
        rates=RateCard(
            per_run=Decimal("0.07"), per_defect=Decimal("0.33"), llm_markup=Decimal("1.15")
        ),
        period_end=PERIOD_END,
    )

    assert statement.total == sum(line.amount for line in statement.lines)


def test_money_never_goes_through_float() -> None:
    """Decimal(0.1) is 0.1000000000000000055..., and invoices built from those
    drift by cents nobody can explain."""
    statement = build_statement(
        _usage(llm={"calls": 1, "tokens": 1, "spend_usd": 0.1}),
        rates=RateCard(llm_markup=Decimal("3")),
        period_end=PERIOD_END,
    )

    line = next(line for line in statement.lines if "AI analysis" in line.description)
    assert line.amount == Decimal("0.30")


def test_amounts_are_quantized_to_cents() -> None:
    statement = build_statement(
        _usage(runs={"passed": 3}), rates=RateCard(per_run=Decimal("0.333")), period_end=PERIOD_END
    )

    assert statement.lines[0].amount == Decimal("1.00")
    assert str(statement.total) == "1.00"


def test_rounding_is_half_up_not_bankers() -> None:
    statement = build_statement(
        _usage(runs={"passed": 1}, llm={}, defects={}),
        rates=RateCard(per_run=Decimal("0.005")),
        period_end=PERIOD_END,
    )

    # Python's default would round 0.005 to 0.00; an invoice should not
    # quietly round in the operator's disfavour.
    assert statement.lines[0].amount == Decimal("0.01")


# ---------------------------------------------------------------- robustness


def test_usage_with_no_activity_produces_a_zero_statement() -> None:
    statement = build_statement(
        {"org_id": "org-1", "period_start": PERIOD_END.isoformat()},
        rates=RateCard(per_run=Decimal("1")),
        period_end=PERIOD_END,
    )

    assert statement.total == Decimal("0.00")


def test_the_period_is_carried_through() -> None:
    payload = build_statement(_usage(), rates=RateCard(), period_end=PERIOD_END).to_dict()

    assert payload["period_start"].startswith("2026-01-01")
    assert payload["period_end"].startswith("2026-02-01")


def test_render_shows_the_lines_and_the_total() -> None:
    text = render(
        build_statement(
            _usage(), rates=RateCard(per_run=Decimal("0.10")), period_end=PERIOD_END
        )
    )

    assert "QA runs" in text
    assert "Total" in text
    assert "USD" in text


def test_render_of_an_unpriced_statement_says_so() -> None:
    text = render(build_statement(_usage(), rates=RateCard(), period_end=PERIOD_END))

    assert "nothing priced" in text


def test_no_usage_is_reported_differently_from_no_rate_card() -> None:
    """Reading "we charge nothing" when it is really "nothing happened" is how
    someone concludes their metering is broken."""
    quiet_month = build_statement(
        {"org_id": "org-1", "period_start": PERIOD_END.isoformat()},
        rates=RateCard(per_run=Decimal("0.10")),
        period_end=PERIOD_END,
    )

    assert any("No billable usage" in note for note in quiet_month.notes)
    assert not any("No rate card" in note for note in quiet_month.notes)


def test_usage_fully_inside_the_allowance_says_so() -> None:
    covered = build_statement(
        _usage(llm={}, defects={}),
        rates=RateCard(per_run=Decimal("0.10"), included_runs=500),
        period_end=PERIOD_END,
    )

    assert covered.lines == []
    assert any("covered by the plan" in note for note in covered.notes)
