"""Agent 1 - Project Analyst (CLAUDE.md section 8).

Not a model call. Everything this agent reports -- technologies, modules,
API surface, risky components -- is deterministic and traceable to a file and
line, for the same reason the triage classifier and the generator's rules are
rules-first: it is free, instant, reproducible across runs, and testable
without a live provider. A model has nothing to add to "does this repo have a
package.json that lists react" that a JSON parse doesn't already answer better.

This module owns no I/O of its own. It composes three things that already
exist for their own reasons -- stack detection, endpoint discovery (OpenAPI or
the static route parser), and the module-tree builder -- into the single
`ProjectAnalysis` CLAUDE.md section 8 describes as Agent 1's output, which is
also exactly the shape `Project.stack` (models.py) was reserved for and never
filled.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from qagent.modules.analyzer.stack import StackReport, detect_stack
from qagent.modules.analyzer.tree import ProjectTree, build_project_tree
from qagent.modules.discovery.openapi import EndpointSpec
from qagent.modules.discovery.routes import parse_routes

logger = logging.getLogger(__name__)


@dataclass
class ProjectAnalysis:
    stack: StackReport
    tree: ProjectTree
    endpoint_source: str  # "openapi" | "route_parser" | "none"
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        """The short, human-facing version CLAUDE.md section 24's example prints."""
        return {
            "technologies": self.stack.names(),
            "endpoint_count": sum(len(m.endpoints) for m in self.tree.backend),
            "frontend_route_count": len(self.tree.frontend),
            "database_model_count": len(self.tree.database),
            "risky_component_count": len(self.tree.risky_components),
        }

    def to_dict(self) -> dict:
        """The shape persisted into ``Project.stack`` and returned by the CLI/API."""
        return {
            "stack": self.stack.to_dict(),
            "tree": self.tree.to_dict(),
            "endpoint_source": self.endpoint_source,
            "warnings": self.warnings,
            "summary": self.summary(),
        }


def analyze_repository(
    repo_dir: Path,
    *,
    endpoints: list[EndpointSpec] | None = None,
) -> ProjectAnalysis:
    """Run the full analyst pass over a checkout already on local disk.

    ``endpoints`` lets a caller that already ran OpenAPI discovery (the normal
    pipeline path) pass those in directly, so this never re-fetches a live
    document itself -- discovery from a *running* app and analysis of a repo
    *checkout* are different inputs (ADR-0001) and this only ever touches the
    second. When no endpoints are supplied, the static route parser runs
    against the checkout, same as ``scan-repo``'s own fallback.
    """
    warnings: list[str] = []

    if not repo_dir.is_dir():
        raise NotADirectoryError(f"not a directory: {repo_dir}")

    stack = detect_stack(repo_dir)

    if endpoints is not None:
        source = "openapi"
    else:
        endpoints = parse_routes(repo_dir)
        source = "route_parser" if endpoints else "none"
        if source == "none":
            warnings.append(
                "no OpenAPI document supplied and static route parsing found nothing; "
                "the backend module tree and risky-component list will be empty"
            )

    tree = build_project_tree(repo_dir, endpoints)

    if not stack.technologies:
        warnings.append("no recognised manifest or config file found under this path")

    logger.info(
        "project analysis: %d technologies, %d backend modules, %d frontend routes, "
        "%d risky component(s)",
        len(stack.technologies),
        len(tree.backend),
        len(tree.frontend),
        len(tree.risky_components),
    )

    return ProjectAnalysis(stack=stack, tree=tree, endpoint_source=source, warnings=warnings)
