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


TWO_SKIPS_UNSORTED = """<testsuites><testsuite name="s">
<testcase classname="pkg.zeta" name="test_late"><skipped message="slow"/></testcase>
<testcase classname="pkg.alpha" name="test_ok"/>
<testcase classname="pkg.alpha" name="test_early"><skipped message="flaky on CI"/></testcase>
</testsuite></testsuites>"""


def test_skips_are_read_from_the_junit_file_not_from_console_output(repo):
    runner_config(repo, junit_runner(
        TWO_SKIPS_UNSORTED,
        console="SKIPPED pkg.ghost::test_console_only - printed, not in JUnit",
    ))

    assert commit(repo).returncode == 0
    report = committed_report(repo)
    assert "`pkg.zeta::test_late` — slow" in report
    assert "`pkg.alpha::test_early` — flaky on CI" in report
    assert "ghost" not in report


def test_report_lists_skips_sorted_by_test_id(repo):
    runner_config(repo, junit_runner(TWO_SKIPS_UNSORTED))

    assert commit(repo).returncode == 0
    report = committed_report(repo)
    assert report.index("pkg.alpha::test_early") < report.index("pkg.zeta::test_late")


def test_report_says_none_when_no_test_is_skipped(repo):
    runner_config(repo, junit_runner(
        '<testsuite><testcase classname="pkg.alpha" name="test_ok"/></testsuite>'))

    assert commit(repo).returncode == 0
    section = committed_report(repo).split("## Skipped tests", 1)[1]
    assert section.strip() == "none"


def test_report_says_not_configured_when_the_skips_section_is_absent(repo):
    write_config(repo, "# no checks configured yet\n")

    assert commit(repo).returncode == 0
    section = committed_report(repo).split("## Skipped tests", 1)[1]
    assert section.strip() == "not configured"


def test_failing_tests_with_a_valid_junit_do_not_block_the_commit(repo):
    runner_config(repo, junit_runner(
        '<testsuite><testcase classname="pkg.alpha" name="test_broken">'
        '<failure message="assert 1 == 2"/></testcase>'
        '<testcase classname="pkg.alpha" name="test_later"><skipped message="todo"/></testcase>'
        '</testsuite>',
        exit_code=1,
    ))

    result = commit(repo)

    assert result.returncode == 0, result.stderr
    assert "`pkg.alpha::test_later` — todo" in committed_report(repo)


def test_test_command_runs_without_the_git_variables_the_hook_inherits(repo):
    # git exports GIT_INDEX_FILE (and friends) to hooks; a test suite that
    # runs git would otherwise act on the commit's index. The runner records
    # every GIT_* variable it sees as a skip reason.
    runner_config(repo, """\
        import os, sys
        leaked = " ".join(sorted(k for k in os.environ if k.startswith("GIT_")))
        open(sys.argv[1], "w").write(
            '<testsuite><testcase classname="env" name="probe">'
            f'<skipped message="leaked: {leaked}"/></testcase></testsuite>')
        """)

    assert commit(repo).returncode == 0
    assert "`env::probe` — leaked:\n" in committed_report(repo)


def test_clean_env_drops_every_git_variable_and_keeps_the_rest():
    env = {
        "GIT_DIR": "/r/.git",
        "GIT_INDEX_FILE": "/r/.git/index",
        "GIT_CONFIG_GLOBAL": "/x",
        "GITHUB_TOKEN": "kept",
        "PATH": "/usr/bin",
    }

    assert gate.clean_env(env) == {"GITHUB_TOKEN": "kept", "PATH": "/usr/bin"}


# ── blocking: the configured check cannot run ────────────────────────


def assert_blocked(repo: Path, result: subprocess.CompletedProcess, reason: str) -> None:
    assert result.returncode != 0
    assert "Traceback" not in result.stderr  # a reason, not a crash
    blocked = [line for line in result.stderr.splitlines() if line.startswith("gate: blocked:")]
    assert blocked and reason in blocked[0], result.stderr
    assert git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode != 0


def test_commit_is_blocked_when_the_test_command_cannot_start(repo):
    write_config(repo, """\
        [skips]
        command = ["no-such-test-runner-xyz", "-q"]
        """)

    assert_blocked(repo, commit(repo), "no-such-test-runner-xyz")


def test_commit_is_blocked_when_the_test_command_writes_no_junit(repo):
    runner_config(repo, "print('SKIPPED pkg::test_x - console only')\n")

    assert_blocked(repo, commit(repo), "no JUnit report")


def test_commit_is_blocked_when_the_junit_report_is_not_parsable(repo):
    runner_config(repo, junit_runner("<testsuite><testcase"))

    assert_blocked(repo, commit(repo), "JUnit")


def test_commit_is_blocked_when_the_config_file_is_missing(repo):
    assert_blocked(repo, commit(repo), gate.CONFIG_PATH)


def test_commit_is_blocked_when_the_config_is_not_valid_toml(repo):
    write_config(repo, "[skips\ncommand = 1\n")

    assert_blocked(repo, commit(repo), "invalid TOML")


@pytest.mark.parametrize("body, reason", [
    ('[skips]\ncommand = "pytest -q"\n', "skips.command"),
    ('[skips]\ncommand = []\n', "skips.command"),
    ('[skips]\ncommand = ["pytest", 1]\n', "skips.command"),
    ('[skips]\ncommand = ["pytest"]\njunit_option = 3\n', "skips.junit_option"),
    ('[skips]\ncommand = ["pytest"]\njunit_option = "--junitxml"\n', "{path}"),
    ('skips = "yes"\n', "[skips]"),
])
def test_commit_is_blocked_when_the_config_has_wrong_types(repo, body, reason):
    write_config(repo, body)

    assert_blocked(repo, commit(repo), reason)


# ── the report lands in the same commit ──────────────────────────────


def test_report_is_staged_into_the_commit_even_when_git_commits_through_a_temporary_index(repo):
    # `git commit <path>` builds the commit from a temporary index
    # (GIT_INDEX_FILE): staging into .git/index would miss the commit.
    runner_config(repo, junit_runner(TWO_SKIPS_UNSORTED))
    (repo / "file.txt").write_text("v1")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "first")
    (repo / "file.txt").write_text("v2")
    (repo / "runner.py").write_text(textwrap.dedent(junit_runner(
        '<testsuite><testcase classname="pkg.new" name="test_only"><skipped message="second"/></testcase></testsuite>')))
    git(repo, "add", "runner.py")

    result = git(repo, "commit", "-q", "-m", "second", "file.txt", "runner.py", check=False)

    assert result.returncode == 0, result.stderr
    assert "`pkg.new::test_only` — second" in committed_report(repo)
