// Third fixture application: a Node.js notes API, no framework and no
// dependencies (ADR-0003 wants ground truth that isn't all one language, and
// buggy-shop/task-tracker are both Python).
//
// Discovery needs a document either way, so this hand-authors one at
// GET /openapi.json, same reason and same contract as task-tracker's.
//
// Every bug here is recorded in seeded_defects.yaml. Do not "fix" the bugs in
// this file -- they are the specification, including BUG-405, seeded because
// it is a defect shape neither buggy-shop nor task-tracker has: a check that
// exists and runs, but checks the wrong thing (CLAUDE.md section 11's "Token
// manipulation"). Run it with:
//
//     node server.js

"use strict";

const crypto = require("crypto");
const http = require("http");

const PORT = 8082;

// Fixture-only. A real deployment reads this from a secret store
// (modules/secrets/ in the API), never a literal in source.
const SECRET = "qagent-fixture-secret";

const NOTES = new Map([
  [1, { id: 1, title: "Renew the TLS certificate", done: false, owner: "alice" }],
  [2, { id: 2, title: "Write the incident postmortem", done: false, owner: "alice" }],
]);
let nextId = 3;

// --------------------------------------------------------------- JWT-shaped tokens

function base64url(buffer) {
  return buffer.toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function base64urlDecode(segment) {
  const padded = segment.replace(/-/g, "+").replace(/_/g, "/");
  return Buffer.from(padded, "base64").toString("utf8");
}

/** Builds a correctly HS256-signed token. Used once at startup for the one
 * identity this fixture ships (there is no login flow -- same simplification
 * task-tracker makes with its static TOKENS map). */
function issueToken(claims) {
  const header = base64url(Buffer.from(JSON.stringify({ alg: "HS256", typ: "JWT" })));
  const payload = base64url(Buffer.from(JSON.stringify(claims)));
  const signature = base64url(
    crypto.createHmac("sha256", SECRET).update(`${header}.${payload}`).digest(),
  );
  return `${header}.${payload}.${signature}`;
}

const ALICE_TOKEN = issueToken({ sub: "alice", role: "admin" });

function bearerToken(req) {
  const header = req.headers["authorization"] || "";
  return header.startsWith("Bearer ") ? header.slice("Bearer ".length) : null;
}

/** Correct verification: checks the signature before trusting anything in the
 * payload. Used by every endpoint except the one seeded to skip this
 * (admin_audit_log, BUG-405) -- so the failure is isolated to that one
 * endpoint, the same way every other fixture isolates its bugs to one cause. */
function verifiedClaims(req) {
  const token = bearerToken(req);
  if (!token) return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  const [header, payload, signature] = parts;
  const expected = base64url(
    crypto.createHmac("sha256", SECRET).update(`${header}.${payload}`).digest(),
  );
  const a = Buffer.from(signature);
  const b = Buffer.from(expected);
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) return null;
  try {
    return JSON.parse(base64urlDecode(payload));
  } catch {
    return null;
  }
}

/** BUG-405: reads the claims out of a JWT-shaped token without ever checking
 * `signature` (or rejecting `alg: "none"`), so any caller who can construct
 * the *shape* of a token controls what it claims. This is what
 * `case_invalid_token` (modules/generator/rules.py) sends: a structurally
 * valid, syntactically well-formed, entirely unsigned admin claim. */
function unsafeAdminClaims(req) {
  const token = bearerToken(req);
  if (!token) return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    return JSON.parse(base64urlDecode(parts[1]));
  } catch {
    return null;
  }
}

// ------------------------------------------------------------------- helpers

function send(res, status, body) {
  const text = JSON.stringify(body);
  res.writeHead(status, { "content-type": "application/json" });
  res.end(text);
}

/** Same rationale as buggy-shop/task-tracker's own handlers: a leaked trace
 * gives the triage classifier the signal it keys on, and it is itself a real,
 * common finding in a staging error page. */
function sendServerError(res, err) {
  send(res, 500, { detail: "Internal Server Error", trace: String(err && err.stack) });
}

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    let raw = "";
    req.on("data", (chunk) => {
      raw += chunk;
    });
    req.on("end", () => {
      if (!raw) return resolve({});
      try {
        resolve(JSON.parse(raw));
      } catch {
        resolve({});
      }
    });
    req.on("error", reject);
  });
}

// --------------------------------------------------------------- correct routes

function health(_req, res) {
  send(res, 200, { status: "ok" });
}

/** Correct: scoped to the caller's own notes, and a missing or forged token
 * is rejected the same way BUG-405's endpoint should have rejected one. */
function listNotes(req, res) {
  const claims = verifiedClaims(req);
  if (!claims) return send(res, 401, { detail: "Missing or invalid bearer token" });
  send(
    res,
    200,
    [...NOTES.values()].filter((n) => n.owner === claims.sub),
  );
}

// ---------------------------------------------------------------- seeded bugs

/** BUG-401: a required field is dereferenced without validation.
 *
 * Omitting 'title', or sending a non-string 'title', both crash: the string
 * method `.trim()` does not exist on `undefined` or on a number. Both are the
 * same ground-truth endpoint (seeded_defects.yaml matches by endpoint, not by
 * which generated check raised it) -- same convention task-tracker's
 * BUG-206 documents.
 */
async function createNote(req, res) {
  const claims = verifiedClaims(req);
  if (!claims) return send(res, 401, { detail: "Missing or invalid bearer token" });
  const body = await readJsonBody(req);
  const title = body.title.trim(); // BUG-401: no validation before .trim()
  const note = { id: nextId++, title, done: false, owner: claims.sub };
  NOTES.set(note.id, note);
  send(res, 201, note);
}

/** BUG-402: the identifier is cast without guarding.
 *
 * A non-numeric id parses to `NaN`; `NOTES.get(NaN)` is `undefined`, and
 * reading `.title` off it throws where 400 or 404 is documented. Same shape
 * as buggy-shop's BUG-001 and task-tracker's BUG-203, in a third language.
 */
function getNote(req, res, id) {
  const claims = verifiedClaims(req);
  if (!claims) return send(res, 401, { detail: "Missing or invalid bearer token" });
  const numericId = Number(id);
  const note = NOTES.get(numericId); // BUG-402: no Number.isInteger guard
  send(res, 200, { id: note.id, title: note.title, done: note.done });
}

/** BUG-403: resource existence is assumed before it is updated.
 *
 * A well-formed but absent id crashes on `note.done = true` instead of
 * returning 404 -- isolated to that path; a malformed id is rejected
 * correctly first, same isolation buggy-shop's BUG-002 and task-tracker's
 * BUG-204 document for their own assumed-existence bugs.
 */
function completeNote(req, res, id) {
  const claims = verifiedClaims(req);
  if (!claims) return send(res, 401, { detail: "Missing or invalid bearer token" });
  const numericId = Number(id);
  if (!Number.isInteger(numericId)) return send(res, 400, { detail: "id must be numeric" });
  const note = NOTES.get(numericId);
  note.done = true; // BUG-403: no existence check before this line
  send(res, 200, note);
}

/** Correct: the same auth/validation/existence order every other endpoint
 * here uses. Not the auth-not-enforced bug -- see `adminListNotes` below for
 * why that one is a GET rather than this. */
function deleteNote(req, res, id) {
  const claims = verifiedClaims(req);
  if (!claims) return send(res, 401, { detail: "Missing or invalid bearer token" });
  const numericId = Number(id);
  if (!Number.isInteger(numericId)) return send(res, 400, { detail: "id must be numeric" });
  if (!NOTES.has(numericId)) return send(res, 404, { detail: "Note not found" });
  NOTES.delete(numericId);
  res.writeHead(204);
  res.end();
}

/** BUG-404: authentication is advertised but never enforced.
 *
 * The OpenAPI document declares this operation bearer-protected, so it looks
 * identical to every other protected endpoint here, but the handler never
 * calls `verifiedClaims`. Same defect shape as buggy-shop's BUG-003 and
 * task-tracker's BUG-205, seeded a third time to show the rule generalises
 * across a third language and a third auth scheme, not just a third
 * framework.
 *
 * A GET, deliberately: `case_unauthenticated`'s negative request and
 * `case_happy_path`'s positive one both target the same resource, and a
 * destructive endpoint (DELETE, seeded here first and moved after this was
 * found) leaves nothing for the second check to observe once the first has
 * already consumed it -- the same reason buggy-shop's own auth-not-enforced
 * bug (BUG-003) and task-tracker's (BUG-205) are both GETs too.
 */
function adminListNotes(_req, res) {
  send(res, 200, [...NOTES.values()]); // BUG-404: no verifiedClaims() call at all
}

/** BUG-405: a check that runs, but checks the wrong thing.
 *
 * Every other protected route above verifies the signature before trusting
 * anything in the token. This one decodes the claims and checks `role` --
 * but never verifies the signature that claim is supposed to be backed by,
 * so `alg: "none"` and an empty signature segment are accepted exactly as
 * readily as a real one. See `unsafeAdminClaims` above.
 */
function adminAuditLog(req, res) {
  const claims = unsafeAdminClaims(req);
  if (!claims || claims.role !== "admin") {
    return send(res, 401, { detail: "Admin token required" });
  }
  send(res, 200, [{ actor: claims.sub, action: "export", at: "2026-01-01T00:00:00Z" }]);
}

// ------------------------------------------------------------------ contract

const TASK_ID_PARAM = {
  name: "id",
  in: "path",
  required: true,
  schema: { type: "integer" },
};

const BEARER_SECURITY = [{ bearerAuth: [] }];

const OPENAPI_DOCUMENT = {
  openapi: "3.0.0",
  info: {
    title: "Notes API",
    version: "1.0.0",
    description: "Fixture app for QAgent evaluation. Contains known, labelled defects.",
  },
  components: {
    securitySchemes: { bearerAuth: { type: "http", scheme: "bearer" } },
  },
  paths: {
    "/health": { get: { operationId: "health", responses: { 200: {} } } },
    "/notes": {
      get: {
        operationId: "list_notes",
        security: BEARER_SECURITY,
        responses: { 200: {} },
      },
      post: {
        operationId: "create_note",
        security: BEARER_SECURITY,
        requestBody: {
          content: {
            "application/json": {
              schema: {
                type: "object",
                required: ["title"],
                properties: { title: { type: "string" } },
              },
            },
          },
        },
        responses: { 201: {}, 400: {} },
      },
    },
    "/notes/{id}": {
      get: {
        operationId: "get_note",
        security: BEARER_SECURITY,
        parameters: [TASK_ID_PARAM],
        responses: { 200: {}, 400: {}, 404: {} },
      },
      delete: {
        operationId: "delete_note",
        security: BEARER_SECURITY,
        parameters: [TASK_ID_PARAM],
        responses: { 204: {}, 400: {}, 404: {} },
      },
    },
    "/notes/{id}/complete": {
      put: {
        operationId: "complete_note",
        security: BEARER_SECURITY,
        parameters: [TASK_ID_PARAM],
        responses: { 200: {}, 400: {}, 404: {} },
      },
    },
    "/admin/notes": {
      get: {
        operationId: "admin_list_notes",
        security: BEARER_SECURITY,
        responses: { 200: {} },
      },
    },
    "/admin/audit-log": {
      get: {
        operationId: "admin_audit_log",
        security: BEARER_SECURITY,
        responses: { 200: {}, 401: {} },
      },
    },
  },
};

// --------------------------------------------------------------------- router

const ROUTES = [
  { method: "GET", pattern: /^\/health$/, handler: health },
  { method: "GET", pattern: /^\/openapi\.json$/, handler: (_req, res) => send(res, 200, OPENAPI_DOCUMENT) },
  { method: "GET", pattern: /^\/notes$/, handler: listNotes },
  { method: "POST", pattern: /^\/notes$/, handler: createNote },
  { method: "GET", pattern: /^\/notes\/([^/]+)$/, handler: getNote },
  { method: "PUT", pattern: /^\/notes\/([^/]+)\/complete$/, handler: completeNote },
  { method: "DELETE", pattern: /^\/notes\/([^/]+)$/, handler: deleteNote },
  { method: "GET", pattern: /^\/admin\/notes$/, handler: adminListNotes },
  { method: "GET", pattern: /^\/admin\/audit-log$/, handler: adminAuditLog },
];

const server = http.createServer(async (req, res) => {
  const path = (req.url || "/").split("?")[0];
  for (const route of ROUTES) {
    if (route.method !== req.method) continue;
    const match = path.match(route.pattern);
    if (!match) continue;
    try {
      await route.handler(req, res, ...match.slice(1));
    } catch (err) {
      sendServerError(res, err);
    }
    return;
  }
  send(res, 404, { detail: "Not found" });
});

if (require.main === module) {
  server.listen(PORT, "0.0.0.0", () => {
    console.log(`notes-api listening on :${PORT}`);
    console.log(`alice's token (identities.primary in seeded_defects.yaml): ${ALICE_TOKEN}`);
  });
}

module.exports = { server, issueToken, ALICE_TOKEN };
