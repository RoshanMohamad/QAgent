"""Repository checkout provisioning (CLAUDE.md section 6: "clones the repository
into an isolated environment"; section 5: GitHub, GitLab and Bitbucket).

ADR-0006 settled what the analyzer may *do* with a checkout - read text, never
execute it. It assumed the checkout was already on local disk, which is why
``POST /projects/{id}/analyze`` takes a local ``repo_path``. This module closes
that gap: it is the thing that produces the checkout in the first place, so
"connect a GitHub repository" stops meaning "clone it yourself first".

It owns lifecycle only, exactly like ``compose.py`` next door: clone on enter,
delete on exit, success or failure. It knows nothing about stack detection or
module trees - it hands ``analyze_repository`` a directory and gets out of the
way, so there is one analyzer, not a second code path for remote repositories.

Cloning is the first moment untrusted third-party content touches the process
(ADR-0004), and it happens *before* any sandbox exists (ADR-0006). So the clone
itself is treated as hostile input rather than a convenience:

- **Host allowlist.** Only ``github.com``, ``gitlab.com`` and
  ``bitbucket.org`` - a fixed list, never a configurable one. This is not
  politeness about scope - ``git clone`` accepts transports that execute
  commands outright (``ext::``), read the local filesystem (``file://``), and
  reach internal hosts the API process can see but the caller should not
  (SSRF). Refusing everything that is not an https URL on a known public host
  removes all three at once. A self-hosted GitLab is exactly such an internal
  host, which is why it is not on the list (ADR-0012).
- **Transports disabled explicitly.** ``protocol.ext.allow=never`` and
  ``protocol.file.allow=never`` hold even if a redirect or a submodule tries to
  reintroduce them behind the URL check.
- **No submodules, no tags, depth 1, single branch.** The analyzer reads the
  working tree; history and submodule payloads are cost and attack surface with
  no reader.
- **No credential prompt.** ``GIT_TERMINAL_PROMPT=0`` turns a private repo
  without a usable token into a fast, clear failure instead of a hung process
  waiting on a tty nobody is attached to.
- **Token never in argv.** A token passed on the command line is readable by
  every other process on the host via ``ps``. It goes into a 0600 credential
  file git is pointed at for the duration of the clone and which is deleted in
  a ``finally``; the remote URL git records stays credential-free.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GitHost:
    """One host this module will clone from, and how it takes a token."""

    provider: str
    host: str
    #: The username a token is presented under in the credential file. Each
    #: host documents its own: GitHub accepts ``x-access-token`` for PATs and
    #: app tokens, GitLab ``oauth2`` for personal/project/group tokens, and
    #: Bitbucket ``x-token-auth`` for repository/workspace access tokens.
    username: str
    #: Where a token comes from when the caller does not pass one.
    token_env: str
    #: Most path segments a repository path may have. GitLab nests groups
    #: (``group/subgroup/project``); GitHub and Bitbucket are ``owner/repo``.
    max_segments: int


# The hosts this module will clone from. See the module docstring: this is a
# security boundary, not a scoping preference, and it is deliberately not read
# from configuration.
ALLOWED_HOSTS: dict[str, GitHost] = {
    "github.com": GitHost("github", "github.com", "x-access-token", "GITHUB_TOKEN", 2),
    "gitlab.com": GitHost("gitlab", "gitlab.com", "oauth2", "GITLAB_TOKEN", 20),
    "bitbucket.org": GitHost("bitbucket", "bitbucket.org", "x-token-auth", "BITBUCKET_TOKEN", 2),
}
GITHUB = ALLOWED_HOSTS["github.com"]

# One path segment, as all three hosts constrain them: alphanumerics, dot,
# dash, underscore. Anything else is not a repository path we are willing to
# build a URL out of.
_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
_SHORTHAND = re.compile(r"^([A-Za-z0-9._-]+)/([A-Za-z0-9._-]*?)(?:\.git)?/?$")
_REPO_URL = re.compile(r"^https://(?:www\.)?([a-z0-9.-]+)/(.*?)(?:\.git)?/?$")

# A branch name we are willing to pass to --branch. Git's own rules are wider,
# but everything excluded here (leading dash, whitespace, ``..``) exists to stop
# a branch name being read as an option or a traversal.
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")


class CloneError(RuntimeError):
    """The repository could not be cloned: bad URL, bad token, missing branch,
    private repo, network failure, or a clone that outran its timeout."""


@dataclass(frozen=True)
class RepoRef:
    """A validated repository reference.

    Built only by :func:`parse_repo_url`, so holding one is proof the host
    allowlist has already been applied - no caller can construct a clone target
    that skipped validation. ``owner`` is everything before the repository
    name, so a GitLab subgroup path lives there whole (``group/subgroup``).
    """

    owner: str
    repo: str
    host: GitHost = GITHUB

    @property
    def provider(self) -> str:
        return self.host.provider

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def https_url(self) -> str:
        return f"https://{self.host.host}/{self.slug}.git"


def parse_repo_url(raw: str) -> RepoRef:
    """Validate ``raw`` and return the repository it names.

    Accepts a full ``https://`` URL on an allowed host (with or without
    ``.git``) or the ``owner/repo`` shorthand, which means GitHub - the one
    host that has always been accepted. Everything else - other hosts,
    ``git@``/``ssh://``/``file://``/``ext::`` transports, URLs carrying
    credentials or a query string, ``.``/``..`` segments - is refused rather
    than normalised, because the point of this function is to be the only way
    in.
    """
    candidate = (raw or "").strip()
    if not candidate:
        raise CloneError("no repository URL given")

    shorthand = _SHORTHAND.match(candidate)
    if shorthand is not None:
        host, segments = GITHUB, [shorthand.group(1), shorthand.group(2)]
    else:
        match = _REPO_URL.match(candidate)
        host = ALLOWED_HOSTS.get(match.group(1)) if match else None
        if match is None or host is None:
            allowed = ", ".join(f"https://{h}/owner/repo" for h in ALLOWED_HOSTS)
            raise CloneError(
                f"not a supported repository URL: {candidate!r}. "
                f"Expected {allowed}, or owner/repo for GitHub."
            )
        segments = match.group(2).split("/")

    if not segments[-1]:
        raise CloneError(f"missing repository name in {candidate!r}")
    if not 2 <= len(segments) <= host.max_segments:
        raise CloneError(f"not a {host.host} repository path: {candidate!r}")
    for segment in segments:
        # `-` is GitLab's separator before UI routes (`/-/tree/main`), and a
        # segment of only dots is traversal; both match the character class.
        if not _SEGMENT.match(segment) or segment == "-" or set(segment) == {"."}:
            raise CloneError(f"invalid path segment {segment!r} in {candidate!r}")
    return RepoRef(owner="/".join(segments[:-1]), repo=segments[-1], host=host)


def _validate_branch(branch: str | None) -> str | None:
    if branch is None:
        return None
    branch = branch.strip()
    if not branch:
        return None
    if ".." in branch or not _BRANCH.match(branch):
        raise CloneError(f"invalid branch name: {branch!r}")
    return branch


def redact(text: str, token: str | None) -> str:
    """Remove ``token`` from text on its way to a log line or an HTTP response.

    Git echoes the remote URL in most of its failure messages, and those
    messages are surfaced to the caller verbatim. One accidental interpolation
    is all it takes to hand a token to whoever reads the logs.
    """
    if not token:
        return text
    return text.replace(token, "***")


def _build_clone_args(
    ref: RepoRef, *, branch: str | None, credential_file: Path | None, destination: Path
) -> list[str]:
    """The exact argv used for the clone. Split out so the flags that make this
    safe are asserted directly by the tests rather than inferred from behaviour.
    """
    args = [
        "git",
        # Defense in depth behind parse_repo_url: even a redirect or a stray
        # submodule cannot reach a command-executing or filesystem transport.
        "-c",
        "protocol.ext.allow=never",
        "-c",
        "protocol.file.allow=never",
        # Ignore any helper the host has configured; the only credential this
        # clone may use is the one we just wrote, if any.
        "-c",
        "credential.helper=",
    ]
    if credential_file is not None:
        args += ["-c", f"credential.helper=store --file={credential_file.as_posix()}"]

    args += ["clone", "--depth", "1", "--single-branch", "--no-tags", "--no-recurse-submodules"]
    if branch:
        args += ["--branch", branch]
    args += ["--", ref.https_url, str(destination)]
    return args


def _write_credential_file(directory: Path, token: str, host: GitHost = GITHUB) -> Path:
    """Write a git credential-store file readable only by this user.

    The username is the host's own convention for token auth (see
    ``GitHost.username``); the token is the password either way.
    """
    path = directory / ".git-credentials"
    path.write_text(
        f"https://{host.username}:{token}@{host.host}\n",
        encoding="utf-8",
    )
    # Best effort: chmod is a no-op for practical purposes on Windows, but the
    # file lives in a per-run temp directory that is deleted either way.
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - platform dependent
        logger.debug("could not restrict permissions on credential file")
    return path


def _force_remove(path: Path) -> None:
    """Delete a checkout, including the read-only files git leaves under
    ``.git/objects`` that make ``shutil.rmtree`` fail on Windows."""

    def _on_error(func, target, _exc_info) -> None:
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:  # pragma: no cover - last resort
            logger.warning("could not remove %s", target)

    shutil.rmtree(path, onerror=_on_error)


@contextmanager
def cloned_repository(
    repo_url: str,
    *,
    branch: str | None = None,
    token: str | None = None,
    timeout_seconds: float = 300.0,
) -> Iterator[Path]:
    """Clone a repository to a temporary directory and yield its path.

    The checkout is deleted on exit whether the body succeeded or raised - the
    same unconditional teardown ``ComposeStack`` gives a compose stack. Nothing
    inside the checkout is ever executed (ADR-0006); the caller is expected to
    read text out of it and nothing more.
    """
    ref = parse_repo_url(repo_url)
    checked_branch = _validate_branch(branch)

    workspace = Path(tempfile.mkdtemp(prefix="qagent-clone-"))
    destination = workspace / "repo"
    credential_file: Path | None = None

    try:
        if token:
            credential_file = _write_credential_file(workspace, token, ref.host)

        args = _build_clone_args(
            ref, branch=checked_branch, credential_file=credential_file, destination=destination
        )
        logger.info("cloning %s (branch=%s)", ref.slug, checked_branch or "default")

        env = {
            **os.environ,
            # No tty here: without this a private repo hangs on a username
            # prompt until the timeout instead of failing immediately.
            "GIT_TERMINAL_PROMPT": "0",
            # Same reasoning for the GUI/SSH prompts git may otherwise spawn.
            "GIT_ASKPASS": "",
            "SSH_ASKPASS": "",
        }

        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, validated inputs, no shell
                args,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                env=env,
            )
        except FileNotFoundError as exc:
            raise CloneError("git is not installed or not on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise CloneError(
                f"clone of {ref.slug} exceeded {timeout_seconds:.0f}s"
            ) from exc

        if completed.returncode != 0:
            detail = redact((completed.stderr or completed.stdout or "").strip(), token)
            raise CloneError(f"could not clone {ref.slug}: {detail}")

        # The credential file has done its job; drop it before the caller gets
        # anywhere near the workspace.
        if credential_file is not None and credential_file.exists():
            credential_file.unlink()
            credential_file = None

        yield destination
    finally:
        if credential_file is not None and credential_file.exists():
            credential_file.unlink(missing_ok=True)
        if workspace.exists():
            _force_remove(workspace)


def head_commit(repo_dir: Path) -> str | None:
    """The SHA the checkout landed on, for recording what was analyzed.

    Reads ``git rev-parse``; a failure here is not worth failing a connection
    over, so it degrades to ``None``.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - git resolved from PATH by design
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - defensive
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None
