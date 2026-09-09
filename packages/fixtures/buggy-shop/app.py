"""Fixture application with deliberately seeded defects.

This is the ground truth for the evaluation harness (ADR-0003). Every defect here is
recorded in seeded_defects.yaml, and several endpoints are deliberately *correct* so
the harness can measure false positives - the metric that decides whether anyone
trusts the tool.

Do not "fix" the bugs in this file. They are the specification.

Run it with:
    uvicorn app:app --port 8080
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

app = FastAPI(
    title="Buggy Shop",
    version="1.0.0",
    description="Fixture app for QAgent evaluation. Contains known, labelled defects.",
)

VALID_TOKEN = "qagent-fixture-valid-token"

# auto_error=True: missing credentials are rejected by the framework.
enforced_auth = HTTPBearer(auto_error=True)
# auto_error=False: the scheme is advertised in the OpenAPI document but never
# enforced. This is seeded defect BUG-003.
advertised_auth = HTTPBearer(auto_error=False)

PRODUCTS: dict[int, dict] = {
    1: {"id": 1, "name": "Mechanical keyboard", "price": 129.00, "stock": 4},
    2: {"id": 2, "name": "27-inch monitor", "price": 349.00, "stock": 0},
}
ORDERS: dict[int, dict] = {1: {"id": 1, "product_id": 1, "quantity": 1, "total": 129.00}}
USERS: dict[str, dict] = {"ada@example.com": {"email": "ada@example.com", "role": "admin"}}


def require_valid_token(
    credentials: HTTPAuthorizationCredentials = Depends(enforced_auth),
) -> str:
    """Correct implementation: verifies the token rather than merely requiring one."""
    if credentials.credentials != VALID_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    return credentials.credentials


# --------------------------------------------------------------------- correct


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok"}


@app.get("/products", tags=["products"])
def list_products() -> list[dict]:
    return list(PRODUCTS.values())


@app.get("/orders/{order_id}", tags=["orders"])
def get_order(order_id: str, _: str = Depends(require_valid_token)) -> dict:
    """Correct: validates the identifier, authenticates, and 404s cleanly."""
    if not order_id.isdigit():
        raise HTTPException(status_code=400, detail="order_id must be numeric")
    order = ORDERS.get(int(order_id))
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@app.get("/me", tags=["users"])
def me(_: str = Depends(require_valid_token)) -> dict:
    """Correct: a forged or absent token is rejected."""
    return {"email": "ada@example.com", "role": "admin"}


# ---------------------------------------------------------------- seeded bugs


@app.get("/products/{product_id}", tags=["products"])
def get_product(product_id: str) -> dict:
    """BUG-001: the identifier is cast without guarding.

    A non-numeric product_id raises ValueError, which surfaces as HTTP 500 where the
    contract documents 400 or 404. This is the single most common API defect there is.
    """
    pid = int(product_id)  # noqa: S101 - seeded defect, deliberate
    product = PRODUCTS.get(pid)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@app.post("/orders", status_code=201, tags=["orders"])
def create_order(payload: dict) -> dict:
    """BUG-002: the handler assumes the product exists before reading its price.

    This is the exact scenario described in CLAUDE.md section 13: an invalid
    product_id produces 500 instead of 400.
    """
    product = PRODUCTS.get(payload["product_id"])
    quantity = payload.get("quantity", 1)
    total = product["price"] * quantity  # AttributeError/TypeError when product is None
    order_id = max(ORDERS) + 1
    ORDERS[order_id] = {
        "id": order_id,
        "product_id": payload["product_id"],
        "quantity": quantity,
        "total": total,
    }
    return ORDERS[order_id]


@app.get("/admin/users", tags=["admin"])
def admin_list_users(
    credentials: HTTPAuthorizationCredentials = Depends(advertised_auth),
) -> list[dict]:
    """BUG-003: authentication is advertised but never enforced.

    The security scheme appears in the OpenAPI document, so the endpoint looks
    protected, but the credentials are never checked. An unauthenticated caller
    receives the full user list.
    """
    return list(USERS.values())


@app.post("/users", status_code=201, tags=["users"])
def create_user(payload: dict) -> dict:
    """BUG-004: a required field is dereferenced without validation.

    Omitting 'email' raises KeyError, producing 500 where 400 is documented.
    """
    email = payload["email"].lower()
    USERS[email] = {"email": email, "role": payload.get("role", "member")}
    return USERS[email]


# ------------------------------------------------------------------ contract

# Declare the documented error responses so QAgent can generate against a real
# contract rather than guessing. The application then fails to honour them.
for route in app.routes:
    responses = getattr(route, "responses", None)
    if responses is None:
        continue
    responses.setdefault(400, {"description": "Invalid request"})
    responses.setdefault(404, {"description": "Not found"})


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Leak a trace, as many real applications do in staging.

    This gives the triage classifier the stack-trace signal it keys on, and is itself
    an information-disclosure finding.
    """
    import traceback

    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal Server Error",
            "trace": traceback.format_exc(),
        },
    )
