"""Code Intelligence Engine: grouping discovered endpoints into modules,
flagging risky ones, and reading frontend routes off Next.js's own directory
convention.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from qagent.modules.analyzer.tree import (
    build_backend_modules,
    build_database_models,
    build_frontend_routes,
    build_project_tree,
)


@dataclass
class _Ep:
    method: str
    path: str
    risk_score: float = 0.1


def _write(root: Path, rel: str, content: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- backend modules


def test_groups_endpoints_by_leading_path_segment() -> None:
    endpoints = [
        _Ep("GET", "/api/v1/orders"),
        _Ep("POST", "/api/v1/orders"),
        _Ep("GET", "/api/v1/orders/{id}"),
        _Ep("GET", "/api/v1/products"),
    ]

    modules, _risky = build_backend_modules(endpoints)
    names = {m.name for m in modules}

    assert names == {"orders", "products"}
    orders = next(m for m in modules if m.name == "orders")
    assert len(orders.endpoints) == 3


def test_api_and_version_segments_are_not_module_names() -> None:
    modules, _ = build_backend_modules([_Ep("GET", "/api/v2/health")])

    assert modules[0].name == "health"


def test_root_path_becomes_root_module() -> None:
    modules, _ = build_backend_modules([_Ep("GET", "/")])

    assert modules[0].name == "root"


def test_auth_module_flagged_risky_regardless_of_score() -> None:
    _modules, risky = build_backend_modules([_Ep("POST", "/auth/login", risk_score=0.1)])

    names = {r.name for r in risky}
    assert "auth" in names


def test_high_score_module_flagged_even_without_keyword_match() -> None:
    _modules, risky = build_backend_modules([_Ep("DELETE", "/widgets/{id}", risk_score=0.7)])

    names = {r.name for r in risky}
    assert "widgets" in names


def test_low_score_unremarkable_module_not_flagged() -> None:
    _modules, risky = build_backend_modules([_Ep("GET", "/health", risk_score=0.1)])

    assert risky == []


def test_risky_components_sorted_by_score_descending() -> None:
    _modules, risky = build_backend_modules(
        [
            _Ep("GET", "/users/{id}", risk_score=0.4),
            _Ep("POST", "/auth/login", risk_score=0.9),
        ]
    )

    assert [r.name for r in risky] == ["auth", "users"]


def test_empty_endpoint_list_yields_empty_tree() -> None:
    modules, risky = build_backend_modules([])

    assert modules == []
    assert risky == []


# --------------------------------------------------------------------------- frontend routes


def test_app_router_routes_discovered(tmp_path: Path) -> None:
    _write(tmp_path, "app/page.tsx")
    _write(tmp_path, "app/login/page.tsx")
    _write(tmp_path, "app/projects/[projectId]/page.tsx")

    routes = build_frontend_routes(tmp_path)

    assert routes == ["/", "/login", "/projects/{projectId}"]


def test_app_router_route_groups_do_not_appear_in_path(tmp_path: Path) -> None:
    _write(tmp_path, "app/(marketing)/about/page.tsx")

    routes = build_frontend_routes(tmp_path)

    assert routes == ["/about"]


def test_pages_router_routes_discovered(tmp_path: Path) -> None:
    _write(tmp_path, "pages/index.tsx")
    _write(tmp_path, "pages/dashboard.tsx")
    _write(tmp_path, "pages/products/[id].tsx")

    routes = build_frontend_routes(tmp_path)

    assert routes == ["/", "/dashboard", "/products/{id}"]


def test_pages_router_api_directory_excluded(tmp_path: Path) -> None:
    _write(tmp_path, "pages/api/login.ts")
    _write(tmp_path, "pages/dashboard.tsx")

    routes = build_frontend_routes(tmp_path)

    assert routes == ["/dashboard"]


def test_pages_router_special_files_excluded(tmp_path: Path) -> None:
    _write(tmp_path, "pages/_app.tsx")
    _write(tmp_path, "pages/_document.tsx")
    _write(tmp_path, "pages/404.tsx")

    routes = build_frontend_routes(tmp_path)

    assert routes == []


def test_node_modules_app_directory_ignored(tmp_path: Path) -> None:
    _write(tmp_path, "node_modules/some-pkg/app/page.tsx")

    routes = build_frontend_routes(tmp_path)

    assert routes == []


def test_no_frontend_directories_yields_empty_list(tmp_path: Path) -> None:
    assert build_frontend_routes(tmp_path) == []


# --------------------------------------------------------------------------- database models


def test_sqlalchemy_tablenames_detected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "models.py",
        'class User(Base):\n    __tablename__ = "users"\n\n'
        'class Order(Base):\n    __tablename__ = "orders"\n',
    )

    assert build_database_models(tmp_path) == ["orders", "users"]


def test_django_models_detected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "models.py",
        "from django.db import models\n\n"
        "class Product(models.Model):\n    name = models.CharField()\n",
    )

    assert build_database_models(tmp_path) == ["Product"]


def test_prisma_models_detected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "schema.prisma",
        "model User {\n  id Int @id\n}\n\nmodel Order {\n  id Int @id\n}\n",
    )

    assert build_database_models(tmp_path) == ["Order", "User"]


def test_no_orm_source_yields_empty_list(tmp_path: Path) -> None:
    _write(tmp_path, "main.py", "print('hello')\n")

    assert build_database_models(tmp_path) == []


# --------------------------------------------------------------------------- integration


def test_build_project_tree_combines_all_three(tmp_path: Path) -> None:
    _write(tmp_path, "app/page.tsx")
    _write(tmp_path, "models.py", 'class Order(Base):\n    __tablename__ = "orders"\n')
    endpoints = [_Ep("POST", "/api/v1/orders", risk_score=0.5)]

    tree = build_project_tree(tmp_path, endpoints)

    assert tree.frontend == ["/"]
    assert tree.database == ["orders"]
    assert [m.name for m in tree.backend] == ["orders"]
    assert tree.risky_components  # money-movement keyword match
    assert tree.to_dict()["frontend"] == ["/"]
