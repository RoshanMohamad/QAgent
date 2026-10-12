# ADR-0012: GitLab and Bitbucket are added as fixed public hosts, not configurable ones

Status: accepted
Amends: [ADR-0009](ADR-0009-github-checkouts-are-ephemeral-and-host-pinned.md)

## Context

CLAUDE.md §5 names GitHub, GitLab and Bitbucket and says to build GitHub first.
ADR-0009 built GitHub by pinning cloning to a single host, `github.com`, and that
pin is what removes SSRF, command-executing transports and local-file reads in one
rule. Adding the other two providers is mostly mechanical. The real question is
how far the pin may be widened.

The obvious extension is a setting, `QAGENT_ALLOWED_GIT_HOSTS`, so that a
self-hosted GitLab works. But a self-hosted GitLab is, by definition, a host on a
network the API process can reach and the caller may not. If an operator can add
`gitlab.internal`, a typo or a compromised config can also add `169.254.169.254`
or `localhost`. That reopens the exact SSRF hole ADR-0009 closed, and it does so
through configuration, where nobody reviews it as a security change.

## Decision

**The allowlist is three named public hosts in code: `github.com`, `gitlab.com`,
`bitbucket.org`.** It is not read from configuration. Adding a host is a code
change with a test, and it is reviewed as a security change.

Everything else in ADR-0009 holds unchanged for all three: the same argv
hardening, `GIT_TERMINAL_PROMPT=0`, the 0600 credential file, token redaction,
and an ephemeral checkout. Per host, only the following differ, and they live
in one `GitHost` record per host (`modules/provisioning/clone.py`):

| | GitHub | GitLab | Bitbucket |
|---|---|---|---|
| Path | `owner/repo` | `group[/subgroup...]/project` (up to 20 segments) | `workspace/repo` |
| Token username | `x-access-token` | `oauth2` | `x-token-auth` |
| Token fallback | `$GITHUB_TOKEN` | `$GITLAB_TOKEN` | `$BITBUCKET_TOKEN` |

The `owner/repo` shorthand still means GitHub, so no existing call changes
meaning. A GitLab URL that points into the UI (`/-/tree/main`) is refused
rather than trimmed, as is any `.` or `..` segment. The old pattern let `..`
through as a GitHub owner name, and the new parser closes that.

**Webhooks: one receiver per host, and a repository answers only on its own.**
Each host proves a delivery differently:

- **GitHub** signs with HMAC-SHA256 in `X-Hub-Signature-256`.
- **Bitbucket** uses the same HMAC in `X-Hub-Signature`, so GitHub's verifier
  is reused.
- **GitLab does not sign.** It echoes the configured secret in
  `X-Gitlab-Token`, which is compared in constant time.

GitLab's scheme is weaker: anyone who reads the token off the wire can forge
any body. It is still accepted, because it is GitLab's only mechanism and
deliveries arrive over TLS. To contain it, a repository's secret is checked only
at the endpoint for its own `provider`, and every other endpoint returns the
same 404 as for an unconfigured repository. As a result, a GitLab token is
never tried as an HMAC key at the GitHub receiver, and a mismatch reveals
nothing to a caller who has not yet proven the secret.

All three receivers feed one `_queue_push_scans`. It takes a list of push
events, because a single Bitbucket delivery can update several branches. It
queues the change to the default branch and ignores the rest.

## Consequences

- `POST /projects/{id}/connect` and `qagent connect` accept GitLab.com and
  Bitbucket Cloud URLs, and `Repository.provider` records which host was used.
  No migration was needed, because the column already existed with a `github`
  default.
- **Self-hosted GitLab and Bitbucket Data Center are not supported.** Supporting
  them would need an egress design: resolve the host, refuse private and
  link-local addresses, and pin the resolved IP for the clone. That is a
  separate decision.
- Issue sync (`qagent report-issues`) is still GitHub-only. It is a separate
  integration with its own API client, and neither CLAUDE.md §5 nor this ADR
  covers it.
