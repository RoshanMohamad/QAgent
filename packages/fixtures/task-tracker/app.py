"""Second fixture application: a Flask task tracker (ADR-0003 wants ground truth
that isn't all one framework).

buggy-shop is FastAPI, which publishes its own OpenAPI document for free. Flask
does not, so this app hand-writes one at GET /openapi.json -- deliberately, so
discovery exercises "a document was found and it happens to be hand-authored"
rather than only ever "a framework generated it correctly."

Every bug here is recorded in seeded_defects.yaml, same contract as buggy-shop.
Do not "fix" the bugs in this file. They are the specification -- including
BUG-201, which is seeded specifically because the current generator rules
cannot catch it (see that file for why). Run it with:

    flask --app app run --port 8081
"""

from __future__ import annotations

import traceback

from flask import Flask, jsonify, request

app = Flask(__name__)

TOKENS = {
    "task-tracker-alice-token": "alice",
    "task-tracker-bob-token": "bob",
}

TASKS: dict[int, dict] = {
    1: {"id": 1, "title": "Draft the Q3 roadmap", "done": False, "owner": "alice"},
    2: {"id": 2, "title": "Rotate the signing key", "done": False, "owner": "bob"},
}


class ApiError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def current_user() -> str:
    """Correct implementation: verifies the bearer token names a real user."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise ApiError(401, "Missing bearer token")
    user = TOKENS.get(header.removeprefix("Bearer ").strip())
    if user is None:
        raise ApiError(401, "Invalid token")
    return user


@app.errorhandler(ApiError)
def handle_api_error(exc: ApiError):
    return jsonify({"detail": exc.detail}), exc.status


@app.errorhandler(Exception)
def handle_unexpected(exc: Exception):
    """Leak a trace, same rationale as buggy-shop's own handler: it gives the
    triage classifier the stack-trace signal it keys on, and information
    disclosure in a staging error page is itself a real, common finding."""
    return (
        jsonify({"detail": "Internal Server Error", "trace": traceback.format_exc()}),
        500,
    )


# --------------------------------------------------------------------- correct


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/tasks")
def list_tasks():
    """Correct: scoped to the caller's own tasks, never another user's."""
    user = current_user()
    return jsonify([t for t in TASKS.values() if t["owner"] == user])


# ---------------------------------------------------------------- seeded bugs


@app.get("/tasks/<task_id>")
def get_task(task_id: str):
    """BUG-201: authenticates the caller but never checks ownership.

    Correct on every axis a generated check exercises -- a malformed id is
    rejected with 400, an absent id with 404, and no credentials with 401 -- so
    every one of the seven rules in generator/rules.py passes here. The defect
    only shows up by comparing what two different valid identities can read,
    which none of them do (see seeded_defects.yaml, BUG-201). This is a
    documented gap, not a claim the harness scores against as detected.
    """
    current_user()
    if not task_id.isdigit():
        raise ApiError(400, "task_id must be numeric")
    task = TASKS.get(int(task_id))
    if task is None:
        raise ApiError(404, "Task not found")
    return jsonify(task)  # missing: `if task["owner"] != user: raise ApiError(404, ...)`


@app.post("/tasks")
def create_task():
    """BUG-202: a required field is dereferenced without validation.

    Omitting 'title', or sending a non-string 'title', both crash: the former
    with KeyError, the latter when `.strip()` is called on it. Unauthenticated
    by design, same as buggy-shop's own POST /orders and POST /users - the
    point of this endpoint is the missing input validation, not auth, and
    requiring a token here would make every negative case 401 before it ever
    reached the bug (see BUG-201/BUG-205 for where auth *is* the point).
    """
    payload = request.get_json(force=True, silent=True) or {}
    title = payload["title"].strip()
    task_id = max(TASKS) + 1
    TASKS[task_id] = {"id": task_id, "title": title, "done": False, "owner": "anonymous"}
    return jsonify(TASKS[task_id]), 201


@app.put("/tasks/<task_id>")
def update_task(task_id: str):
    """BUG-203: the identifier is cast without guarding.

    A non-numeric task_id raises ValueError, surfacing as 500 where 400 or 404
    is documented. Absent resources are handled correctly (404), so this is
    isolated to the identifier cast, same shape as buggy-shop's BUG-001.
    """
    payload = request.get_json(force=True, silent=True) or {}
    task = TASKS.get(int(task_id))  # noqa: S101 - seeded defect, deliberate
    if task is None:
        raise ApiError(404, "Task not found")
    if "title" in payload:
        task["title"] = payload["title"]
    if "done" in payload:
        task["done"] = payload["done"]
    return jsonify(task)


@app.delete("/tasks/<task_id>")
def delete_task(task_id: str):
    """BUG-204: the handler assumes the resource exists before removing it.

    A well-formed but absent id raises KeyError from the dict `del`, producing
    500 where 404 is documented. The identifier format is validated correctly,
    so this is isolated to the existence check, not the cast.
    """
    if not task_id.isdigit():
        raise ApiError(400, "task_id must be numeric")
    del TASKS[int(task_id)]  # BUG-204: KeyError when the id is absent, instead of 404
    return "", 204


@app.get("/admin/export")
def admin_export():
    """BUG-205: authentication is advertised but never enforced.

    The OpenAPI document declares this operation as bearer-protected, so it
    looks the same as every other protected endpoint here, but the handler
    never calls current_user(). Same defect shape as buggy-shop's BUG-003,
    seeded again deliberately: one instance proves a rule fires once, a second
    instance in a different framework proves the rule generalises.
    """
    return jsonify(list(TASKS.values()))


@app.post("/tasks/bulk")
def bulk_create():
    """BUG-206: a wrongly typed field crashes instead of being rejected.

    'items' is documented as an array of titles. A well-formed request works;
    an object where an array was expected fails the `items[0]` lookup (a dict
    keyed by string has no integer key 0), producing 500 where 400 is
    documented. Omitting 'items' entirely crashes the same way, one line
    earlier -- both are the same ground-truth endpoint (seeded_defects.yaml
    matches by endpoint, not by which line raised).
    """
    payload = request.get_json(force=True, silent=True) or {}
    items = payload["items"]
    _ = items[0]
    created = []
    for title in items:
        task_id = max(TASKS) + 1
        TASKS[task_id] = {"id": task_id, "title": title, "done": False, "owner": "anonymous"}
        created.append(TASKS[task_id])
    return jsonify(created), 201


# ------------------------------------------------------------------ contract

_TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "integer"},
        "title": {"type": "string"},
        "done": {"type": "boolean"},
        "owner": {"type": "string"},
    },
}

_BEARER_SECURITY = [{"bearerAuth": []}]

_TASK_ID_PARAM = {
    "name": "task_id",
    "in": "path",
    "required": True,
    "schema": {"type": "integer"},
}

_OPENAPI_DOCUMENT = {
    "openapi": "3.0.0",
    "info": {
        "title": "Task Tracker",
        "version": "1.0.0",
        "description": "Fixture app for QAgent evaluation. Contains known, labelled defects.",
    },
    "components": {
        "securitySchemes": {
            "bearerAuth": {"type": "http", "scheme": "bearer"},
        }
    },
    "paths": {
        "/health": {"get": {"operationId": "health", "responses": {"200": {}}}},
        "/tasks": {
            "get": {
                "operationId": "list_tasks",
                "security": _BEARER_SECURITY,
                "responses": {"200": {}},
            },
            "post": {
                "operationId": "create_task",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["title"],
                                "properties": {"title": {"type": "string"}},
                            }
                        }
                    }
                },
                "responses": {"201": {}, "400": {}},
            },
        },
        "/tasks/{task_id}": {
            "get": {
                "operationId": "get_task",
                "security": _BEARER_SECURITY,
                "parameters": [_TASK_ID_PARAM],
                "responses": {"200": {}, "400": {}, "404": {}},
            },
            "put": {
                "operationId": "update_task",
                "parameters": [_TASK_ID_PARAM],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "done": {"type": "boolean"},
                                },
                            }
                        }
                    }
                },
                "responses": {"200": {}, "404": {}},
            },
            "delete": {
                "operationId": "delete_task",
                "parameters": [_TASK_ID_PARAM],
                "responses": {"204": {}, "400": {}, "404": {}},
            },
        },
        "/tasks/bulk": {
            "post": {
                "operationId": "bulk_create",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["items"],
                                "properties": {
                                    "items": {"type": "array", "items": {"type": "string"}}
                                },
                            }
                        }
                    }
                },
                "responses": {"201": {}, "400": {}},
            }
        },
        "/admin/export": {
            "get": {
                "operationId": "admin_export",
                "security": _BEARER_SECURITY,
                "responses": {"200": {}},
            }
        },
    },
}


@app.get("/openapi.json")
def openapi_document():
    return jsonify(_OPENAPI_DOCUMENT)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8081)  # noqa: S104 - fixture app, evaluation-only
