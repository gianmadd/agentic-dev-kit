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
    data = tomllib.loads((repo / CONFIG_PATH).read_text())
    skips = data.get("skips")
    if skips is None:
        return GateConfig(skips=None)
    return GateConfig(skips=SkipsConfig(
        command=tuple(skips["command"]),
        junit_option=skips.get("junit_option", "--junitxml={path}"),
    ))


def clean_env(env: Mapping[str, str]) -> dict[str, str]:
    return dict(env)


def run_skips(repo: Path, cfg: SkipsConfig) -> tuple[SkippedTest, ...]:
    with tempfile.TemporaryDirectory(prefix="gate-") as tmp:
        junit = Path(tmp) / "junit.xml"
        argv = [*cfg.command, cfg.junit_option.format(path=junit)]
        subprocess.run(argv, cwd=repo, env=clean_env(os.environ),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return parse_junit(junit.read_text())


def parse_junit(xml_text: str) -> tuple[SkippedTest, ...]:
    root = ET.fromstring(xml_text)
    found = []
    for case in root.iter("testcase"):
        skipped = case.find("skipped")
        if skipped is not None:
            found.append(SkippedTest(
                test_id=f"{case.get('classname', '')}::{case.get('name', '')}",
                reason=skipped.get("message", ""),
            ))
    return tuple(found)


def render_report(report: Report) -> str:
    lines = ["# Gate report", "", "## Skipped tests", ""]
    for skip in report.skips or ():
        lines.append(f"- `{skip.test_id}` — {skip.reason}")
    return "\n".join(lines) + "\n"


def write_and_stage(repo: Path, text: str) -> None:
    path = repo / REPORT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    subprocess.run(["git", "-C", str(repo), "add", "--", REPORT_PATH], check=True)


def install(repo: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "config", "--local",
                    "core.hooksPath", str(HOOKS_DIR)], check=True)


def pre_commit(repo: Path) -> int:
    cfg = load_config(repo)
    skips = run_skips(repo, cfg.skips) if cfg.skips else None
    write_and_stage(repo, render_report(Report(skips=skips)))
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
