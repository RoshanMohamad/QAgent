# ADR-0010: Infrastructure is validated, not proven — and the microservice split stays unbuilt

Status: accepted
Supersedes in part: [ADR-0008](ADR-0008-phase-6-scope.md)

## Context

ADR-0008 deferred Kubernetes manifests, autoscaling and payment integration on
the grounds that "untested infra code is a liability, not an asset", and the
README separately rejected reference image 03's twelve microservices. Those were
the right calls at the time and the reasoning has not changed.

They were then asked for anyway, which is a legitimate thing for the owner of a
project to ask. This records what was built, what was deliberately not, and how
to tell which claims here are load-bearing.

## Decision

### 1. Manifests are written and *validated*, never described as proven

`deploy/kubernetes/` is checked by `kubeconform` in **strict** mode against the
real Kubernetes OpenAPI schemas, in CI, both as raw files and as the built
kustomization — because a kustomization can emit something no individual file
contained. Strict mode matters specifically: it rejects unknown fields, which is
the typo class that otherwise survives review and surfaces as a silently ignored
setting in production.

Nothing here has run in a cluster under load. Every number that genuinely needs
one — replica counts, HPA thresholds, requests and limits, PDB budgets — is
marked `# TUNE:` and carries the reasoning behind the starting value instead of
a figure presented as if it were measured. A manifest that looks authoritative
about numbers nobody measured is exactly the liability ADR-0008 warned about;
one that is structurally correct and honest about its guesses is not.

Two properties are asserted by CI rather than trusted, because both are
load-bearing security decisions carried over from `docker-compose.yml`:

- **The admin database credential reaches only the migration Job.** A superuser
  bypasses RLS unconditionally ([ADR-0007](ADR-0007-rls-requires-an-unprivileged-role.md)),
  and the worker fetches user-supplied URLs. CI parses the built manifests and
  fails if `ADMIN_DATABASE_URL` appears on any other workload. That check was
  tested by injecting a violation and confirming it failed.
- **The placeholder secret can never be applied.** `secrets.example.yaml`
  documents the required shape and contains `CHANGE_ME`; it is deliberately
  absent from `kustomization.yaml`, and CI greps the built output to keep it so.

### 2. Ephemeral environments are compose, not Terraform

Reference image 02 draws a per-PR cloud environment (Terraform/EKS/Pulumi).
`.github/workflows/ephemeral-environment.yml` implements the *shape* — provision,
migrate, deploy, gate, comment, destroy — on the runner with docker compose.

The property that actually matters is that a pull request is tested against a
real, isolated, freshly-migrated deployment of itself, and compose delivers it.
What cloud provisioning would add is a public URL and realistic infrastructure;
what it costs is a cloud account, a cost owner, and a teardown guarantee that
survives a cancelled job. An orphaned EKS cluster is a bill, not a bug. The
provision and destroy steps are two steps; swapping them for Terraform later
changes those two and nothing else.

### 3. Billing prices, and stops short of settling

`modules/billing/statement.py` turns metered usage into line items, a subtotal
and a total. It does not talk to a payment provider, hold a card, handle tax or
choose a currency.

That line is where ADR-0008 already said the seam should be cut, and the reason
holds: a Stripe integration written against a pricing page that does not exist
would *look* finished, and the first real pricing decision would discard it.
Every rate defaults to zero, so an unconfigured deployment produces a statement
with no amounts on it rather than quietly billing something nobody decided.

Money is `Decimal` throughout. A float subtotal is how an invoice ends up a cent
off from its own line items, and the first person to notice is a customer.

### 4. The twelve-microservice split stays unbuilt

This is the one Tier-3 item deliberately **not** delivered, and it is worth
being explicit rather than quietly skipping it.

Splitting a working single service into twelve would not add a capability. It
would add eleven deployment units, eleven failure modes, a network hop on every
call that is currently a function call, distributed transactions where there is
currently one, and a tracing requirement to debug what a stack trace answers
today. The README has said this since before Phase 6 and the argument has not
weakened: the module boundaries reference image 03 draws are real and enforced
in `modules/`, and the seams are where they would be cut.

What *was* built instead is the part that carries the actual benefit: independent
horizontal scaling where the workload genuinely differs. `qagent.scan` and
`qagent.performance` are separate queues with separate Deployments and separate
autoscaling, because a load test and an API check have nothing in common
operationally. That is reference image 06's fan-out, expressed as queue routing
rather than as a service mesh — a second worker pool is a manifest, not a
rewrite.

If a single component ever needs to scale or deploy independently of the rest,
splitting *that one* is a contained change. Splitting all twelve in advance,
against no load, is building on a guess — which is what
[ADR-0001](ADR-0001-scope-and-input-contract.md) exists to refuse.

## Consequences

- The manifests are a correct starting point that still needs an SRE. The README
  and their own README say so; nobody should read them as a production
  configuration.
- CI now fails on a manifest that is structurally invalid, leaks the placeholder
  secret, or widens the admin credential's blast radius. It cannot fail on a
  badly-sized HPA, and does not pretend to.
- An operator wanting real billing wires a payment provider against
  `GET /api/v1/billing/statement`. The metering is done, the pricing is done, the
  settlement is theirs.
- The single-service architecture is now a recorded decision with a stated
  trigger for revisiting it, rather than an unexamined default.
