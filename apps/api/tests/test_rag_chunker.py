"""Chunking a checkout: symbols where they can be found, windows everywhere else,
and nothing from the checkout ever executed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qagent.modules.rag.chunker import (
    MAX_CHUNK_CHARS,
    CodeChunk,
    chunk_js_like,
    chunk_python,
    chunk_repository,
)


def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ------------------------------------------------------------------ python


def test_functions_become_their_own_chunks() -> None:
    source = "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n"

    chunks = chunk_python("app.py", source)

    assert {c.symbol for c in chunks} == {"alpha", "beta"}
    assert all(c.language == "python" for c in chunks)


def test_methods_are_qualified_by_their_class() -> None:
    """A 600-line service class is not a useful retrieval unit; its method is."""
    source = (
        "class OrderService:\n"
        "    def create(self, item):\n"
        "        return item.price\n"
        "\n"
        "    def cancel(self, order_id):\n"
        "        return order_id\n"
    )

    chunks = chunk_python("svc.py", source)

    assert {c.symbol for c in chunks} == {
        "OrderService",
        "OrderService.create",
        "OrderService.cancel",
    }
    body = next(c for c in chunks if c.symbol == "OrderService.create")
    assert "item.price" in body.text


def test_class_attributes_keep_the_class_name(tmp_path: Path) -> None:
    """An ORM model is all class attributes; it has to stay findable by its name."""
    source = (
        "class Order(Base):\n"
        "    total = Column(Numeric)\n"
        "\n"
        "    def save(self):\n"
        "        pass\n"
    )

    header = next(c for c in chunk_python("m.py", source) if c.symbol == "Order")

    assert "total = Column(Numeric)" in header.text


def test_class_without_methods_is_kept_whole() -> None:
    source = "class Config:\n    debug = True\n    port = 8080\n"

    chunks = chunk_python("config.py", source)

    assert [c.symbol for c in chunks] == ["Config"]


def test_module_level_code_is_not_dropped() -> None:
    """Route tables and constants often are the thing that explains a defect."""
    source = "import os\n\nDEBUG = True\n\n\ndef handler():\n    return DEBUG\n"

    chunks = chunk_python("app.py", source)

    assert any(c.symbol is None and "DEBUG = True" in c.text for c in chunks)
    assert any(c.symbol == "handler" for c in chunks)


def test_unparseable_python_still_gets_indexed() -> None:
    chunks = chunk_python("broken.py", "def oops(:\n    this is not python\n")

    assert chunks
    assert all(c.symbol is None for c in chunks)


def test_line_numbers_point_at_the_real_source() -> None:
    source = "# header\n# header\n\ndef target():\n    return 7\n"

    chunk = next(c for c in chunk_python("app.py", source) if c.symbol == "target")

    assert chunk.start_line == 4
    assert source.splitlines()[chunk.start_line - 1] == "def target():"


def test_location_is_what_a_bug_report_can_cite() -> None:
    chunk = CodeChunk(
        path="services/orders.py", start_line=41, end_line=77, text="...", symbol="create_order"
    )

    assert chunk.location == "services/orders.py:41-77 (create_order)"


# ---------------------------------------------------------------- javascript


def test_js_declarations_become_chunks() -> None:
    source = (
        "import express from 'express'\n"
        "\n"
        "function createOrder(req, res) {\n"
        "  return res.json({})\n"
        "}\n"
        "\n"
        "const cancelOrder = async (req) => {\n"
        "  return null\n"
        "}\n"
    )

    chunks = chunk_js_like("routes.js", source, "javascript")
    symbols = {c.symbol for c in chunks}

    assert "createOrder" in symbols
    assert "cancelOrder" in symbols
    # The import preamble is kept as an unnamed chunk.
    assert any(c.symbol is None and "express" in c.text for c in chunks)


def test_js_without_declarations_falls_back_to_windows() -> None:
    chunks = chunk_js_like("data.js", "\n".join(f"line {i}" for i in range(200)), "javascript")

    assert len(chunks) > 1
    assert all(c.symbol is None for c in chunks)


# ---------------------------------------------------------------- repository


def test_walks_a_checkout_and_skips_vendored_trees(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "def handler():\n    return 1\n")
    _write(tmp_path, "node_modules/lib/index.js", "function vendored() {}\n")
    _write(tmp_path, ".venv/lib/thing.py", "def vendored():\n    pass\n")
    _write(tmp_path, "README.md", "# not source\n")

    chunks = chunk_repository(tmp_path)
    paths = {c.path for c in chunks}

    assert paths == {"app.py"}


def test_paths_are_relative_so_nothing_leaks_the_worker_filesystem(tmp_path: Path) -> None:
    _write(tmp_path, "src/api/orders.py", "def create():\n    pass\n")

    chunks = chunk_repository(tmp_path)

    assert all(not Path(c.path).is_absolute() for c in chunks)
    assert chunks[0].path == "src/api/orders.py"


def test_oversized_files_are_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "bundle.js", "x = 1;\n" * 100_000)
    _write(tmp_path, "small.py", "def ok():\n    pass\n")

    assert {c.path for c in chunk_repository(tmp_path)} == {"small.py"}


def test_minified_line_is_clipped(tmp_path: Path) -> None:
    _write(tmp_path, "min.js", "var a=" + "1+" * 10_000 + "1;\n")

    for chunk in chunk_repository(tmp_path):
        assert len(chunk.text) <= MAX_CHUNK_CHARS + 20


def test_max_files_guard_stops_the_walk(tmp_path: Path) -> None:
    for i in range(12):
        _write(tmp_path, f"mod_{i}.py", f"def f_{i}():\n    pass\n")

    chunks = chunk_repository(tmp_path, max_files=3)

    assert len({c.path for c in chunks}) == 3


def test_missing_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        chunk_repository(tmp_path / "nope")


def test_digest_is_stable_and_content_sensitive() -> None:
    a = CodeChunk(path="a.py", start_line=1, end_line=2, text="x = 1")
    b = CodeChunk(path="a.py", start_line=1, end_line=2, text="x = 1")
    c = CodeChunk(path="a.py", start_line=1, end_line=2, text="x = 2")

    assert a.digest() == b.digest()
    assert a.digest() != c.digest()
