# Kubernetes deployment

[ADR-0008](../../docs/decisions/ADR-0008-phase-6-scope.md) deferred these on the
grounds that untested infrastructure code is a liability, not an asset. That
reasoning has not changed, so read this before applying anything:

**These manifests are validated, not proven.** Every file here is checked by
`kubeconform` against the real Kubernetes OpenAPI schemas in CI, so they are
structurally correct and will be accepted by an API server. Nothing here has
run in a cluster under load. The parts that genuinely need a real deployment to
get right — replica counts, HPA thresholds, resource requests and limits, PDB
budgets — are marked `# TUNE:` and carry the reasoning behind the starting
value rather than a number presented as if it were measured.

Treat them as a correct starting point that still needs an SRE, not as a
production configuration.

## What is here

| File | Purpose |
|---|---|
| `namespace.yaml` | Namespace, and a default-deny `NetworkPolicy` |
| `secrets.example.yaml` | The shape of the secrets required — never real values |
| `configmap.yaml` | Non-secret configuration |
| `migration-job.yaml` | `db_init` as a `Job`, run before a rollout |
| `api.yaml` | API `Deployment`, `Service`, `HPA`, `PodDisruptionBudget` |
| `worker.yaml` | Scan and performance worker `Deployment`s, and their HPAs |
| `kustomization.yaml` | Ties them together |

## Applying

```bash
kubectl apply -k deploy/kubernetes/base
```

The migration `Job` must complete before the API rolls out — it creates the
schema, the unprivileged application role and the RLS policies
([ADR-0007](../../docs/decisions/ADR-0007-rls-requires-an-unprivileged-role.md)).
It is the only workload that receives the admin DSN.

## Two security properties carried over from `docker-compose.yml`

These are not Kubernetes idioms for their own sake; they mirror decisions that
are already load-bearing in this project.

1. **The worker never gets the admin database credential.** It reaches out to
   arbitrary third-party targets, so a compromise must not yield Postgres
   superuser access. Only the migration Job mounts `admin-database-url`.
2. **Egress is default-deny.** The worker fetches user-supplied URLs; the
   application-level SSRF guard is the first line, and a `NetworkPolicy` is the
   second. The policy here allows DNS, Postgres and Redis, and expects an
   operator to add the egress rule matching their own target environments.
