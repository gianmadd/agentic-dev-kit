# agentic-dev-kit

[![version](https://img.shields.io/github/v/tag/salvatorebottiglieri/agentic-dev-kit?label=version&sort=semver)](CHANGELOG.md)
[![license: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

An opinionated, phase-by-phase workflow for building software with an AI
coding agent — from a raw idea to a merged PR — plus the custom skills and the
audit gate that enforce it.

The method is **agent-agnostic**: `WORKFLOW.md` is plain process, readable by
any agent. The skills ship in the portable *agent-skills* (`SKILL.md`) format,
which Claude Code loads natively and other agents can adopt.

`WORKFLOW.md` is the entry point: it defines the pipeline every session
follows (design → product review → system architecture → tickets → program
design → implement-loop) and the review criteria family that gates a merge.

## What's in here

| Path | What it is |
|---|---|
| `WORKFLOW.md` | **Start here.** The operational spec — the phases, in order, and the rules. |
| `INVARIANTS.md` / `INVARIANTS-METHOD.md` | The workflow invariants (I1–I6) the audit enforces, and the theory behind them. |
| `RATIONALE.md` | The *why* of every load-bearing rule. |
| `skills/` | The four custom skills the workflow invokes (`implement-loop`, `product-review`, `system-architecture`, `program-design`). |
| `audit.py` + `tests/` | The session-end gate: verifies the invariants against the tracker. |
| `handoffs/` | Where your own session handoffs land (starts empty). |

## Two layers — mind the prerequisite

This kit is a **layer on top of** [Matt Pocock's skills](https://github.com/mattpocock/skills)
(MIT). The backbone flow it orchestrates — `grill-with-docs`, `to-spec`,
`to-tickets`, `implement`, `tdd`, `code-review`, `ponytail-review`,
`domain-modeling`, `ask-matt` — lives there, not here. This repo adds the
orchestration doc (`WORKFLOW.md`), the invariant gate (`audit.py`), and four
skills that slot into the flow.

**Install the base layer first**, then this one:

```bash
# 1. base layer — Matt Pocock's skills (see that repo's install steps)
#    https://github.com/mattpocock/skills

# 2. this kit
git clone https://github.com/salvatorebottiglieri/agentic-dev-kit.git ~/agentic-dev-kit
cd ~/agentic-dev-kit
./install.sh
```

`install.sh` symlinks the four skills from `skills/` into your agent's skills
directory and leaves the docs and `audit.py` in the clone. It defaults to
`~/.claude/skills` (Claude Code); point it elsewhere for any other agent:

```bash
AGENT_SKILLS_DIR=~/.agents/skills ./install.sh
```

It never overwrites a skill you already have without asking.

## Using it

Have your coding agent read `~/agentic-dev-kit/WORKFLOW.md` at the start of any
non-trivial coding work — the usual way is a rule in the agent's global or
project instructions file (`CLAUDE.md`, `AGENTS.md`, or your agent's
equivalent). Then follow its phases. A typo fix, a one-line config change, or a
question about code does not need the pipeline.

## Pre-commit gate

A git pre-commit hook that computes facts the review needs and stages them
into the commit itself, so they do not depend on what the agent chooses to
report. Requires Python >= 3.11 (`tomllib`). Install it into a repository's
top level (repo-local `core.hooksPath`, no global config touched; idempotent):

```bash
~/agentic-dev-kit/install.sh --gate /path/to/repo
```

If `core.hooksPath` is already set to something else (e.g. husky's `.husky`),
the install refuses and changes nothing: unset it first if the gate should
replace it. Hooks in `.git/hooks/` stop running once `core.hooksPath` is set;
the install names any it finds.

Configure it in that repository's `.workflow/gate.toml`:

```toml
[skips]
command = ["python3", "-m", "pytest", "-q"]   # argv, not a shell string
# junit_option = "--junitxml={path}"          # default; {path} is a temp file
```

On every `git commit` the gate runs `command` plus the JUnit option (with
every `GIT_*` variable removed from its environment), and writes
`.workflow/gate-report.md` into the same commit: each skipped test as
`<classname>::<name>` with its skip reason, sorted, no timestamps — `none`
when nothing is skipped, `not configured` when `[skips]` is absent. Skips are
read from the JUnit report only, never from console output. Deselected tests
are not listed: JUnit does not contain them.

It **blocks the commit only when a configured check cannot run**: Python
older than 3.11, missing, unreadable (or non-UTF-8) `.workflow/gate.toml`,
invalid TOML or wrong types, a test command that cannot start, a JUnit report
that is missing, unreadable or not parsable, or a gate report that cannot be
written or staged. The reason is on stderr. Failing tests do not block — that
is the canonical test gate's job, not this one's.

Known limitation: with `git commit <paths>` the report lands in the commit,
but the main index keeps the previous report afterwards (`git status` shows
`.workflow/gate-report.md` modified) until the next commit.

Known bypasses: `git commit --no-verify`, changing `core.hooksPath`, editing
the gate. Detecting them after the fact is planned (a `final` mode), not
implemented.

## Caveats

- **`audit.py` is GitHub-only** (it reads issues/PRs via the `gh` CLI). On
  GitLab, Bitbucket, or a local tracker the session-end gate is manual — the
  handoff is still the auditable artifact.
- The skills use the `SKILL.md` + `skill://` invocation convention. Agents
  that don't support it can still follow `WORKFLOW.md` and read each
  `skills/<name>/SKILL.md` as a plain instruction file.
- The custom skills marked *WIP* in their own `SKILL.md`
  (`product-review`, `system-architecture`, `program-design`) are first
  versions, not yet battle-tested.

## Versioning

Released under [Semantic Versioning](https://semver.org/); see
[`CHANGELOG.md`](CHANGELOG.md) and the git tags. Pin a specific version by
checking out its tag (e.g. `git checkout v0.1.0`).

## License

MIT — see `LICENSE`. The base-layer skills are Matt Pocock's, under his repo's
MIT license — not redistributed here; install them from the upstream.
