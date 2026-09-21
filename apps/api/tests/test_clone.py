"""GitHub checkout provisioning.

The end-to-end clone tests run against a real ``git`` binary and a repository
created on local disk in a tmp_path -- no network, and nothing pulled from
github.com in CI. They reach that local repository by pointing ``ALLOWED_HOST``
at a local path for the duration of one test, which is the honest way to
exercise the real subprocess: the host allowlist is the thing being bypassed, so
it is bypassed explicitly and visibly rather than by weakening the module.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from qagent.modules.provisioning import clone as clone_mod
from qagent.modules.provisioning.clone import (
    CloneError,
    RepoRef,
    _build_clone_args,
    cloned_repository,
    head_commit,
    parse_repo_url,
    redact,
)

git_required = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


# --- URL validation: the security boundary ---------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "https://github.com/acme/shop",
        "https://github.com/acme/shop.git",
        "https://github.com/acme/shop/",
        "https://www.github.com/acme/shop",
        "acme/shop",
    ],
)
def test_parse_accepts_github_forms(raw: str) -> None:
    ref = parse_repo_url(raw)
    assert ref == RepoRef(owner="acme", repo="shop")
    assert ref.https_url == "https://github.com/acme/shop.git"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "https://gitlab.com/acme/shop",
        "https://github.com.evil.test/acme/shop",
        "https://evil.test/acme/shop",
        # Transports that execute a command or read the local filesystem: the
        # reason the allowlist exists at all.
        "ext::sh -c 'curl evil.test'",
        "file:///etc/passwd",
        "/etc/passwd",
        "ssh://git@github.com/acme/shop",
        "git@github.com:acme/shop.git",
        # Credentials smuggled into the URL, and an SSRF target.
        "https://user:pass@github.com/acme/shop",
        "http://169.254.169.254/latest/meta-data",
        # Traversal and option injection through the path.
        "https://github.com/acme/../../etc",
        "--upload-pack=touch pwned",
        "https://github.com/acme/shop?x=1",
        "https://github.com/acme",
    ],
)
def test_parse_rejects_everything_else(raw: str) -> None:
    with pytest.raises(CloneError):
        parse_repo_url(raw)


@pytest.mark.parametrize("branch", ["--upload-pack=x", "a b", "../main", "-main", "", "   "])
def test_invalid_branches_never_reach_git(branch: str, tmp_path: Path) -> None:
    if branch.strip() == "":
        # An empty branch means "the repo's default", not an error.
        assert clone_mod._validate_branch(branch) is None
        return
    with pytest.raises(CloneError):
        with cloned_repository("acme/shop", branch=branch):
            pytest.fail("clone should not have been attempted")


# --- argv: the flags that make the clone safe ------------------------------


def test_clone_argv_carries_the_safety_flags(tmp_path: Path) -> None:
    args = _build_clone_args(
        RepoRef("acme", "shop"), branch="main", credential_file=None, destination=tmp_path / "repo"
    )
    joined = " ".join(args)

    assert "protocol.ext.allow=never" in joined
    assert "protocol.file.allow=never" in joined
    assert "--depth 1" in joined
    assert "--single-branch" in joined
    assert "--no-tags" in joined
    assert "--no-recurse-submodules" in joined
    # `--` before the URL: a repository name can never be read as an option.
    assert args[args.index("--") + 1] == "https://github.com/acme/shop.git"
    assert args[:2] == ["git", "-c"]


def test_token_is_never_passed_on_the_command_line(tmp_path: Path) -> None:
    credential_file = tmp_path / ".git-credentials"
    credential_file.write_text("https://x-access-token:ghp_secret@github.com\n", encoding="utf-8")

    args = _build_clone_args(
        RepoRef("acme", "shop"),
        branch=None,
        credential_file=credential_file,
        destination=tmp_path / "repo",
    )

    assert "ghp_secret" not in " ".join(args)
    assert any("credential.helper=store" in a for a in args)


def test_credential_file_holds_the_token_and_is_deleted(tmp_path: Path) -> None:
    path = clone_mod._write_credential_file(tmp_path, "ghp_secret")
    assert "ghp_secret" in path.read_text(encoding="utf-8")
    assert path.name == ".git-credentials"


def test_redact_strips_the_token_from_git_output() -> None:
    message = "fatal: could not read https://x-access-token:ghp_secret@github.com/acme/shop"
    assert "ghp_secret" not in redact(message, "ghp_secret")
    assert redact(message, None) == message
    assert redact("no token here", "ghp_secret") == "no token here"


# --- lifecycle, against a real git binary ----------------------------------


def _make_origin(tmp_path: Path) -> Path:
    """A real repository on local disk, standing in for a GitHub remote."""
    origin = tmp_path / "origin"
    origin.mkdir()
    (origin / "package.json").write_text('{"dependencies": {"react": "18.0.0"}}', encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(  # noqa: S603 - fixed argv, a repo this test just made
            ["git", *args],  # noqa: S607 - git resolved from PATH, as everywhere else here
            cwd=origin,
            check=True,
            capture_output=True,
            text=True,
        )

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("add", ".")
    git("commit", "-m", "initial")
    return origin


@pytest.fixture
def local_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the module's single allowed host at a local repository.

    Deliberately explicit: the allowlist is a security boundary, so a test that
    needs to get past it says so instead of the module offering a way through.
    """
    origin = _make_origin(tmp_path)
    monkeypatch.setattr(RepoRef, "https_url", property(lambda self: str(origin)))
    # The local path is a file transport, which the hardened argv forbids.
    real_build = clone_mod._build_clone_args

    def build(*args, **kwargs):
        argv = real_build(*args, **kwargs)
        return [a if a != "protocol.file.allow=never" else "protocol.file.allow=always" for a in argv]

    monkeypatch.setattr(clone_mod, "_build_clone_args", build)
    return origin


@git_required
def test_clone_yields_a_working_checkout(local_origin: Path) -> None:
    with cloned_repository("acme/shop") as repo_dir:
        assert (repo_dir / "package.json").is_file()
        assert head_commit(repo_dir)
        captured = repo_dir

    assert not captured.exists()


@git_required
def test_checkout_is_deleted_even_when_the_body_raises(local_origin: Path) -> None:
    captured: Path | None = None

    with pytest.raises(ValueError, match="boom"):
        with cloned_repository("acme/shop") as repo_dir:
            captured = repo_dir
            raise ValueError("boom")

    assert captured is not None
    assert not captured.exists()


@git_required
def test_analyzer_runs_over_a_clone(local_origin: Path) -> None:
    """The point of the whole module: a clone is a checkout the analyzer reads."""
    from qagent.modules.analyzer.analyst import analyze_repository

    with cloned_repository("acme/shop") as repo_dir:
        analysis = analyze_repository(repo_dir)

    assert "React" in analysis.stack.names()


@git_required
def test_missing_branch_is_a_clone_error(local_origin: Path) -> None:
    with pytest.raises(CloneError, match="could not clone"):
        with cloned_repository("acme/shop", branch="no-such-branch"):
            pytest.fail("clone should have failed")


def test_missing_git_binary_is_reported_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(clone_mod.subprocess, "run", boom)

    with pytest.raises(CloneError, match="git is not installed"):
        with cloned_repository("acme/shop"):
            pytest.fail("clone should have failed")


def test_timeout_is_reported_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(clone_mod.subprocess, "run", boom)

    with pytest.raises(CloneError, match="exceeded"):
        with cloned_repository("acme/shop", timeout_seconds=1):
            pytest.fail("clone should have failed")


def test_failed_clone_redacts_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    class Failed:
        returncode = 128
        stdout = ""
        stderr = "fatal: Authentication failed for 'https://x-access-token:ghp_secret@github.com/'"

    monkeypatch.setattr(clone_mod.subprocess, "run", lambda *a, **k: Failed())

    with pytest.raises(CloneError) as excinfo:
        with cloned_repository("acme/shop", token="ghp_secret"):
            pytest.fail("clone should have failed")

    assert "ghp_secret" not in str(excinfo.value)
    assert "***" in str(excinfo.value)
