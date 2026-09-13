"""Environment provisioning: bringing a target up before the pipeline can run.

``target_url`` mode (the MVP default) needs nothing here — the app is already
running. This package holds the additive path from ADR-0001: ``compose`` mode,
which owns the lifecycle of a repository's own ``docker-compose.yml`` stack.
"""
