"""Agent 2 - Test Planner (CLAUDE.md section 8).

Generation already knew *what* it could produce for an endpoint. What was
missing was anyone deciding what ought to be covered first, and then being held
to it. Without that layer, ``max_cases`` truncates in whatever order discovery
happened to return, so a run capped at 40 cases can spend all forty on a health
check and never reach the auth module at all.

The planner fixes the ordering and makes the omission legible:

    build_plan(endpoints)       -> modules ranked by priority, each with its
                                   required checks
    generate(plan=...)          -> spends the case budget highest-priority-first
    coverage(plan, report)      -> says which required checks did not make it

That last line is the reason this module earns its place. A test tool that
silently covers less than it claimed is worse than one that covers less and
says so.

Rules-first, like every other decision layer here (the triage classifier, the
generator's rules, the analyst). Priority comes from two things that are already
computed and already traceable: the risk keywords the module tree flags
(``tree.py``) and the per-endpoint ``risk_score`` discovery assigns
(``openapi.py``). ``enrich_plan`` may add a model's judgement on top, but it can
only *raise* a priority or attach a rationale - never lower one, and never
remove a required check. The rules are the floor.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from qagent.modules.analyzer.tree import build_backend_modules
from qagent.modules.discovery.openapi import EndpointSpec
from qagent.modules.generator.rules import applicable_rules, rule_intent

if TYPE_CHECKING:
    from qagent.modules.generator.rules import GenerationReport
    from qagent.modules.llm.client import LlmClient

logger = logging.getLogger(__name__)


class Priority(enum.StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


#: Highest first. Used to order modules and to compare two priorities without
#: relying on the string values sorting usefully (they do not).
PRIORITY_ORDER: list[Priority] = [
    Priority.CRITICAL,
    Priority.HIGH,
    Priority.MEDIUM,
    Priority.LOW,
]


def _rank(priority: Priority) -> int:
    return PRIORITY_ORDER.index(priority)


#: A module the tree flagged as risky *and* whose endpoints score high is the
#: definition of critical: the blast radius is credentials, money or tenant
#: data, and the surface is large enough that it will not get covered by luck.
_CRITICAL_SCORE = 0.5
_HIGH_SCORE = 0.5
_MEDIUM_SCORE = 0.3


@dataclass(frozen=True)
class PlannedCheck:
    """One check the plan requires, named by the rule that will produce it.

    ``rule`` is the generator function's name, which is what makes the plan
    verifiable rather than aspirational: ``coverage()`` matches these against
    what generation actually emitted.
    """

    endpoint_key: str
    rule: str
    intent: str

    def to_dict(self) -> dict:
        return {"endpoint_key": self.endpoint_key, "rule": self.rule, "intent": self.intent}


@dataclass
class PlannedModule:
    name: str
    priority: Priority
    rationale: str
    endpoint_keys: list[str] = field(default_factory=list)
    checks: list[PlannedCheck] = field(default_factory=list)
    max_risk_score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "priority": self.priority.value,
            "rationale": self.rationale,
            "endpoint_keys": self.endpoint_keys,
            "checks": [c.to_dict() for c in self.checks],
            "max_risk_score": round(self.max_risk_score, 3),
        }


@dataclass
class TestPlan:
    modules: list[PlannedModule] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def required_checks(self) -> int:
        return sum(len(m.checks) for m in self.modules)

    def module_for(self, endpoint_key: str) -> PlannedModule | None:
        for module in self.modules:
            if endpoint_key in module.endpoint_keys:
                return module
        return None

    def priority_of(self, endpoint_key: str) -> Priority:
        module = self.module_for(endpoint_key)
        return module.priority if module else Priority.LOW

    def order_endpoints(self, endpoints: list[EndpointSpec]) -> list[EndpointSpec]:
        """Highest-priority module first, then riskiest endpoint within it.

        This is the whole point of the planner as far as ``generate`` is
        concerned: when the case budget runs out, it runs out on the health
        check and not on the auth module.
        """
        return sorted(
            endpoints,
            key=lambda e: (_rank(self.priority_of(e.key())), -e.risk_score, e.key()),
        )

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for module in self.modules:
            counts[module.priority.value] = counts.get(module.priority.value, 0) + 1
        return {
            "modules": len(self.modules),
            "required_checks": self.required_checks,
            "by_priority": counts,
        }

    def to_dict(self) -> dict:
        return {
            "modules": [m.to_dict() for m in self.modules],
            "warnings": self.warnings,
            "summary": self.summary(),
        }


def _priority_for(*, risk_reason: str | None, max_score: float) -> tuple[Priority, str]:
    """Derive a priority and say why, in the same breath.

    A priority with no stated reason is an opinion; one that names the risk
    keyword or the score that produced it is a claim a reader can check.
    """
    if risk_reason and max_score >= _CRITICAL_SCORE:
        return Priority.CRITICAL, risk_reason
    if risk_reason:
        return Priority.HIGH, risk_reason
    if max_score >= _HIGH_SCORE:
        return (
            Priority.HIGH,
            f"No risk keyword matched, but the riskiest endpoint here scores "
            f"{max_score:.2f} (write method, identifier in the path, or both).",
        )
    if max_score >= _MEDIUM_SCORE:
        return (
            Priority.MEDIUM,
            f"Moderate risk ({max_score:.2f}): worth covering once the critical "
            f"modules are green.",
        )
    return (
        Priority.LOW,
        f"Low risk ({max_score:.2f}): read-only, unauthenticated, or both.",
    )


def build_plan(endpoints: list[EndpointSpec]) -> TestPlan:
    """Group endpoints into modules, rank them, and state the required checks.

    Grouping is delegated to ``tree.build_backend_modules`` rather than repeated
    here, so the planner's idea of a module is the same one the analyst report
    and the dashboard already show. Two different answers to "what is a module"
    would become a bug the moment anyone compared the two screens.
    """
    plan = TestPlan()

    if not endpoints:
        plan.warnings.append("no endpoints to plan against")
        return plan

    modules, risky = build_backend_modules(endpoints)
    risk_reasons = {r.name: r.reason for r in risky}
    by_key = {e.key(): e for e in endpoints}

    for node in modules:
        scores = [by_key[k].risk_score for k in node.endpoints if k in by_key]
        max_score = max(scores) if scores else 0.0
        priority, rationale = _priority_for(
            risk_reason=risk_reasons.get(node.name) or node.risk_reason,
            max_score=max_score,
        )

        checks: list[PlannedCheck] = []
        for key in node.endpoints:
            endpoint = by_key.get(key)
            if endpoint is None:
                continue
            for rule_name in applicable_rules(endpoint):
                checks.append(
                    PlannedCheck(
                        endpoint_key=key,
                        rule=rule_name,
                        intent=rule_intent(rule_name),
                    )
                )

        if not checks:
            plan.warnings.append(
                f"module '{node.name}' has endpoints but no rule applies to any of them"
            )

        plan.modules.append(
            PlannedModule(
                name=node.name,
                priority=priority,
                rationale=rationale,
                endpoint_keys=list(node.endpoints),
                checks=checks,
                max_risk_score=max_score,
            )
        )

    plan.modules.sort(key=lambda m: (_rank(m.priority), -m.max_risk_score, m.name))

    logger.info(
        "planned %d module(s), %d required check(s)", len(plan.modules), plan.required_checks
    )
    return plan


@dataclass
class CoverageReport:
    """Plan versus reality, per module.

    ``missing`` is the number that matters. Everything else on this object
    exists to explain it.
    """

    planned: int
    generated: int
    by_module: dict[str, dict] = field(default_factory=dict)
    uncovered_modules: list[str] = field(default_factory=list)

    @property
    def missing(self) -> int:
        return max(0, self.planned - self.generated)

    @property
    def ratio(self) -> float:
        return 1.0 if self.planned == 0 else round(self.generated / self.planned, 3)

    def to_dict(self) -> dict:
        return {
            "planned": self.planned,
            "generated": self.generated,
            "missing": self.missing,
            "ratio": self.ratio,
            "uncovered_modules": self.uncovered_modules,
            "by_module": self.by_module,
        }


def coverage(plan: TestPlan, report: GenerationReport) -> CoverageReport:
    """Say which required checks generation actually produced.

    Matching is on ``(endpoint_key, rule)`` because that pair is what the plan
    promised - matching on case *name* would silently pass the moment a rule's
    wording changed.
    """
    produced: set[tuple[str, str]] = {
        (case.endpoint_key or "", case.generated_by_rule or "")
        for case in report.cases
        if case.generated_by_rule
    }

    result = CoverageReport(planned=plan.required_checks, generated=0)

    for module in plan.modules:
        hit = sum(1 for c in module.checks if (c.endpoint_key, c.rule) in produced)
        result.generated += hit
        result.by_module[module.name] = {
            "priority": module.priority.value,
            "planned": len(module.checks),
            "generated": hit,
        }
        if module.checks and hit == 0:
            result.uncovered_modules.append(module.name)

    return result


# --------------------------------------------------------------------- LLM layer

#: The model may only raise a priority or supply a sharper rationale. There is
#: deliberately no field here for removing a check or lowering a priority: the
#: rules are the floor, and a prompt-injected repository must not be able to talk
#: the planner out of testing the auth module.
PLAN_ENRICHMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "modules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "raise_to": {"type": "string", "enum": ["critical", "high"]},
                    "rationale": {"type": "string", "maxLength": 400},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["modules"],
    "additionalProperties": False,
}

_ENRICH_SYSTEM = (
    "You are a senior QA strategist reviewing an automatically derived test plan for "
    "an HTTP API. The plan's priorities were assigned by rules from path keywords and "
    "a risk score. Your only job is to identify modules whose business impact those "
    "rules under-rated, and say so. You may raise a module to 'high' or 'critical' and "
    "give a one-sentence reason. You cannot lower a priority or remove a check; do not "
    "attempt to. Return only the modules you are actually changing."
)


def enrich_plan(plan: TestPlan, llm: LlmClient) -> TestPlan:
    """Optionally let a model raise priorities. Degrades to the rules on any failure.

    Mutates and returns ``plan``. A module named in the response that is not in
    the plan is ignored rather than created - plan membership comes from
    discovery, which is ground truth, not from model output.
    """
    if not plan.modules or not llm.available:
        return plan

    listing = "\n".join(
        f"- {m.name} (priority={m.priority.value}, endpoints={len(m.endpoint_keys)}, "
        f"max_risk={m.max_risk_score:.2f}): {', '.join(m.endpoint_keys[:8])}"
        for m in plan.modules
    )

    data = llm.try_complete_json(
        purpose="plan_enrichment",
        system=_ENRICH_SYSTEM,
        user=f"Derived plan:\n{listing}",
        schema=PLAN_ENRICHMENT_SCHEMA,
        max_tokens=1024,
    )

    by_name = {m.name: m for m in plan.modules}
    for entry in data.get("modules", []) or []:
        if not isinstance(entry, dict):
            continue
        module = by_name.get(str(entry.get("name", "")))
        if module is None:
            continue
        try:
            proposed = Priority(str(entry.get("raise_to", "")))
        except ValueError:
            continue
        # Raise only. The rules are the floor.
        if _rank(proposed) < _rank(module.priority):
            module.priority = proposed
            reason = str(entry.get("rationale", "")).strip()
            module.rationale = (
                f"{module.rationale} Raised to {proposed.value} by review: {reason}"
                if reason
                else f"{module.rationale} Raised to {proposed.value} by review."
            )

    plan.modules.sort(key=lambda m: (_rank(m.priority), -m.max_risk_score, m.name))
    return plan
