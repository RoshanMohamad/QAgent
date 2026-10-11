# ADR-0011: Compose mode runs sandboxed — refuse host access, constrain the rest

Status: accepted
Amends: [ADR-0001](ADR-0001-scope-and-input-contract.md)

## Context

ADR-0001 added `compose` mode: QAgent runs a repository's own `docker-compose.yml`.
Until now it ran that file exactly as written. The file is third-party input, and
it controls its own privileges: `privileged: true`, a bind mount of
`/var/run/docker.sock` (root on the host), `network_mode: host`, or a database
published on `0.0.0.0` were all passed straight to the daemon. CLAUDE.md §22 asks
for isolated containers with CPU, memory and network limits, read-only
filesystems where possible, and secret isolation. None of that was in place.

## Decision

`ComposeStack.up` now runs two steps before `docker compose up`
(`modules/provisioning/sandbox.py`):

1. **Refuse.** Resolve the configuration with `docker compose config --format
   json`, so `extends`, `include` and variable interpolation are already applied,
   and reject:
   - privileged mode
   - host network, pid, ipc, uts, cgroup or user namespaces, and `network_mode:
     container:`
   - any `cap_add`
   - host devices
   - an `unconfined` seccomp or AppArmor profile
   - a mounted Docker socket
   - any bind mount outside the repository

   Every violation is listed at once, and nothing is built or started.
   These are refused rather than overridden, because an override can't take a bind
   mount away. A stack that needs the Docker socket can't be run safely at all.

2. **Constrain.** Write an override file to a temp directory (never into the
   repository, so a hostile repository can't plant one of its own) and pass it
   with a second `-f`. For every service it sets:
   - `cpus`, `mem_limit`, and `memswap_limit` equal to `mem_limit` (no swap)
   - `pids_limit`
   - `cap_drop: ALL`, plus only the capabilities image entrypoints need to hand
     their data directory to their service user and switch to it (`CHOWN`,
     `DAC_OVERRIDE`, `FOWNER`, `SETGID`, `SETUID`, `NET_BIND_SERVICE`)
   - `no-new-privileges`
   - a read-only root filesystem, with tmpfs at `/tmp`, `/run`, `/var/run` and
     `/var/cache`

   It also removes every published port except the target's, which is bound to
   `127.0.0.1` only. Every service joins an `internal: true` network.

The override uses Compose's `!override` tag so it *replaces* the repository's
`ports`, `networks`, `cap_add` and `security_opt` instead of merging with them.
That tag needs Compose 2.24.4 or later. On an older Compose the override would
quietly merge and the sandbox would fail open, so older versions are refused, not
tolerated.

`scan-repo` exposes `--cpus`, `--memory` and `--writable-rootfs`. There is no flag
to turn the sandbox off. `ComposeConfig(sandbox=None)` exists for tests and for a
trusted local stack, and it logs a warning.

## What this does not do

- **The target keeps egress.** Docker doesn't forward published ports into an
  `internal` network, so the target service also joins an ordinary bridge network
  that the host can reach. Its sidecars (database, cache, workers) have no route
  out; the target does. Closing that route needs host firewall rules or an egress
  proxy, which belong to the operator's infrastructure, not to a compose file.
- **The build still has network access.** `--build` runs the repository's
  Dockerfile, which normally needs a package registry. The build runs under
  BuildKit's own isolation, not these limits.
- **Docker is still not a security boundary against actively hostile code.** A
  kernel exploit escapes all of this. The README's gVisor/Firecracker note still
  applies. This ADR removes the ways a compose file could *ask* for host access,
  and caps what a well-meaning but buggy or greedy stack can use. It does not
  contain a determined attacker.

## Verification

`tests/test_sandbox.py` covers each refusal and every field of the override.
`tests/test_compose.py` checks that a refused file never reaches `up`, that the
override is layered and then deleted on `down`, and that an old Compose is
refused. It was also run against a real Docker 29.8 / Compose 5.5 daemon, where
`docker inspect` showed:

- the limits, capabilities, read-only root and tmpfs mounts as set above
- the target published on `127.0.0.1` only, and the sidecar with no ports
- the sidecar on the internal network only, with its egress blocked
- writes to the root filesystem failing, while `/tmp` stayed writable
- containers and networks removed afterwards

A compose file with `privileged`, the Docker socket, a parent-directory bind
mount, `network_mode: host`, `pid: host` and `cap_add` was refused with all six
listed and nothing started. That machine had no outbound network at all, so its
"target egress blocked" result proves nothing about the target. The known gap
above is from Docker's documented behaviour, not from this test.
