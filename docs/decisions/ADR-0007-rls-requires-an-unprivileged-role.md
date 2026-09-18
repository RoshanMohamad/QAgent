# ADR-0007: Row-level security requires a role that isn't a superuser

**Status:** Accepted
**Date:** 2026-09-18

## Context

The README's own Status section flagged this honestly before it was checked: "the
Postgres-backed paths - schema creation, RLS policies, the worker and persistence -
import cleanly [...] but confirming them needs a working Docker daemon." With Docker
available, this ADR is that confirmation, and it did not confirm what the code assumed.

`db_init.py` created the schema and applied `ENABLE ROW LEVEL SECURITY` /
`FORCE ROW LEVEL SECURITY` policies (models.py's `TENANT_TABLES`) using the exact same
connection - `DATABASE_URL` - that the API and worker use at runtime. In
`docker-compose.yml` that connection authenticates as `POSTGRES_USER`, the role the
official `postgres` Docker image creates during `initdb`.

That role is always a Postgres superuser. Reproduced live:

```
SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'qagent';
 rolname | rolsuper | rolbypassrls
---------+----------+--------------
 qagent  | t        | t
```

Postgres superusers bypass row-level security **unconditionally**, and `FORCE ROW
LEVEL SECURITY` does not change that - `FORCE` only extends RLS to a policy's
non-superuser *owner*, who is otherwise exempt by default. With the app connecting as
a superuser, every policy `apply_rls` installed was a no-op. Reproduced live, before
the fix, using the app's own `set_tenant` helper exactly as `current_principal`
(main.py) calls it on every request:

```
inserted project for org A: 0c238ddc-...
org A sees 1 project(s) while bound to org A
org B sees 1 project(s) while bound to org B (should be 0)      <- leak
CROSS-TENANT WRITE SUCCEEDED (BAD)                                <- leak
no tenant set: sees 2 project(s) (should be 0)                   <- leak
```

Every tenant could read and write every other tenant's rows. The `USING`/`WITH CHECK`
clauses, the `FORCE` keyword, the `org_id` index on every table - all correct, and all
irrelevant, because the role evaluating them was never subject to them in the first
place. This is the single most consequential thing this codebase's own "Verified"
section had not actually verified.

## Decision

The API and worker must never connect to Postgres as a role that can bypass RLS.
Concretely:

1. **Two roles, two DSNs.** `ADMIN_DATABASE_URL` (a real superuser - bootstrap only)
   creates the schema, and `DATABASE_URL` (a separate, unprivileged role) is what the
   API and worker actually connect as at request/task time. `db_init.py` now takes an
   explicit `admin` `Engine` parameter for every bootstrap operation and never reads
   `DATABASE_URL` to do DDL.

2. **The app role is created by db_init itself**, from `DATABASE_URL`'s own
   username/password (`ensure_app_role`) - not a second, independently-configured
   identity that could drift from what the app actually connects as. It is
   idempotently stripped of `SUPERUSER`, `BYPASSRLS`, `CREATEDB` and `CREATEROLE` on
   every run, not just on first creation, so a manual `ALTER ROLE` on a long-lived
   database can't quietly widen it back out without the next deploy narrowing it again.

3. **The app role owns no tables.** `create_schema` runs on the admin connection, so
   table ownership stays there; the app role receives only `SELECT/INSERT/UPDATE/DELETE`
   grants. This matters independently of point 2: `FORCE` governs an *owner's*
   exemption specifically, so a role that owns nothing has one fewer way to have ever
   been exempt, regardless of what else is or isn't set on it.

4. **The admin DSN never reaches the worker.** `docker-compose.yml` loads
   `.env.admin` only into the `api` service (whose startup command runs `db_init`);
   `worker` loads only `.env`. The worker makes outbound requests to arbitrary,
   third-party targets - it is the untrusted-facing half of this system (README
   Security section: no exposed ports, no Docker socket, read-only filesystem) - so it
   must not hold Postgres superuser credentials even unused. `env_file:` accepts a list,
   which is what makes this a one-line difference between the two services rather than
   a second compose file.

5. **A role name that can't be proven safe is refused, not trusted.** The role to
   create is parsed out of `DATABASE_URL`, and DDL has no parameter placeholder for an
   identifier or a role-creation password literal. Both are validated/escaped in
   `ensure_app_role` before they reach a SQL string, the same discipline already used
   for `TENANT_TABLES` in `_POLICY.format(table=table)`.

Reproduced live, after the fix, same script, connecting as `qagent_app`:

```
org A sees 1 project(s) in its own transaction (expect 1)
org B sees 0 project(s) while bound to org B (expect 0)
cross-tenant write correctly rejected: ProgrammingError
no tenant set: query failed closed: DataError
```

And through the real HTTP surface, as a second registered organization with a
perfectly valid token for its own tenant:

```
GET /api/v1/projects           -> 200 []
GET /api/v1/projects/{org-a's project}/bugs  -> 200 []
```

`tests_integration/test_rls_isolation.py` encodes exactly this as a regression test,
including `test_app_role_is_not_privileged` - the direct check for the condition that
caused the leak - and `test_table_owner_is_not_the_app_role` for point 3.
`tests_integration/test_worker_task.py` proves the same isolation, plus the full
distributed path (`POST /runs` -> Redis -> a real, separate `celery worker` process ->
Postgres -> `GET /runs/{id}`), end to end.

## Consequences

- Anyone deploying this outside `docker-compose.yml`'s exact shape must configure two
  connection strings, not one. `db_init.py` degrades to the old, unsafe, single-role
  behavior when `ADMIN_DATABASE_URL` is unset - loudly, with a warning that names the
  exact failure mode this ADR reproduced - rather than refusing to start, because a
  first `docker compose up` with no admin file yet configured should still work for
  someone kicking the tyres, just not silently securely.
- `TENANT_TABLES`/RLS policies are now provably load-bearing rather than merely
  present, closing the exact gap the README's "Implemented but not yet exercised end
  to end" line named.
- Alembic migrations (when they arrive) must run on the admin connection, matching
  `create_schema`'s existing pattern - `ALTER DEFAULT PRIVILEGES` in `ensure_app_role`
  means a table a migration creates is automatically grantable to the app role without
  a manual follow-up step, but only if the migration itself runs as admin.
