"""Code Intelligence Engine: a module tree from what discovery already found
(CLAUDE.md section 7).

This deliberately does not re-derive anything discovery or the route parser
already computed. An `ApiEndpoint`/`EndpointSpec` list is the ground truth for
what the backend exposes; this module's only job is to group that flat list
into the "Auth API / Product API / Order API" shape CLAUDE.md's example shows,
so a human -- or the planner, later -- sees modules instead of forty
unconnected paths.

Grouping is by path segment, not by guessed business meaning: the first
non-parameter, non-version segment of a path (`/api/v1/orders/{id}/items` ->
`orders`) is treated as the module name. That is legible and wrong in the same
way a human's first guess would be wrong -- which is the honest place for a
rules-first tool to sit, versus a confident-sounding label with no path to
verify it against.

Frontend routes are read the same way discovery reads Python route decorators:
literal strings only, this time from a Next.js-shaped `app/` or `pages/`
directory tree, because that convention *is* the routing table for those two
frameworks -- no server has to be asked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from qagent.modules.discovery.routes import SKIP_DIRS

#: Path segments that don't name a module -- they're plumbing every API has.
_NON_MODULE_SEGMENTS = {"api", "v1", "v2", "v3", "internal"}

_PARAM_SEGMENT_RE = re.compile(r"^[{:].*|^\[.*\]$")

#: A module name matching one of these is where CLAUDE.md's own examples (the
#: Auth API, the Payment API) point a reviewer's attention first: get this
#: wrong and the blast radius is credentials, money, or every tenant's data.
_RISKY_MODULE_KEYWORDS = {
    "auth": "Authentication/authorization endpoints: broken access control here "
    "affects every other module.",
    "login": "Credential handling: a defect risks account takeover.",
    "session": "Session lifecycle: a defect risks session fixation or hijack.",
    "user": "Account data: a defect risks cross-tenant or cross-user data exposure.",
    "admin": "Elevated-privilege surface: a defect here bypasses normal authorization.",
    "payment": "Money movement: a defect risks financial loss or double charges.",
    "checkout": "Money movement: a defect risks financial loss or double charges.",
    "order": "Money movement: a defect risks financial loss or double charges.",
    "billing": "Money movement: a defect risks financial loss or double charges.",
    "webhook": "Externally triggered: a defect is reachable without a user session.",
    "upload": "Accepts arbitrary content: a defect risks stored injection or resource exhaustion.",
    "token": "Credential issuance: a defect risks forged or over-privileged tokens.",
}


@dataclass
class ModuleNode:
    name: str
    endpoints: list[str] = field(default_factory=list)  # "GET /orders/{id}"
    risk_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "endpoints": self.endpoints,
            "risk_reason": self.risk_reason,
        }


@dataclass
class RiskyComponent:
    name: str
    reason: str
    max_risk_score: float

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "reason": self.reason,
            "max_risk_score": round(self.max_risk_score, 3),
        }


@dataclass
class ProjectTree:
    frontend: list[str] = field(default_factory=list)
    backend: list[ModuleNode] = field(default_factory=list)
    database: list[str] = field(default_factory=list)
    risky_components: list[RiskyComponent] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "frontend": self.frontend,
            "backend": [m.to_dict() for m in self.backend],
            "database": self.database,
            "risky_components": [r.to_dict() for r in self.risky_components],
        }


def _module_name(path: str) -> str:
    segments = [s for s in path.strip("/").split("/") if s]
    for segment in segments:
        if _PARAM_SEGMENT_RE.match(segment):
            continue
        lowered = segment.lower()
        if lowered in _NON_MODULE_SEGMENTS:
            continue
        return lowered
    return "root"


def build_backend_modules(endpoints: list) -> tuple[list[ModuleNode], list[RiskyComponent]]:
    """Group endpoints into modules and flag the ones CLAUDE.md's examples name.

    Accepts anything with ``.method``, ``.path`` and ``.risk_score`` -- the same
    duck-typed shape ``EndpointSpec`` and the persisted ``ApiEndpoint`` row both
    already have -- so this works from either a fresh discovery run or rows
    already in the database, without importing either module.
    """
    grouped: dict[str, list] = {}
    for ep in endpoints:
        grouped.setdefault(_module_name(ep.path), []).append(ep)

    modules: list[ModuleNode] = []
    risky: list[RiskyComponent] = []
    for name in sorted(grouped):
        members = grouped[name]
        members.sort(key=lambda e: (-e.risk_score, e.path, e.method))
        node = ModuleNode(
            name=name,
            endpoints=[f"{e.method.upper()} {e.path}" for e in members],
        )
        top_score = members[0].risk_score if members else 0.0

        reason = None
        for keyword, text in _RISKY_MODULE_KEYWORDS.items():
            if keyword in name:
                reason = text
                break
        # A module can also earn scrutiny purely on score -- an unauthenticated
        # write to a parameterised resource -- even with an unremarkable name.
        if reason is None and top_score >= 0.6:
            reason = (
                "High-risk endpoint(s) by method/path shape "
                "(write access, an identifier parameter, or a sensitive path fragment)."
            )

        node.risk_reason = reason
        modules.append(node)
        if reason is not None:
            risky.append(RiskyComponent(name=name, reason=reason, max_risk_score=top_score))

    risky.sort(key=lambda r: -r.max_risk_score)
    return modules, risky


#: Next.js App Router: a route segment is a directory; the route itself is the
#: directory's path, named by the presence of one of these files inside it.
_APP_ROUTER_LEAVES = {"page.tsx", "page.jsx", "page.ts", "page.js"}

#: Pages Router: the file *is* the route, relative to pages/.
_PAGE_EXTENSIONS = {".tsx", ".jsx", ".ts", ".js"}
_PAGES_ROUTER_IGNORE = {"_app", "_document", "_error", "404", "500"}


def _app_router_routes(app_dir: Path) -> list[str]:
    routes: set[str] = set()
    for leaf_name in _APP_ROUTER_LEAVES:
        for leaf in app_dir.rglob(leaf_name):
            if any(part in SKIP_DIRS for part in leaf.parts):
                continue
            rel = leaf.parent.relative_to(app_dir)
            segments = [s for s in rel.parts if not (s.startswith("(") and s.endswith(")"))]
            route = "/" + "/".join(segments) if segments else "/"
            routes.add(re.sub(r"\[([^/\]]+)\]", r"{\1}", route))
    return sorted(routes)


def _pages_router_routes(pages_dir: Path) -> list[str]:
    routes: set[str] = set()
    for path in pages_dir.rglob("*"):
        if not path.is_file() or path.suffix not in _PAGE_EXTENSIONS:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.stem in _PAGES_ROUTER_IGNORE or path.name.startswith("_"):
            continue
        if path.parent.name == "api" or "api" in path.relative_to(pages_dir).parts[:-1]:
            continue  # API routes, not pages -- already covered by backend discovery
        rel = path.relative_to(pages_dir).with_suffix("")
        segments = [s for s in rel.parts if s != "index"]
        route = "/" + "/".join(segments) if segments else "/"
        routes.add(re.sub(r"\[([^/\]]+)\]", r"{\1}", route))
    return sorted(routes)


def build_frontend_routes(repo_dir: Path) -> list[str]:
    """Next.js App Router and Pages Router conventions, filesystem-derived.

    A directory-convention router *is* its own routing table, unlike Express or
    Flask where a route only exists once a decorator runs. That makes this
    exhaustive for the two conventions it knows, not best-effort the way the
    Python/JS decorator parser in ``discovery/routes.py`` has to be.
    """
    routes: set[str] = set()
    for app_dir in repo_dir.rglob("app"):
        if any(part in SKIP_DIRS for part in app_dir.parts) or not app_dir.is_dir():
            continue
        routes.update(_app_router_routes(app_dir))
    for pages_dir in repo_dir.rglob("pages"):
        if any(part in SKIP_DIRS for part in pages_dir.parts) or not pages_dir.is_dir():
            continue
        routes.update(_pages_router_routes(pages_dir))
    return sorted(routes)


#: SQLAlchemy: ``__tablename__ = "orders"``. Prisma: ``model Order {``.
_SQLALCHEMY_TABLE_RE = re.compile(r'__tablename__\s*=\s*["\']([A-Za-z0-9_]+)["\']')
_PRISMA_MODEL_RE = re.compile(r'^\s*model\s+([A-Za-z0-9_]+)\s*\{', re.MULTILINE)
_DJANGO_MODEL_RE = re.compile(
    r'^\s*class\s+([A-Za-z0-9_]+)\s*\(\s*models\.Model\s*\)', re.MULTILINE
)


def build_database_models(repo_dir: Path) -> list[str]:
    """Table/model names from ORM source -- never a live database connection.

    Consistent with the rest of the analyzer: this reads text, it never opens a
    connection, imports a settings module, or runs a migration to find out what
    the schema looks like.
    """
    names: set[str] = set()
    for path in repo_dir.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts) or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        names.update(_SQLALCHEMY_TABLE_RE.findall(text))
        names.update(_DJANGO_MODEL_RE.findall(text))

    for path in repo_dir.rglob("*.prisma"):
        if any(part in SKIP_DIRS for part in path.parts) or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        names.update(_PRISMA_MODEL_RE.findall(text))

    return sorted(names)


def build_project_tree(repo_dir: Path, endpoints: list) -> ProjectTree:
    backend, risky = build_backend_modules(endpoints)
    return ProjectTree(
        frontend=build_frontend_routes(repo_dir),
        backend=backend,
        database=build_database_models(repo_dir),
        risky_components=risky,
    )
