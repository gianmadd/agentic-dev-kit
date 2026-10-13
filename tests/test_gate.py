"""Tests for guards/gate.py — the pre-commit gate.

Most tests drive a real `git commit` in a temporary repository whose
core.hooksPath points at the kit's guards/hooks, so the hook shim, the CLI
and the checks are exercised together. Git is isolated: every git process
gets GIT_CONFIG_GLOBAL under tmp_path and GIT_CONFIG_NOSYSTEM=1.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / "guards"))

import gate  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────


@pytest.fixture
def git_env(tmp_path, monkeypatch) -> dict[str, str]:
    """Process env with git isolated from the user's global/system config."""
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    global_cfg = tmp_path / "global.gitconfig"
    global_cfg.write_text("[init]\n\tdefaultBranch = main\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return dict(os.environ)


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=check,
    )


@pytest.fixture
def repo(tmp_path, git_env) -> Path:
    """A fresh repo with the gate installed and a local identity."""
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "--local", "user.name", "Test")
    git(path, "config", "--local", "user.email", "test@example.com")
    gate.install(path)
    return path


def write_config(repo: Path, body: str) -> None:
    cfg = repo / ".workflow" / "gate.toml"
    cfg.parent.mkdir(exist_ok=True)
    cfg.write_text(textwrap.dedent(body))


def runner_config(repo: Path, script: str) -> None:
    """Configure [skips] to run a fake test runner that receives the JUnit path."""
    (repo / "runner.py").write_text(textwrap.dedent(script))
    write_config(repo, f"""\
        [skips]
        command = ["{sys.executable}", "runner.py"]
        junit_option = "{{path}}"
        """)


def junit_runner(xml: str, console: str = "", exit_code: int = 0) -> str:
    """Source of a fake runner writing ``xml`` to argv[1] and printing ``console``."""
    return f"""\
        import sys
        open(sys.argv[1], "w").write({xml!r})
        print({console!r})
        sys.exit({exit_code})
        """


def commit(repo: Path, message: str = "change") -> subprocess.CompletedProcess:
    (repo / "file.txt").write_text(message)
    git(repo, "add", "file.txt")
    return git(repo, "commit", "-q", "-m", message, check=False)


def committed_report(repo: Path) -> str:
    return git(repo, "show", f"HEAD:{gate.REPORT_PATH}").stdout


# ── pre-commit, end to end ───────────────────────────────────────────


def test_commit_with_one_passing_and_one_skipped_pytest_test_carries_the_skip_in_its_tree(repo):
    (repo / "test_sample.py").write_text(textwrap.dedent("""\
        import pytest

        def test_passes():
            assert True

        @pytest.mark.skip(reason="needs network")
        def test_offline():
            pass
        """))
    write_config(repo, f"""\
        [skips]
        command = ["{sys.executable}", "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_sample.py"]
        """)

    result = commit(repo)

    assert result.returncode == 0, result.stderr
    report = committed_report(repo)
    assert "`test_sample::test_offline`" in report
    assert "needs network" in report
    assert "test_passes" not in report
