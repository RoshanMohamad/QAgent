"""Stack detection: reads manifests and config files only, never executes
anything, and every detection carries the file that proved it.
"""

from __future__ import annotations

import json
from pathlib import Path

from qagent.modules.analyzer.stack import (
    AUTH,
    BACKEND,
    DATABASE,
    FRONTEND,
    INFRASTRUCTURE,
    LANGUAGE,
    TESTING,
    detect_stack,
)


def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_detects_node_stack_from_package_json(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "package.json",
        json.dumps(
            {
                "dependencies": {"next": "^15.0.1", "react": "19.0.0"},
                "devDependencies": {"typescript": "^5.7.3", "playwright": "1.48.0"},
            }
        ),
    )

    report = detect_stack(tmp_path)
    names = {tech.name: tech for tech in report.technologies}

    assert "Next.js" in names
    assert names["Next.js"].version == "15.0.1"
    assert names["Next.js"].category == FRONTEND
    assert "React" in names
    assert "TypeScript" in names
    assert names["TypeScript"].category == LANGUAGE
    assert "Playwright" in names
    assert names["Playwright"].category == TESTING
    assert "Node.js" in names


def test_ignores_unknown_npm_packages(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", json.dumps({"dependencies": {"left-pad": "1.0.0"}}))

    report = detect_stack(tmp_path)

    assert "left-pad" not in report.names()
    # The manifest itself is still evidence of a Node.js project.
    assert "Node.js" in report.names()


def test_detects_python_stack_from_pyproject(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "pyproject.toml",
        """
[project]
dependencies = ["fastapi>=0.115", "sqlalchemy>=2.0.36", "pyjwt>=2.9"]

[project.optional-dependencies]
dev = ["pytest>=8.3"]
""",
    )

    report = detect_stack(tmp_path)
    names = {tech.name: tech for tech in report.technologies}

    assert names["FastAPI"].category == BACKEND
    assert names["FastAPI"].version == "0.115"
    assert names["SQLAlchemy"].category == DATABASE
    assert names["JWT"].category == AUTH
    assert names["pytest"].category == TESTING


def test_detects_python_stack_from_requirements_txt(tmp_path: Path) -> None:
    _write(tmp_path, "requirements.txt", "django==5.1\npsycopg2-binary==2.9.9\n# a comment\n")

    report = detect_stack(tmp_path)
    names = report.names()

    assert "Django" in names
    assert "PostgreSQL" in names


def test_corroborated_detection_raises_confidence(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", json.dumps({"dependencies": {"next": "15.0.0"}}))
    _write(tmp_path, "next.config.js", "module.exports = {}\n")

    report = detect_stack(tmp_path)
    next_js = next(t for t in report.technologies if t.name == "Next.js")

    assert len(next_js.evidence) == 2
    assert next_js.confidence == 0.99


def test_detects_docker_and_compose_services(tmp_path: Path) -> None:
    _write(tmp_path, "Dockerfile", "FROM python:3.12\n")
    _write(
        tmp_path,
        "docker-compose.yml",
        """
services:
  db:
    image: postgres:16-alpine
  cache:
    image: redis:7
  api:
    build: .
""",
    )

    report = detect_stack(tmp_path)
    names = {tech.name: tech for tech in report.technologies}

    assert "Docker" in names
    assert "Docker Compose" in names
    assert "PostgreSQL" in names
    assert names["PostgreSQL"].version == "16-alpine"
    assert names["PostgreSQL"].category == DATABASE
    assert "Redis" in names
    assert report.compose_services == ["api", "cache", "db"]


def test_env_example_names_are_recorded_but_never_values(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".env.example",
        "DATABASE_URL=postgres://localhost/db\nQAGENT_SECRET_KEY=changeme\nPORT=8080\n",
    )

    report = detect_stack(tmp_path)
    by_name = {item["name"]: item for item in report.env_vars}

    assert "DATABASE_URL" in by_name
    assert by_name["DATABASE_URL"]["secret"] is True
    assert by_name["QAGENT_SECRET_KEY"]["secret"] is True
    assert by_name["PORT"]["secret"] is False
    # Only the variable name is stored anywhere in the report.
    assert "changeme" not in json.dumps(report.to_dict())
    assert "postgres://localhost/db" not in json.dumps(report.to_dict())


def test_real_env_file_is_never_read(tmp_path: Path) -> None:
    """Only .env.example-style templates are opened -- never a populated .env,
    which is exactly the file most likely to hold a live credential."""
    _write(tmp_path, ".env", "QAGENT_SECRET_KEY=a-real-production-secret\n")

    report = detect_stack(tmp_path)

    assert report.env_vars == []
    assert "a-real-production-secret" not in json.dumps(report.to_dict())


def test_github_actions_detected_from_workflow_directory(tmp_path: Path) -> None:
    _write(tmp_path, ".github/workflows/ci.yml", "name: ci\non: [push]\n")

    report = detect_stack(tmp_path)

    assert "GitHub Actions" in report.names()
    assert any(t.category == INFRASTRUCTURE for t in report.technologies if t.name == "GitHub Actions")


def test_openapi_document_presence_detected(tmp_path: Path) -> None:
    _write(tmp_path, "openapi.json", json.dumps({"openapi": "3.0.0"}))

    report = detect_stack(tmp_path)

    assert report.has_openapi_document is True


def test_empty_repo_yields_empty_report(tmp_path: Path) -> None:
    report = detect_stack(tmp_path)

    assert report.technologies == []
    assert report.compose_services == []
    assert report.env_vars == []
    assert report.has_openapi_document is False


def test_node_modules_are_never_walked(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "node_modules/some-lib/package.json",
        json.dumps({"dependencies": {"django": "1.0"}}),
    )

    report = detect_stack(tmp_path)

    assert report.technologies == []


def test_skips_unparsable_manifest_without_raising(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", "{not valid json")

    report = detect_stack(tmp_path)

    assert report.technologies == []
