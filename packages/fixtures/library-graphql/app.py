"""Fourth fixture application: a GraphQL library API (CLAUDE.md section 6 lists
GraphQL beside REST; ADR-0003 wants ground truth for it).

Served by Flask with graphql-core and nothing else - no Strawberry, no Ariadne -
so errors are formatted the way graphql-core formats them by default: a resolver
that raises an ordinary exception produces an error with its raw message and
*no* ``extensions.code``. That is the harder case for detection on purpose.
Apollo's ``INTERNAL_SERVER_ERROR`` code would make a crash easy to spot; a
vanilla server only offers the exception text, which is what QAgent has to read
(apps/api/qagent/modules/graphql/errors.py). Errors this API raises *on
purpose* do carry a code (BAD_USER_INPUT, NOT_FOUND), the way a well-behaved
GraphQL API marks a client error.

Everything answers HTTP 200, as GraphQL servers do. No status code in this
file distinguishes a bug from a correct rejection.

Every bug is recorded in seeded_defects.yaml. Do not "fix" them. Run with:

    flask --app app run --port 8083
"""

from __future__ import annotations

from flask import Flask, jsonify, request
from graphql import GraphQLError, build_schema, graphql_sync

app = Flask(__name__)

SDL = """
type Book {
  id: ID!
  title: String!
  author: String!
  available: Boolean!
  tags: [String!]!
}

type Query {
  health: String!
  books(author: String): [Book!]!
  book(id: ID!): Book
}

type Mutation {
  addBook(title: String!, author: String!, tags: [String!]): Book!
  borrowBook(id: ID!): Book
  renameBook(id: ID!, title: String): Book
  returnBook(id: ID!): Book
}
"""

schema = build_schema(SDL)

BOOKS: dict[int, dict] = {
    1: {"id": 1, "title": "Dune", "author": "Frank Herbert", "available": True, "tags": ["sf"]},
    2: {
        "id": 2,
        "title": "The Left Hand of Darkness",
        "author": "Ursula K. Le Guin",
        "available": True,
        "tags": ["sf", "classic"],
    },
}


def _bad_input(message: str) -> GraphQLError:
    return GraphQLError(message, extensions={"code": "BAD_USER_INPUT"})


def _not_found(message: str) -> GraphQLError:
    return GraphQLError(message, extensions={"code": "NOT_FOUND"})


def _numeric_id(raw: str) -> int:
    """The correct guard, used by every resolver that isn't seeded to skip it."""
    if not str(raw).isdigit():
        raise _bad_input("id must be numeric")
    return int(raw)


# ----------------------------------------------------------------------- correct


def resolve_health(_info) -> str:
    return "ok"


def resolve_books(_info, author: str | None = None) -> list[dict]:
    """Correct: an explicit null author means "no filter", same as omitting it."""
    if author is None:
        return list(BOOKS.values())
    return [b for b in BOOKS.values() if b["author"] == author]


def resolve_return_book(_info, id: str) -> dict:  # noqa: A002 - GraphQL argument name
    """Correct: guards the cast and the existence, and says which went wrong."""
    book = BOOKS.get(_numeric_id(id))
    if book is None:
        raise _not_found("Book not found")
    book["available"] = True
    return book


# -------------------------------------------------------------------- seeded bugs


def resolve_book(_info, id: str) -> dict | None:  # noqa: A002
    """BUG-501: the identifier is cast without guarding.

    GraphQL's ID scalar accepts any string, so "not-a-number" passes schema
    validation and reaches this line. An absent-but-numeric id is handled
    correctly (it returns null), so this is isolated to the cast - the same
    shape as buggy-shop's BUG-001, in a GraphQL resolver.
    """
    return BOOKS.get(int(id))  # BUG-501: ValueError on a non-numeric id


def resolve_add_book(_info, title: str, author: str, tags: list[str] | None = None) -> dict:
    """BUG-502: an optional argument is dereferenced as if it were required.

    'tags' is nullable in the schema. Omitting it and sending an explicit null
    both reach ``", ".join(None)``. The omitted case is what the happy-path
    check sends (it only supplies required arguments), so even the baseline
    check finds this - one ground-truth endpoint, reached by two checks.
    """
    tag_line = ", ".join(tags)  # BUG-502: TypeError when tags is None
    book_id = max(BOOKS) + 1
    BOOKS[book_id] = {
        "id": book_id,
        "title": title,
        "author": author,
        "available": True,
        "tags": tag_line.split(", ") if tag_line else [],
    }
    return BOOKS[book_id]


def resolve_borrow_book(_info, id: str) -> dict:  # noqa: A002
    """BUG-503: resource existence is assumed before it is updated.

    The cast is guarded (a malformed id is a BAD_USER_INPUT error), but a
    well-formed absent id reaches ``book["available"] = False`` on None - the
    same isolation buggy-shop's BUG-002 and task-tracker's BUG-204 document.
    """
    book = BOOKS.get(_numeric_id(id))
    book["available"] = False  # BUG-503: TypeError when the book is absent
    return book


def resolve_rename_book(_info, id: str, **kwargs) -> dict:  # noqa: A002
    """BUG-504: an explicit null is treated as if it were a value.

    Omitting 'title' is handled - nothing changes. Sending ``title: null``,
    which the schema allows for a nullable argument, puts the key in kwargs
    with value None, and ``.strip()`` is called on it. The GraphQL-specific
    shape: "omitted" and "null" are different inputs, and a resolver written
    against only the first is the most common way to get the second wrong.
    """
    book = BOOKS.get(_numeric_id(id))
    if book is None:
        raise _not_found("Book not found")
    if "title" in kwargs:
        book["title"] = kwargs["title"].strip()  # BUG-504: AttributeError on null
    return book


ROOT = {
    "health": resolve_health,
    "books": resolve_books,
    "book": resolve_book,
    "addBook": resolve_add_book,
    "borrowBook": resolve_borrow_book,
    "renameBook": resolve_rename_book,
    "returnBook": resolve_return_book,
}


@app.post("/graphql")
def graphql_endpoint():
    payload = request.get_json(force=True, silent=True) or {}
    result = graphql_sync(
        schema,
        payload.get("query") or "",
        root_value=ROOT,
        variable_values=payload.get("variables"),
        operation_name=payload.get("operationName"),
    )
    body: dict = {"data": result.data}
    if result.errors:
        # graphql-core's default formatting: message, locations, path - and
        # extensions only when the error itself set them. No stack traces.
        body["errors"] = [error.formatted for error in result.errors]
    return jsonify(body), 200


@app.get("/health")
def http_health():
    """Plain HTTP readiness probe for CI; not part of the GraphQL surface."""
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8083)  # noqa: S104 - fixture app, evaluation-only
