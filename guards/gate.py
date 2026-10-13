"""Pre-commit gate: compute review facts into the commit.

Run by guards/hooks/pre-commit on every `git commit` once installed with
`install.sh --gate <repo>`. Standard library only.
"""
import os
import subprocess
import sys
import tempfile

if sys.version_info < (3, 11):
    sys.exit("gate: blocked: Python >= 3.11 required (tomllib); found "
             + ".".join(map(str, sys.version_info[:3])))

import tomllib  # noqa: E402
import xml.etree.ElementTree as ET  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Mapping  # noqa: E402

REPORT_PATH = ".workflow/gate-report.md"
CONFIG_PATH = ".workflow/gate.toml"

HOOKS_DIR = Path(__file__).resolve().parent / "hooks"


class GateError(Exception):
    """A configured check cannot run. The commit is blocked; str(e) is the reason."""


@dataclass(frozen=True)
class SkipsConfig:
    command: tuple[str, ...]
    junit_option: str = "--junitxml={path}"


@dataclass(frozen=True)
class GateConfig:
    skips: SkipsConfig | None


@dataclass(frozen=True)
class SkippedTest:
    test_id: str
    reason: str


@dataclass(frozen=True)
class Report:
    """The facts written to the gate report; each further check adds a sibling field."""
    skips: tuple[SkippedTest, ...] | None


def load_config(repo: Path) -> GateConfig:
    """Read .workflow/gate.toml. A missing file is an error: an installed gate
    without configuration checks nothing, which is inconclusive, not a pass."""
    path = repo / CONFIG_PATH
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise GateError(f"{CONFIG_PATH} not found: the gate is installed but not configured")
    except (OSError, UnicodeDecodeError) as e:
        raise GateError(f"{CONFIG_PATH}: cannot read: {e}")
    except tomllib.TOMLDecodeError as e:
        raise GateError(f"{CONFIG_PATH}: invalid TOML: {e}")
    skips = data.get("skips")
    return GateConfig(skips=None if skips is None else _parse_skips(skips))


def _parse_skips(table: Any) -> SkipsConfig:
    if not isinstance(table, dict):
        raise GateError(f"{CONFIG_PATH}: [skips] must be a table")
    command = table.get("command")
    if (not isinstance(command, list) or not command
            or not all(isinstance(arg, str) and arg for arg in command)):
        raise GateError(f"{CONFIG_PATH}: skips.command must be a non-empty list of strings (argv)")
    junit_option = table.get("junit_option", SkipsConfig.junit_option)
    if not isinstance(junit_option, str):
        raise GateError(f"{CONFIG_PATH}: skips.junit_option must be a string")
    return SkipsConfig(command=tuple(command), junit_option=junit_option)


def clean_env(env: Mapping[str, str]) -> dict[str, str]:
    """Drop every GIT_* variable git exports to hooks (GIT_INDEX_FILE, GIT_DIR, ...).

    A test suite that runs git would otherwise act on the commit's index.
    """
    return {k: v for k, v in env.items() if not k.startswith("GIT_")}


def run_skips(repo: Path, cfg: SkipsConfig) -> tuple[SkippedTest, ...]:
    """Run the test command with a JUnit report and return the skipped tests.

    A non-zero exit with a valid JUnit is not an error: failing tests are not
    this gate's job. The command's console output goes to stderr for the
    developer and is never parsed.
    """
    with tempfile.TemporaryDirectory(prefix="gate-") as tmp:
        junit = Path(tmp) / "junit.xml"
        # A junit_option without {path} writes no report and is blocked below.
        argv = [*cfg.command, cfg.junit_option.replace("{path}", str(junit))]
        try:
            subprocess.run(argv, cwd=repo, env=clean_env(os.environ),
                           stdin=subprocess.DEVNULL, stdout=sys.stderr)
        except OSError as e:
            raise GateError(f"test command cannot start: {cfg.command[0]}: {e.strerror or e}")
        try:
            xml_text = junit.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise GateError(f"test command produced no JUnit report ({' '.join(argv)})")
        except (OSError, UnicodeDecodeError) as e:
            raise GateError(f"cannot read the JUnit report: {e}")
        return parse_junit(xml_text)


def parse_junit(xml_text: str) -> tuple[SkippedTest, ...]:
    """Skipped test cases from a JUnit report, sorted by test id."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise GateError(f"JUnit report is not parsable XML: {e}")
    if root.tag not in ("testsuites", "testsuite"):
        raise GateError(f"JUnit report has unexpected root <{root.tag}>")
    found = [
        SkippedTest(test_id=f"{case.get('classname', '')}::{case.get('name', '')}",
                    reason=skipped.get("message", ""))
        for case in root.iter("testcase")
        if (skipped := case.find("skipped")) is not None
    ]
    return tuple(sorted(found, key=lambda s: s.test_id))


def render_report(report: Report) -> str:
    """Deterministic markdown: no timestamps, skips already sorted by parse_junit."""
    lines = [
        "# Gate report",
        "",
        f"Generated by the pre-commit gate (guards/gate.py) from {CONFIG_PATH}.",
        "Do not edit: it is rewritten on every commit.",
        "",
        "## Skipped tests",
        "",
    ]
    if report.skips is None:
        lines.append("not configured")
    elif not report.skips:
        lines.append("none")
    else:
        for skip in report.skips:
            reason = " ".join(skip.reason.split())
            lines.append(f"- `{skip.test_id}` — {reason}" if reason else f"- `{skip.test_id}`")
    return "\n".join(lines) + "\n"


def _git(repo: Path, *args: str, what: str) -> str:
    """Run git in repo; a non-zero exit becomes GateError("cannot <what>: ...").

    The hook's environment is inherited: GIT_INDEX_FILE is the index being committed.
    """
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise GateError(f"cannot {what}: {result.stderr.strip()}")
    return result.stdout.strip()


def write_and_stage(repo: Path, text: str) -> None:
    path = repo / REPORT_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as e:
        raise GateError(f"cannot write {REPORT_PATH}: {e}")
    _git(repo, "add", "--", REPORT_PATH, what=f"stage {REPORT_PATH}")


def install(repo: Path) -> None:
    """Point the repo-local core.hooksPath at the kit's hooks.

    Idempotent. Refuses a repo subdirectory (the enclosing repo's config would
    change) and a hooksPath already set to something else (another hook
    manager would be silently replaced). Never touches global config.
    """
    top = _git(repo, "rev-parse", "--show-toplevel", what=f"find the work tree of {repo}")
    if Path(top).resolve() != repo.resolve():
        raise GateError(f"{repo} is not the top level of its repository ({top}); "
                        "pass the top level")
    current = _git(repo, "config", "--local", "--default", "", "--get", "core.hooksPath",
                   what=f"read core.hooksPath in {repo}")
    # A relative hooksPath is relative to the top level, as git reads it.
    if current and (Path(top) / current).resolve() == HOOKS_DIR:
        return
    if current:
        raise GateError(f"core.hooksPath is already set to {current!r} in {repo}; "
                        "unset it first (git config --local --unset core.hooksPath) "
                        "if you want the gate to replace it")
    hooks = repo / _git(repo, "rev-parse", "--git-path", "hooks", what="locate .git/hooks")
    active = sorted(h.name for h in hooks.glob("*") if h.is_file() and h.suffix != ".sample")
    _git(repo, "config", "--local", "core.hooksPath", str(HOOKS_DIR),
         what=f"set core.hooksPath in {repo}")
    if active:
        print(f"gate: warning: these hooks in {hooks} will stop running: {', '.join(active)}",
              file=sys.stderr)


def pre_commit(repo: Path) -> int:
    """0 = report written and staged; 1 = a configured check cannot run (blocked)."""
    try:
        cfg = load_config(repo)
        skips = run_skips(repo, cfg.skips) if cfg.skips else None
        write_and_stage(repo, render_report(Report(skips=skips)))
    except GateError as e:
        print(f"gate: blocked: {e}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["pre-commit"]:
        # githooks(5): git runs pre-commit from the top of the work tree.
        return pre_commit(Path.cwd())
    if len(args) == 2 and args[0] == "install":
        try:
            install(Path(args[1]))
        except GateError as e:
            print(f"gate: install failed: {e}", file=sys.stderr)
            return 1
        print(f"gate: installed in {args[1]} (core.hooksPath = {HOOKS_DIR})")
        return 0
    print("usage: gate.py pre-commit | gate.py install <repo>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
