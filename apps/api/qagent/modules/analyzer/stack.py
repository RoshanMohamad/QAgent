"""Technology detection for a repository checkout (CLAUDE.md section 6).

Answers "what is this thing built out of" from declared manifests and config
files, so that later stages -- the planner, the generator, the operator reading
a dashboard -- work from a description of the application instead of a guess.

Three properties matter more than breadth of coverage:

**Nothing is executed.** No ``npm install``, no importing the repository, no
resolving a lockfile against a registry. Detection reads text files and parses
them with stdlib parsers. Under ADR-0004 a checkout is attacker-controlled
content, and section 22 says third-party code never runs outside a sandbox --
an analyzer that ran the project's own tooling to find out what it was would
break both rules before the first test was ever generated.

**Every detection carries its evidence.** A `Technology` names the file and the
marker that proved it. "Detected Django" is an assertion the operator has to
take on faith; "Detected Django -- ``requirements.txt`` declares ``django``"
is one they can check in two seconds, and correct when it's wrong.

**Declared beats inferred.** A dependency the project itself lists is strong
evidence; a config file's presence is good evidence; a container image name in
a compose file is weaker still, because that service might belong to something
else in the stack. Those rank as separate confidences rather than collapsing
into a single boolean, and corroboration across sources raises them.

The detection tables below are deliberately explicit rather than clever. A
lookup of exact package names is auditable and cheap to extend; a heuristic
over substrings would match ``react-hook-form`` as React and be wrong in a way
nobody notices until a test plan is built on it.
"""

from __future__ import annotations

import json
import logging
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from qagent.modules.discovery.routes import SKIP_DIRS

logger = logging.getLogger(__name__)

#: Detection strength by source. A project that lists a dependency is telling
#: you what it uses; a compose image only tells you something nearby uses it.
CONFIDENCE_DECLARED = 0.95
CONFIDENCE_CONFIG = 0.9
CONFIDENCE_IMAGE = 0.8
CONFIDENCE_CORROBORATED = 0.99

LANGUAGE = "language"
FRONTEND = "frontend"
BACKEND = "backend"
DATABASE = "database"
INFRASTRUCTURE = "infrastructure"
AUTH = "auth"
TESTING = "testing"
API = "api"


@dataclass(frozen=True)
class Evidence:
    """Where a detection came from, in terms the reader can go and verify."""

    path: str  # repo-relative, always posix-style
    marker: str  # the literal thing found: a package name, a filename, an image

    def to_dict(self) -> dict:
        return {"path": self.path, "marker": self.marker}


@dataclass
class Technology:
    name: str
    category: str
    version: str | None = None
    confidence: float = CONFIDENCE_DECLARED
    evidence: list[Evidence] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "category": self.category,
            "version": self.version,
            "confidence": round(self.confidence, 2),
            "evidence": [e.to_dict() for e in self.evidence],
        }


# --------------------------------------------------------------------------- tables

#: npm package -> (display name, category). Exact names only, never substrings.
_NODE_PACKAGES: dict[str, tuple[str, str]] = {
    "react": ("React", FRONTEND),
    "next": ("Next.js", FRONTEND),
    "vue": ("Vue", FRONTEND),
    "@angular/core": ("Angular", FRONTEND),
    "svelte": ("Svelte", FRONTEND),
    "tailwindcss": ("Tailwind CSS", FRONTEND),
    "typescript": ("TypeScript", LANGUAGE),
    "express": ("Express", BACKEND),
    "fastify": ("Fastify", BACKEND),
    "@nestjs/core": ("NestJS", BACKEND),
    "koa": ("Koa", BACKEND),
    "pg": ("PostgreSQL", DATABASE),
    "postgres": ("PostgreSQL", DATABASE),
    "mysql": ("MySQL", DATABASE),
    "mysql2": ("MySQL", DATABASE),
    "mongodb": ("MongoDB", DATABASE),
    "mongoose": ("MongoDB", DATABASE),
    "redis": ("Redis", DATABASE),
    "ioredis": ("Redis", DATABASE),
    "@prisma/client": ("Prisma", DATABASE),
    "prisma": ("Prisma", DATABASE),
    "typeorm": ("TypeORM", DATABASE),
    "drizzle-orm": ("Drizzle", DATABASE),
    "jsonwebtoken": ("JWT", AUTH),
    "next-auth": ("NextAuth", AUTH),
    "passport": ("Passport", AUTH),
    "@auth/core": ("Auth.js", AUTH),
    "bcrypt": ("bcrypt", AUTH),
    "bcryptjs": ("bcrypt", AUTH),
    "jest": ("Jest", TESTING),
    "vitest": ("Vitest", TESTING),
    "mocha": ("Mocha", TESTING),
    "cypress": ("Cypress", TESTING),
    "playwright": ("Playwright", TESTING),
    "@playwright/test": ("Playwright", TESTING),
    "graphql": ("GraphQL", API),
    "@apollo/server": ("GraphQL", API),
    "apollo-server": ("GraphQL", API),
    "swagger-ui-express": ("OpenAPI", API),
    "@nestjs/swagger": ("OpenAPI", API),
}

#: PyPI distribution name -> (display name, category). Keys are normalised to
#: lowercase with underscores folded to hyphens before lookup.
_PYTHON_PACKAGES: dict[str, tuple[str, str]] = {
    "fastapi": ("FastAPI", BACKEND),
    "django": ("Django", BACKEND),
    "flask": ("Flask", BACKEND),
    "tornado": ("Tornado", BACKEND),
    "litestar": ("Litestar", BACKEND),
    "uvicorn": ("Uvicorn", INFRASTRUCTURE),
    "gunicorn": ("Gunicorn", INFRASTRUCTURE),
    "celery": ("Celery", INFRASTRUCTURE),
    "psycopg": ("PostgreSQL", DATABASE),
    "psycopg2": ("PostgreSQL", DATABASE),
    "psycopg2-binary": ("PostgreSQL", DATABASE),
    "asyncpg": ("PostgreSQL", DATABASE),
    "pymysql": ("MySQL", DATABASE),
    "mysqlclient": ("MySQL", DATABASE),
    "pymongo": ("MongoDB", DATABASE),
    "motor": ("MongoDB", DATABASE),
    "redis": ("Redis", DATABASE),
    "sqlalchemy": ("SQLAlchemy", DATABASE),
    "alembic": ("Alembic", DATABASE),
    "pyjwt": ("JWT", AUTH),
    "python-jose": ("JWT", AUTH),
    "authlib": ("OAuth", AUTH),
    "bcrypt": ("bcrypt", AUTH),
    "passlib": ("passlib", AUTH),
    "pytest": ("pytest", TESTING),
    "playwright": ("Playwright", TESTING),
    "locust": ("Locust", TESTING),
    "strawberry-graphql": ("GraphQL", API),
    "graphene": ("GraphQL", API),
    "ariadne": ("GraphQL", API),
}

#: Container image prefix -> (display name, category). Matched against the part
#: of an image reference before the tag, after any registry/namespace prefix.
_IMAGE_TECHNOLOGIES: dict[str, tuple[str, str]] = {
    "postgres": ("PostgreSQL", DATABASE),
    "postgis": ("PostgreSQL", DATABASE),
    "pgvector": ("pgvector", DATABASE),
    "mysql": ("MySQL", DATABASE),
    "mariadb": ("MySQL", DATABASE),
    "mongo": ("MongoDB", DATABASE),
    "redis": ("Redis", DATABASE),
    "rabbitmq": ("RabbitMQ", INFRASTRUCTURE),
    "elasticsearch": ("Elasticsearch", INFRASTRUCTURE),
    "minio": ("MinIO", INFRASTRUCTURE),
    "nginx": ("nginx", INFRASTRUCTURE),
}

#: Filename (or glob) -> (display name, category). Presence alone is the signal.
_CONFIG_MARKERS: list[tuple[str, str, str]] = [
    ("Dockerfile", "Docker", INFRASTRUCTURE),
    ("dockerfile", "Docker", INFRASTRUCTURE),
    ("docker-compose.yml", "Docker Compose", INFRASTRUCTURE),
    ("docker-compose.yaml", "Docker Compose", INFRASTRUCTURE),
    ("compose.yml", "Docker Compose", INFRASTRUCTURE),
    ("compose.yaml", "Docker Compose", INFRASTRUCTURE),
    ("next.config.js", "Next.js", FRONTEND),
    ("next.config.mjs", "Next.js", FRONTEND),
    ("next.config.ts", "Next.js", FRONTEND),
    ("nuxt.config.ts", "Nuxt", FRONTEND),
    ("angular.json", "Angular", FRONTEND),
    ("vite.config.ts", "Vite", FRONTEND),
    ("tsconfig.json", "TypeScript", LANGUAGE),
    ("tailwind.config.js", "Tailwind CSS", FRONTEND),
    ("tailwind.config.ts", "Tailwind CSS", FRONTEND),
    ("manage.py", "Django", BACKEND),
    ("alembic.ini", "Alembic", DATABASE),
    ("playwright.config.ts", "Playwright", TESTING),
    ("playwright.config.js", "Playwright", TESTING),
    ("cypress.config.ts", "Cypress", TESTING),
    ("jest.config.js", "Jest", TESTING),
    ("jest.config.ts", "Jest", TESTING),
    ("vitest.config.ts", "Vitest", TESTING),
    ("pytest.ini", "pytest", TESTING),
    ("schema.prisma", "Prisma", DATABASE),
    ("go.mod", "Go", LANGUAGE),
    ("Gemfile", "Ruby", LANGUAGE),
]

#: Filenames whose presence means "this is an OpenAPI-documented API", which is
#: what decides whether discovery can skip static route parsing entirely.
_OPENAPI_FILENAMES = {
    "openapi.json",
    "openapi.yaml",
    "openapi.yml",
    "swagger.json",
    "swagger.yaml",
    "swagger.yml",
}

#: An environment variable whose name matches is a credential: the analyzer
#: records that it must be provisioned, and never records what it is set to.
_SECRET_NAME_RE = re.compile(
    r"(SECRET|PASSWORD|PASSWD|TOKEN|API_KEY|APIKEY|PRIVATE_KEY|CREDENTIAL|DSN|DATABASE_URL)",
    re.IGNORECASE,
)

_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")

_VERSION_RE = re.compile(r"(\d+(?:\.\d+)*(?:[.-][A-Za-z0-9]+)*)")


# --------------------------------------------------------------------------- helpers


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover - defensive, paths always come from root
        return path.as_posix()


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug("unreadable file %s: %s", path, exc)
        return None


def _normalize_python_name(raw: str) -> str:
    return raw.strip().lower().replace("_", "-")


def _clean_version(raw: str | None) -> str | None:
    """A version constraint reduced to the number a human would quote.

    ``^15.0.1`` and ``>=0.115`` both become their bare number. A constraint is
    not an installed version -- that's only knowable from a lockfile or a live
    environment, neither of which this reads -- so what's shown is the declared
    constraint's number and nothing more.
    """
    if not raw:
        return None
    match = _VERSION_RE.search(raw)
    return match.group(1) if match else None


def _iter_files(root: Path, names: set[str]) -> list[Path]:
    found = []
    for path in root.rglob("*"):
        if path.name not in names or not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        found.append(path)
    return found


class _Accumulator:
    """Collects detections, merging duplicates instead of listing them twice.

    The same technology is routinely provable from several directions -- Next.js
    from ``package.json`` and from ``next.config.js``, PostgreSQL from a driver
    and from a compose image. That is corroboration, not two findings, so the
    evidence is unioned and confidence rises to `CONFIDENCE_CORROBORATED`.
    """

    def __init__(self) -> None:
        self._by_name: dict[str, Technology] = {}

    def add(
        self,
        name: str,
        category: str,
        *,
        evidence: Evidence,
        version: str | None = None,
        confidence: float = CONFIDENCE_DECLARED,
    ) -> None:
        existing = self._by_name.get(name)
        if existing is None:
            self._by_name[name] = Technology(
                name=name,
                category=category,
                version=version,
                confidence=confidence,
                evidence=[evidence],
            )
            return

        if evidence not in existing.evidence:
            existing.evidence.append(evidence)
            existing.confidence = CONFIDENCE_CORROBORATED
        existing.confidence = max(existing.confidence, confidence)
        # A version from a manifest beats no version from a config-file marker.
        existing.version = existing.version or version

    def technologies(self) -> list[Technology]:
        return sorted(self._by_name.values(), key=lambda t: (t.category, t.name))


# --------------------------------------------------------------------------- parsers


def _detect_package_json(path: Path, root: Path, acc: _Accumulator) -> None:
    text = _read_text(path)
    if text is None:
        return
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.debug("skipping unparsable package.json %s: %s", path, exc)
        return
    if not isinstance(manifest, dict):
        return

    rel = _relative(path, root)
    acc.add("Node.js", LANGUAGE, evidence=Evidence(rel, "package.json"))

    for section in ("dependencies", "devDependencies"):
        deps = manifest.get(section)
        if not isinstance(deps, dict):
            continue
        for raw_name, raw_version in deps.items():
            entry = _NODE_PACKAGES.get(raw_name)
            if entry is None:
                continue
            name, category = entry
            acc.add(
                name,
                category,
                evidence=Evidence(rel, raw_name),
                version=_clean_version(raw_version if isinstance(raw_version, str) else None),
            )


def _detect_pyproject(path: Path, root: Path, acc: _Accumulator) -> None:
    text = _read_text(path)
    if text is None:
        return
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        logger.debug("skipping unparsable pyproject %s: %s", path, exc)
        return

    rel = _relative(path, root)
    acc.add("Python", LANGUAGE, evidence=Evidence(rel, "pyproject.toml"))

    project = data.get("project", {})
    requirements: list[str] = []
    if isinstance(project, dict):
        declared = project.get("dependencies")
        if isinstance(declared, list):
            requirements.extend(str(item) for item in declared)
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for group in optional.values():
                if isinstance(group, list):
                    requirements.extend(str(item) for item in group)

    for requirement in requirements:
        _detect_requirement(requirement, rel, acc)


def _detect_requirement(requirement: str, rel: str, acc: _Accumulator) -> None:
    """One PEP 508 requirement line, reduced to a name and a declared version.

    Parsed by hand rather than with ``packaging``: the only fields that matter
    are the distribution name and the first version number, and a full
    requirement grammar is not worth a dependency to reach them.
    """
    line = requirement.split("#", 1)[0].strip()
    if not line or line.startswith("-"):
        return
    # Drop environment markers and extras: "uvicorn[standard]>=0.32; python_version>='3.11'"
    line = line.split(";", 1)[0].strip()
    name_part = re.split(r"[<>=!~\[\s]", line, maxsplit=1)[0]
    entry = _PYTHON_PACKAGES.get(_normalize_python_name(name_part))
    if entry is None:
        return
    name, category = entry
    acc.add(
        name,
        category,
        evidence=Evidence(rel, name_part.strip()),
        version=_clean_version(line[len(name_part) :]),
    )


def _detect_requirements_txt(path: Path, root: Path, acc: _Accumulator) -> None:
    text = _read_text(path)
    if text is None:
        return
    rel = _relative(path, root)
    acc.add("Python", LANGUAGE, evidence=Evidence(rel, path.name))
    for line in text.splitlines():
        _detect_requirement(line, rel, acc)


def _image_technology(image: str) -> tuple[str, str] | None:
    """``docker.io/library/postgres:16-alpine`` -> PostgreSQL."""
    repository = image.split("@", 1)[0].rsplit(":", 1)[0]
    base = repository.rsplit("/", 1)[-1].lower()
    return _IMAGE_TECHNOLOGIES.get(base)


def _detect_compose(path: Path, root: Path, acc: _Accumulator) -> list[str]:
    """Services and their images. Returns the service names for the summary."""
    text = _read_text(path)
    if text is None:
        return []
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        logger.debug("skipping unparsable compose file %s: %s", path, exc)
        return []
    if not isinstance(document, dict):
        return []

    rel = _relative(path, root)
    services = document.get("services")
    if not isinstance(services, dict):
        return []

    for service_name, service in services.items():
        if not isinstance(service, dict):
            continue
        image = service.get("image")
        if not isinstance(image, str):
            continue
        entry = _image_technology(image)
        if entry is None:
            continue
        name, category = entry
        acc.add(
            name,
            category,
            evidence=Evidence(rel, f"service {service_name}: {image}"),
            version=_clean_version(image.rsplit(":", 1)[-1] if ":" in image else None),
            confidence=CONFIDENCE_IMAGE,
        )
    return sorted(str(name) for name in services)


def _detect_jvm_build(path: Path, root: Path, acc: _Accumulator) -> None:
    """Spring Boot, without an XML parser.

    A substring test on the build file is enough to answer the only question
    asked here, and parsing untrusted XML brings entity-expansion problems that
    a dependency-name lookup does not.
    """
    text = _read_text(path)
    if text is None:
        return
    rel = _relative(path, root)
    acc.add("Java", LANGUAGE, evidence=Evidence(rel, path.name))
    if "spring-boot" in text:
        acc.add("Spring Boot", BACKEND, evidence=Evidence(rel, "spring-boot"))


def _detect_env_files(root: Path) -> list[dict]:
    """Variable *names* the app expects, from example env files and nothing else.

    Only ``.env.example``-style templates are read, never a real ``.env``: a
    populated environment file is exactly the file most likely to contain a live
    credential, and the analyzer has no reason to open it. Names are recorded,
    values never are, and a credential-shaped name is flagged so the operator
    knows what has to be provisioned before a scan can reach anything.
    """
    templates = {".env.example", ".env.sample", ".env.template", ".env.dist"}
    found: dict[str, dict] = {}
    for path in _iter_files(root, templates):
        rel = _relative(path, root)
        text = _read_text(path)
        if text is None:
            continue
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            match = _ENV_LINE_RE.match(line)
            if match is None:
                continue
            name = match.group(1)
            found.setdefault(
                name,
                {"name": name, "secret": bool(_SECRET_NAME_RE.search(name)), "source": rel},
            )
    return sorted(found.values(), key=lambda item: item["name"])


# --------------------------------------------------------------------------- entry point


@dataclass
class StackReport:
    technologies: list[Technology] = field(default_factory=list)
    compose_services: list[str] = field(default_factory=list)
    env_vars: list[dict] = field(default_factory=list)
    has_openapi_document: bool = False

    def by_category(self, category: str) -> list[Technology]:
        return [tech for tech in self.technologies if tech.category == category]

    def names(self) -> list[str]:
        return [tech.name for tech in self.technologies]

    def to_dict(self) -> dict:
        return {
            "technologies": [tech.to_dict() for tech in self.technologies],
            "compose_services": self.compose_services,
            "env_vars": self.env_vars,
            "has_openapi_document": self.has_openapi_document,
        }


def detect_stack(repo_dir: Path) -> StackReport:
    """Everything CLAUDE.md section 6 asks for, from manifests and config alone."""
    acc = _Accumulator()
    compose_services: list[str] = []

    for path in _iter_files(repo_dir, {"package.json"}):
        _detect_package_json(path, repo_dir, acc)
    for path in _iter_files(repo_dir, {"pyproject.toml"}):
        _detect_pyproject(path, repo_dir, acc)
    for path in _iter_files(repo_dir, {"requirements.txt", "requirements-dev.txt"}):
        _detect_requirements_txt(path, repo_dir, acc)
    for path in _iter_files(repo_dir, {"pom.xml", "build.gradle", "build.gradle.kts"}):
        _detect_jvm_build(path, repo_dir, acc)

    marker_names = {name for name, _, _ in _CONFIG_MARKERS}
    for path in _iter_files(repo_dir, marker_names):
        for filename, tech_name, category in _CONFIG_MARKERS:
            if path.name == filename:
                acc.add(
                    tech_name,
                    category,
                    evidence=Evidence(_relative(path, repo_dir), path.name),
                    confidence=CONFIDENCE_CONFIG,
                )
        if path.name.startswith(("docker-compose", "compose.")):
            compose_services.extend(_detect_compose(path, repo_dir, acc))

    workflows = repo_dir / ".github" / "workflows"
    if workflows.is_dir() and any(workflows.glob("*.y*ml")):
        acc.add(
            "GitHub Actions",
            INFRASTRUCTURE,
            evidence=Evidence(".github/workflows", "workflow files"),
            confidence=CONFIDENCE_CONFIG,
        )

    openapi_files = _iter_files(repo_dir, _OPENAPI_FILENAMES)
    for path in openapi_files:
        acc.add(
            "OpenAPI",
            API,
            evidence=Evidence(_relative(path, repo_dir), path.name),
            confidence=CONFIDENCE_CONFIG,
        )

    report = StackReport(
        technologies=acc.technologies(),
        compose_services=sorted(set(compose_services)),
        env_vars=_detect_env_files(repo_dir),
        has_openapi_document=bool(openapi_files),
    )
    logger.info(
        "stack detection found %d technolog(ies) under %s", len(report.technologies), repo_dir
    )
    return report
