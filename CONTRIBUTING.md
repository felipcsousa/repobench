# Contributing to RepoBench

Thanks for helping. RepoBench is a methodology-heavy tool: the bar for every
contribution is not just "it works" but "it stays honest" — no invented data,
no silent verdicts, no unmeasured claims. This document explains the setup, the
project layout, the conventions that keep it that way, and the roadmap.

## Setup

```bash
git clone https://github.com/felipcsousa/repobench.git
cd repobench
uv sync                 # Python 3.12+ and uv required
uv run pytest           # 458 tests, ~2 min, hermetic — no network, no API keys
uv run repobench --help # the CLI, live from your checkout
```

Everything runs locally. You never need harness credentials to develop or run
the test suite.

## Project map

| Package | Responsibility |
| --- | --- |
| `repobench/core/` | Shared domain types, errors, ids, paths, git utilities, logging |
| `repobench/repository/` | Repository ingestion: git history, GitHub enrichment, workload statistics |
| `repobench/mining/` | Candidate mining: classification, complexity, subsystem, instruction provenance |
| `repobench/tasks/` | Task packaging: diff split, instruction rendering, package reconstruction, leakage |
| `repobench/validation/` | Validation checks and pipeline (PRD §77-82): baseline / no-op / oracle / regression / determinism |
| `repobench/benchmark/` | Benchmark construction: sampling, coverage, health, manifests (PRD §83-89) |
| `repobench/execution/` | Trial execution: runner pipeline (PRD §32, §41-43, §59-64), workspace lifecycle, environment sanitization, process handling, fingerprints, pricing catalog; `adapters/` for `claude`, `codex`, `opencode`, `gemini` and the generic command adapter |
| `repobench/analysis/` | Analysis of run results: metrics, statistics, Pareto, recommendation (PRD §101-110) |
| `repobench/reporting/` | Report rendering: terminal text and machine-readable JSON / JSONL / CSV (PRD §111-112) |
| `repobench/storage/` | SQLite persistence (PRD §114) — a thin layer; domain modules stay pure |
| `repobench/cli/` | Typer app (`app.py`); commands delegate to `services`, `builds`, `reports`, `maintenance`, `render` |
| `tests/` | Hermetic suite: shared fixture repository and fake agents; wave test files |

## Conventions

- **Service-layer CLI.** Commands in `cli/app.py` are thin: they parse
  arguments, print, and delegate orchestration to `cli/services.py` and the
  `builds` / `reports` / `maintenance` / `render` modules. New behavior goes in
  a service or a domain module, not in the command body.
- **Pure domain modules.** `core`, `mining`, `tasks`, `validation`,
  `benchmark`, `analysis` contain no I/O orchestration; `storage/db.py` is the
  only persistence layer, and it stays thin.
- **Docstrings cite sources.** Module and command docstrings reference the PRD
  section (`PRD §83-89`) and the originating issue (`issue #14`). When you add
  behavior, add the pointer.
- **Honesty rules (PRD §53-54).** Never invent data. If the harness reports
  tokens but no cost, report cost as unavailable; if a generic command reports
  only an exit code, both are unavailable. Quality stays measurable; missing
  data produces an explicit warning, never a silent gap or a plausible-looking
  number. Estimates are always marked (`~` / `CATALOG_ESTIMATE`).
- **Hidden-verifier discipline.** Exit codes never decide SOLVED/UNSOLVED, and
  nothing in the execution path may leak the gold patch or verifier into the
  workspace or the prompt.
- **File size.** Keep files under 1000 lines; split by responsibility before
  you reach it.
- **Tests.** The suite is hermetic: a shared fixture repository and fake
  agents stand in for real harnesses — no network, no credentials. Feature
  waves get their own test files (`test_wave2_compare.py`,
  `test_wave3_verifier.py`, ...); land yours alongside the code.

## Pull requests

1. Reference an issue. If none exists for your change, open one first —
   especially for methodology-affecting changes (statistics, verdict rules,
   cost attribution), which should be discussed before code.
2. Keep the suite green: `uv run pytest -q` must pass before you push.
3. One wave (or one concern) per PR. A PR that lands Wave 4 behavior should
   not carry unrelated refactors; small drive-by fixes are fine, mixed
   features are not.

## Roadmap

The roadmap is the open issue list, not a document that drifts. Current
priorities (labels `P0` highest product value → `P2` later / opt-in):

| Priority | Issue | What it delivers | Effort |
| --- | --- | --- | --- |
| **P0** | [#20](https://github.com/felipcsousa/repobench/issues/20) | Official GitHub Action: wrap `run --yes` + `report --format json/text`, post a PR-comment comparison (solve %, Wilson CIs, $/solve — "no conclusive difference" copy intact) | S |
| **P1** | [#21](https://github.com/felipcsousa/repobench/issues/21) | Single-file HTML report (`report --format html` is a stub today): tables + CI visualization, shareable with stakeholders (PRD §113) | S |
| **P1** | [#22](https://github.com/felipcsousa/repobench/issues/22) | Benchmark bundle export/import (`benchmark export <id> -o bundle.tar.gz` / `benchmark import`): build the expensive validated benchmark once, teammates and CI re-run it | M |
| **P1** | [#26](https://github.com/felipcsousa/repobench/issues/26) | **Decision item, not code** — name collision with RepoBench (ICLR 2024 repo-level code-completion benchmark): keep and differentiate the narrative, or rename pre-1.0. Input welcome | — |
| **P2** | [#23](https://github.com/felipcsousa/repobench/issues/23) | Config A/B: AGENTS.md sections, skills, MCP servers, permission flags as paired targets — same harness+model, config differs; the paired bootstrap already does the statistics | M |
| **P2** | [#24](https://github.com/felipcsousa/repobench/issues/24) | Harbor task-format export: re-run validated tasks in container/cloud environments; opt-in, local-first stays the default | M |
| **P2** | [#25](https://github.com/felipcsousa/repobench/issues/25) | Container runner, opt-in (`runner: local \| container`): local remains the default; Workspace and Runner are already separable (PRD §151) | L |

Planned waves:

- **Wave 4 — distribution:** [#20](https://github.com/felipcsousa/repobench/issues/20) [#21](https://github.com/felipcsousa/repobench/issues/21) [#22](https://github.com/felipcsousa/repobench/issues/22)
  Get benchmarks and reports out of one machine: CI integration, shareable
  reports, reusable bundles.
- **Wave 5 — depth (opt-in):** [#23](https://github.com/felipcsousa/repobench/issues/23) [#24](https://github.com/felipcsousa/repobench/issues/24) [#25](https://github.com/felipcsousa/repobench/issues/25)
  New experiment types and execution modes that extend Native Mode without
  making it mandatory.
- **Open decision:** [#26](https://github.com/felipcsousa/repobench/issues/26)
  A positioning call that belongs to the community while mindshare is small —
  weigh in on the issue before any rename work starts.

Want to pick something up? Comment on the issue so work isn't duplicated, and
start from the setup above.

## Release process

Maintainers cut releases from `main`:

1. Bump the version in **both** places — `version` in `pyproject.toml` and
   `__version__` in `repobench/__init__.py` (the CLI reads the latter).
2. Green suite: `uv run pytest -q`.
3. Tag and build:

   ```bash
   git tag vX.Y.Z
   uv build
   uv publish
   git push origin main --tags
   ```

## License

By contributing, you agree that your contributions are licensed under the
Apache License 2.0, the project's license — see [LICENSE](LICENSE).
