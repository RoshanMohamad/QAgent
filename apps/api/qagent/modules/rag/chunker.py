"""Split a checkout into retrievable chunks.

The unit of retrieval is a *symbol* wherever one can be identified - a function,
method or class - and a bounded line window everywhere else. That choice is the
whole reason this file exists rather than a naive 500-character splitter: the
answer to "why did POST /orders return 500" is a function, and a chunk that ends
halfway through one is a chunk that cannot answer it.

Python is parsed with `ast`, which is exact and already a dependency. JavaScript
and TypeScript get a regex pass over the handful of declaration forms that
actually appear in server code, in the same deliberately-narrow spirit as the
route parser next door: it recovers most of the surface and never pretends to be
a parser. Everything else falls back to line windows.

Nothing here executes, imports or evaluates a single line of the checkout
(ADR-0006, ADR-0009). It is text in, dataclasses out.
"""

from __future__ import annotations

import ast
import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from qagent.modules.discovery.routes import SKIP_DIRS

logger = logging.getLogger(__name__)

#: Extensions worth indexing. Config and markup are excluded on purpose: they
#: match query terms constantly (every `package.json` mentions every dependency)
#: and almost never contain the logic that explains a defect.
SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rb", ".java"}

#: A file larger than this is a bundle, a fixture or generated output, whatever
#: its extension says. Indexing it buys noise.
MAX_FILE_BYTES = 400_000

#: Line windows for files with no symbols we can find. Overlap keeps a function
#: that straddles a boundary retrievable from either side.
WINDOW_LINES = 60
WINDOW_OVERLAP = 15

#: Guards against a minified file that survived the size check: one 8000-char
#: line is not a useful retrieval unit and poisons the token statistics.
MAX_CHUNK_CHARS = 6_000

#: Floor for chunks that carry no symbol name - a stray decorator line or a
#: lone `return x` left over between two functions. These are not merely
#: useless, they actively win: BM25 normalises by document length, so a
#: one-line chunk containing a query term outscores the actual handler.
#: Named symbols are exempt, because `def healthz(): return "ok"` is short and
#: genuinely the answer to a query about health checks.
MIN_UNNAMED_CHUNK_CHARS = 60

_JS_SYMBOL_RE = re.compile(
    r"""^\s*(?:export\s+)?(?:default\s+)?
        (?:async\s+)?
        (?:
            function\s+(?P<fn>[A-Za-z_$][\w$]*)
          | class\s+(?P<cls>[A-Za-z_$][\w$]*)
          | (?:const|let|var)\s+(?P<const>[A-Za-z_$][\w$]*)\s*=\s*
            (?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>
        )""",
    re.VERBOSE,
)


@dataclass(frozen=True)
class CodeChunk:
    """One retrievable span of source.

    ``path`` is always relative to the repository root. An absolute path would
    leak the worker's filesystem layout into a bug report that gets pasted into
    an issue tracker.
    """

    path: str
    start_line: int
    end_line: int
    text: str
    symbol: str | None = None
    language: str = "text"

    @property
    def location(self) -> str:
        """What a bug report cites, e.g. ``services/orders.py:41-77 (create_order)``."""
        where = f"{self.path}:{self.start_line}-{self.end_line}"
        return f"{where} ({self.symbol})" if self.symbol else where

    def digest(self) -> str:
        """Stable identity for a chunk's content, used to avoid re-embedding
        text that has not changed between two indexing runs."""
        return hashlib.sha256(
            f"{self.path}:{self.start_line}:{self.text}".encode()
        ).hexdigest()

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "symbol": self.symbol,
            "language": self.language,
            "text": self.text,
        }


def _language_for(suffix: str) -> str:
    return {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".go": "go",
        ".rb": "ruby",
        ".java": "java",
    }.get(suffix, "text")


def _clip(text: str) -> str:
    return text if len(text) <= MAX_CHUNK_CHARS else text[:MAX_CHUNK_CHARS] + "\n... (truncated)"


def _window_chunks(rel_path: str, lines: list[str], language: str) -> list[CodeChunk]:
    chunks: list[CodeChunk] = []
    step = max(1, WINDOW_LINES - WINDOW_OVERLAP)
    for start in range(0, len(lines), step):
        window = lines[start : start + WINDOW_LINES]
        if not any(line.strip() for line in window):
            continue
        chunks.append(
            CodeChunk(
                path=rel_path,
                start_line=start + 1,
                end_line=start + len(window),
                text=_clip("\n".join(window)),
                language=language,
            )
        )
        if start + WINDOW_LINES >= len(lines):
            break
    return chunks


def chunk_python(rel_path: str, source: str) -> list[CodeChunk]:
    """One chunk per top-level function, method or class body.

    Methods are emitted individually and qualified (``OrderService.create``)
    rather than swallowed by their class, because a 600-line service class is
    not a useful retrieval unit and its name is the part worth keeping.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # A file we cannot parse is still worth indexing; it just gets windows.
        return _window_chunks(rel_path, source.splitlines(), "python")

    lines = source.splitlines()
    chunks: list[CodeChunk] = []
    covered: set[int] = set()

    def emit(node: ast.AST, name: str) -> None:
        start = getattr(node, "lineno", 1)
        # `node.lineno` points at `def`, not at the decorators above it. For a
        # web application that is exactly backwards: `@app.get("/admin/users")`
        # is the single most identifying line the handler has, and splitting it
        # off leaves the function unfindable by the route it serves.
        decorators = getattr(node, "decorator_list", None) or []
        if decorators:
            start = min(start, *(d.lineno for d in decorators))
        end = getattr(node, "end_lineno", start) or start
        text = "\n".join(lines[start - 1 : end])
        if not text.strip():
            return
        chunks.append(
            CodeChunk(
                path=rel_path,
                start_line=start,
                end_line=end,
                text=_clip(text),
                symbol=name,
                language="python",
            )
        )
        covered.update(range(start, end + 1))

    def emit_span(start: int, end: int, name: str) -> None:
        text = "\n".join(lines[start - 1 : end])
        if not text.strip():
            covered.update(range(start, end + 1))
            return
        chunks.append(
            CodeChunk(
                path=rel_path,
                start_line=start,
                end_line=end,
                text=_clip(text),
                symbol=name,
                language="python",
            )
        )
        covered.update(range(start, end + 1))

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            methods = [
                child
                for child in node.body
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
            ]
            if methods:
                # The class header and anything above the first method - ORM
                # columns, pydantic fields, a `Meta` - is kept as its own chunk
                # named after the class. Folding it into the first method would
                # misattribute it; leaving it unnamed would make a model
                # definition unfindable by the model's own name.
                emit_span(node.lineno, methods[0].lineno - 1, node.name)
                for method in methods:
                    emit(method, f"{node.name}.{method.name}")
            else:
                emit(node, node.name)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            emit(node, node.name)

    # Module-level code (imports, constants, route tables) is often exactly what
    # explains a misconfiguration, so it must not be dropped just because it sits
    # outside a def.
    leftover = [i for i in range(1, len(lines) + 1) if i not in covered and lines[i - 1].strip()]
    if leftover:
        chunks.extend(_contiguous_runs(rel_path, lines, leftover, "python"))

    return sorted(chunks, key=lambda c: c.start_line)


def _contiguous_runs(
    rel_path: str, lines: list[str], line_numbers: list[int], language: str
) -> list[CodeChunk]:
    """Group loose line numbers into chunks, splitting runs longer than a window."""
    chunks: list[CodeChunk] = []
    run: list[int] = []

    def flush() -> None:
        if not run:
            return
        for offset in range(0, len(run), WINDOW_LINES):
            piece = run[offset : offset + WINDOW_LINES]
            text = "\n".join(lines[i - 1] for i in piece)
            if text.strip():
                chunks.append(
                    CodeChunk(
                        path=rel_path,
                        start_line=piece[0],
                        end_line=piece[-1],
                        text=_clip(text),
                        language=language,
                    )
                )

    for number in line_numbers:
        if run and number != run[-1] + 1:
            flush()
            run = []
        run.append(number)
    flush()
    return chunks


def chunk_js_like(rel_path: str, source: str, language: str) -> list[CodeChunk]:
    """Split on declaration lines.

    A chunk runs from one declaration to the next, which is wrong for nested
    declarations and right for the flat handler/service files that HTTP servers
    are actually written in. Being narrow and legible beats being subtly wrong.
    """
    lines = source.splitlines()
    starts: list[tuple[int, str]] = []

    for index, line in enumerate(lines):
        match = _JS_SYMBOL_RE.match(line)
        if match:
            name = match.group("fn") or match.group("cls") or match.group("const")
            starts.append((index, name))

    if not starts:
        return _window_chunks(rel_path, lines, language)

    chunks: list[CodeChunk] = []

    # Whatever precedes the first declaration (imports, middleware setup).
    if starts[0][0] > 0:
        head = lines[: starts[0][0]]
        if any(line.strip() for line in head):
            chunks.append(
                CodeChunk(
                    path=rel_path,
                    start_line=1,
                    end_line=len(head),
                    text=_clip("\n".join(head)),
                    language=language,
                )
            )

    for position, (index, name) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        body = lines[index:end]
        if not any(line.strip() for line in body):
            continue
        chunks.append(
            CodeChunk(
                path=rel_path,
                start_line=index + 1,
                end_line=end,
                text=_clip("\n".join(body)),
                symbol=name,
                language=language,
            )
        )
    return chunks


def _drop_noise(chunks: list[CodeChunk]) -> list[CodeChunk]:
    """Remove unnamed fragments too small to answer anything.

    Applied once, at the boundary, rather than inside each chunker: every
    strategy produces these and every one of them should be filtered the same
    way.
    """
    return [
        c
        for c in chunks
        if c.symbol or len(c.text.strip()) >= MIN_UNNAMED_CHUNK_CHARS
    ]


def chunk_file(repo_dir: Path, path: Path) -> list[CodeChunk]:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("could not read %s: %s", path, exc)
        return []

    rel_path = path.relative_to(repo_dir).as_posix()
    language = _language_for(path.suffix)

    if language == "python":
        return _drop_noise(chunk_python(rel_path, source))
    if language in {"javascript", "typescript"}:
        return _drop_noise(chunk_js_like(rel_path, source, language))
    return _drop_noise(_window_chunks(rel_path, source.splitlines(), language))


def chunk_repository(repo_dir: Path, *, max_files: int = 2_000) -> list[CodeChunk]:
    """Walk a checkout and return every chunk worth indexing.

    ``max_files`` is a guard, not a tuning knob: a monorepo with 40,000 source
    files would otherwise turn one indexing run into an outage. When it bites,
    it is logged loudly rather than silently truncating.
    """
    if not repo_dir.is_dir():
        raise NotADirectoryError(f"not a directory: {repo_dir}")

    chunks: list[CodeChunk] = []
    seen = 0

    for path in sorted(repo_dir.rglob("*")):
        if path.suffix not in SOURCE_SUFFIXES or not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue

        seen += 1
        if seen > max_files:
            logger.warning(
                "stopped indexing at %d files; %s has more source than one index should hold",
                max_files,
                repo_dir,
            )
            break
        chunks.extend(chunk_file(repo_dir, path))

    logger.info("chunked %d file(s) into %d chunk(s)", seen, len(chunks))
    return chunks
