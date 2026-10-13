"""Pre-commit gate: compute review facts into the commit.

Run by guards/hooks/pre-commit on every `git commit` once installed with
`install.sh --gate <repo>`. Standard library only.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

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
    skips: tuple[SkippedTest, ...] | None


def load_config(repo: Path) -> GateConfig:
    """Read .workflow/gate.toml. A missing file is an error: an installed gate
    without configuration checks nothing, which is inconclusive, not a pass."""
    path = repo / CONFIG_PATH
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise GateError(f"{CONFIG_PATH} not found: the gate is installed but not configured")
    except tomllib.TOMLDecodeError as e:
        raise GateError(f"{CONFIG_PATH}: invalid TOML: {e}")
    skips = data.get("skips")
    if skips is None:
        return GateConfig(skips=None)
    if not isinstance(skips, dict):
        raise GateError(f"{CONFIG_PATH}: [skips] must be a table")
    unknown = sorted(set(skips) - {"command", "junit_option"})
    if unknown:
        raise GateError(f"{CONFIG_PATH}: unknown key(s) in [skips]: {', '.join(unknown)}")
    command = skips.get("command")
    if (not isinstance(command, list) or not command
            or not all(isinstance(arg, str) and arg for arg in command)):
        raise GateError(f"{CONFIG_PATH}: skips.command must be a non-empty list of strings (argv)")
    junit_option = skips.get("junit_option", SkipsConfig.junit_option)
    if not isinstance(junit_option, str):
        raise GateError(f"{CONFIG_PATH}: skips.junit_option must be a string")
    if "{path}" not in junit_option:
        raise GateError(f"{CONFIG_PATH}: skips.junit_option must contain {{path}}")
    try:
        junit_option.format(path="x")
    except (KeyError, IndexError, ValueError):
        raise GateError(f"{CONFIG_PATH}: skips.junit_option may only use the {{path}} placeholder")
    return GateConfig(skips=SkipsConfig(command=tuple(command), junit_option=junit_option))


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
        argv = [*cfg.command, cfg.junit_option.format(path=junit)]
        try:
            run = subprocess.run(argv, cwd=repo, env=clean_env(os.environ),
                                 stdin=subprocess.DEVNULL, capture_output=True, text=True)
        except OSError as e:
            raise GateError(f"test command cannot start: {cfg.command[0]}: {e.strerror or e}")
        sys.stderr.write(run.stdout + run.stderr)
        try:
            xml_text = junit.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise GateError(f"test command produced no JUnit report ({' '.join(argv)})")
        return parse_junit(xml_text)


def parse_junit(xml_text: str) -> tuple[SkippedTest, ...]:
    """Skipped test cases from a JUnit report, sorted by test id."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise GateError(f"JUnit report is not parsable XML: {e}")
    if root.tag not in ("testsuites", "testsuite"):
        raise GateError(f"JUnit report has unexpected root <{root.tag}>")
    found = []
    for case in root.iter("testcase"):
        skipped = case.find("skipped")
        if skipped is not None:
            found.append(SkippedTest(
                test_id=f"{case.get('classname', '')}::{case.get('name', '')}",
                reason=skipped.get("message", ""),
            ))
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


def write_and_stage(repo: Path, text: str) -> None:
    path = repo / REPORT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    # Inherit the hook's environment: GIT_INDEX_FILE is the index being committed.
    result = subprocess.run(["git", "-C", str(repo), "add", "--", REPORT_PATH],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise GateError(f"cannot stage {REPORT_PATH}: {result.stderr.strip()}")


def install(repo: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "config", "--local",
                    "core.hooksPath", str(HOOKS_DIR)], check=True)


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
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True)
        return pre_commit(Path(top.stdout.strip()))
    if len(args) == 2 and args[0] == "install":
        install(Path(args[1]))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
