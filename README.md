# RepoBench

[![PyPI](https://img.shields.io/pypi/v/repobench.svg)](https://pypi.org/project/repobench/)
[![Python](https://img.shields.io/pypi/pyversions/repobench.svg)](https://pypi.org/project/repobench/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

**Know which coding agent actually works for your codebase.**

RepoBench turns the merged-PR history your team already shipped into a private,
reproducible benchmark suite — then runs the coding-agent CLIs already installed
and configured on your machine (Claude Code, Codex CLI, OpenCode, Gemini CLI, or
any custom command) against it, with hidden verifiers deciding every verdict.

Public benchmarks answer *"which model scores highest on a fixed suite?"*.
That is not the question your team faces. RepoBench answers:

> **Which agent stack works best for the work that actually happens in this codebase?**

The unit of comparison is the whole **Execution Target** — harness + model +
provider + configuration + repository instructions — not just a model name. It
never manages API keys, never implements an agent loop, and never calls a model
API: it runs the CLIs you already have, the way you already configured them.

## Why RepoBench

- **Your repo IS the benchmark.** Every task is mined from a real merged PR —
  base/gold SHAs, implementation and test changes, task type, subsystem,
  complexity. Nothing is invented, nothing is synthetic.
- **No agent loop to own.** No execution framework, no Docker requirement, no
  backend, no keys to manage. RepoBench runs the harnesses you already have —
  Native Mode measures *your* configuration, not a sanitized copy of it.
- **Honest by design.** Hidden verifiers decide SOLVED/UNSOLVED (exit codes
  never do). Wilson confidence intervals and paired bootstrap comparisons mean
  RepoBench never declares a winner without statistically conclusive evidence.

## Who it's for

**You're the AI / developer-productivity engineer** told to "pick our agent
stack." You need quality, cost and time evidence on *your* workload — not a
leaderboard from someone else's repositories. RepoBench compares full stacks
with confidence intervals and a cost-effective recommendation.

**You're a staff engineer** with a hunch: *"Claude seems better at this, Codex
at that."* RepoBench replaces perception with evidence mined from your own
merged PRs — including `compare` to catch regressions between runs.

**You're an OSS maintainer or local-AI power user** wondering what the model
that costs 8× less actually costs you in quality. RepoBench measures it on the
repository you maintain, with models served anywhere you can point a CLI at.

## Install

Requires Python 3.12+, git, and installed harnesses (`claude`, `codex`,
`opencode`, `gemini`, or a custom command).

```bash
pip install repobench
# or: uv tool install repobench
```

## Quickstart

```bash
cd your-repository

repobench doctor              # git, project tooling, installed harnesses
repobench init                # detect project environment, write repobench.yml
repobench analyze             # mine merged PRs into eval candidates — no tokens used
repobench candidates          # inspect what was found and what was filtered
repobench benchmark build     # reconstruct + validate tasks, sample the benchmark
repobench run claude codex    # run your targets (or: repobench run --all)
repobench report              # comparison with Wilson CIs and $/solve
```

That is the entire product: your history in, a defensible comparison out.

```console
$ repobench run claude codex glm

Running 72 local trials...
  [01/72] t_482_1a2b · claude · SOLVED · 8m41s
  [02/72] t_482_1a2b · codex  · SOLVED · 7m02s
  ...

$ repobench report

Target                     Solve      Time   $/Solve
claude                     86%       9m18      $1.49
codex                      82%       8m42      $0.71
glm                        75%      11m03      $0.18

claude vs codex
Observed difference: +4pp
95% CI: -7pp → +14pp
No conclusive quality difference.

Cost-effective recommendation:
codex
```

`repobench.yml` starts from detected suggestions — test commands, package
manager, benchmark size — and is yours to edit:

```yaml
version: 1

project:
  language: python
  install_command: uv sync --frozen
  test_command: python -m pytest
  regression_command: python -m pytest
  # test_report: auto  # partial credit: per-test counts (passed/total) from the hidden verifier; "off" disables
  # cwd: backend  # monorepos: run the commands inside a sub-project (relative to the repo root)

benchmark:
  size: 24
  dimensions:
    task_type: 0.30
    subsystem: 0.40
    complexity: 0.30

execution:
  jobs: 1
  timeout_minutes: 20

targets:
  claude:
    harness: claude
    model: opus
  codex:
    harness: codex
    model: gpt-x
  glm:
    harness: opencode
    model: zai/glm-x
```

Monorepos: `doctor` and `init` also list sub-projects that carry their own
runner (`backend/`, `api/`, `server/`, `apps/*/`, `packages/*/`, `services/*/`).
Benchmarking still uses one command set — set `project.cwd` (a repo-relative
path) to choose where install/test run. Commands stay argv-only, so `cd X && …`
is not possible; the agent always works from the repository root.

## What you get

**Validated tasks, not scraped diffs.** Each candidate is reconstructed into a
task package (`base.tar`, `instruction.md`, `gold.patch`, `verifier.patch`,
`metadata.json`) and proven with real test runs: baseline must pass (plus a
no-op check), the hidden verifier must fail on the untouched base, must pass
with the gold solution, must survive regression and determinism checks.
Tasks that fail validation are rejected with stable codes — never silently kept.
For pytest-shaped verifiers, counts of the hidden verifier's own tests (`TESTS
9/12`) are recorded per trial as partial credit — a finding beside the verdict,
never part of it.

**A benchmark that represents your workload.** Validated tasks are sampled
greedily so the benchmark's task-type/subsystem/complexity distribution matches
your Workload Universe. Coverage and a composite Benchmark Health score (with
honest warnings) are computed and stored; benchmarks are immutable and
versioned. Health includes a **verifier-strength** component: flakiness across
the append-only validation history plus linter warnings for brittle
exact-string assertions in verifier tests.

**Any harness, including yours.** The generic command adapter (PRD §25) runs
any local CLI. Placeholders `{workspace}` `{prompt}` `{prompt_file}`
`{task_id}` `{target_id}` are substituted into a plain argv list, never a shell:

```yaml
targets:
  my-agent:
    harness: command
    command:
      - my-agent
      - run
      - --model
      - my-model
      - --prompt-file
      - "{prompt_file}"
    output: text
```

Custom commands are explicit local code, so the first execution is gated: the
run preview always shows the exact template, and the run requires
`--trust-custom-command` (or `execution.trust_custom_commands: true`). The
exact template that already ran once is trusted thereafter; any change to it
re-arms the gate.

**A decision layer, not a leaderboard.**

- `report --format json | jsonl | csv` for CI and dashboards;
- `--rollouts N` for pass@k / pass^k multi-rollout reliability (cost ×N — shown
  in the run preview);
- `compare RUN_A RUN_B` for target regression across runs of the same
  benchmark: solve deltas + CI, cost, segments;
- `benchmark refresh` re-analyzes the repo, reports coverage drift, and builds
  the next benchmark version.

**Credibility signals.** Trials whose final diff touches test files are flagged
in the report as reward hacking ("test tampering") without changing the
verifier's verdict. Cost attribution follows a strict chain: harness-reported
cost → your `pricing:` rule → a bundled, dated pricing-catalog estimate (always
marked `~`/`CATALOG_ESTIMATE`, never presented as fact). Unpriced models get an
explicit warning instead of a silent gap.

**Reproducible runs.** `runs/<id>/manifest.json` captures the RepoBench
version, OS/arch, harness versions, a config hash per target and the hashes of
repository instruction files (AGENTS.md/CLAUDE.md/GEMINI.md) — never credential
contents. Each trial persists its prompt, stdout/stderr and agent patch under
`runs/<id>/trials/<trial>/`.

## How it works

```text
engineering history
      │
      ▼
1. DISCOVER   every merged PR assessed as a potential retrospective task;
              hard filters reject candidates with stable codes
2. VALIDATE   reconstruct the task package and prove it with real test runs:
              baseline / no-op / oracle / regression / determinism
3. REPRESENT  sample validated tasks so the benchmark matches your
              Workload Universe; compute coverage and Benchmark Health
4. PREPARE    materialize the base tree into a fresh workspace with a
              synthetic git repository (one commit, no history, no gold)
5. EXECUTE    run the target's harness CLI natively, with timeout and
              process-group cleanup; capture the final tree diff
6. VERIFY     apply the hidden verifier on a copy; hidden tests — never
              exit codes — decide SOLVED/UNSOLVED
7. MEASURE    quality, time, tokens, cost (when the harness reports it)
8. DECIDE     Pareto frontier, paired comparisons, cost-effective
              recommendation — only when statistically conclusive
```

## Methodology and honesty

What RepoBench measures, and what it refuses to claim:

- **Instruction provenance is explicit.** Every task carries a confidence tier:
  **A** — pre-existing linked issue; **B** — strong PR problem statement;
  **C** — possibly solution-contaminated (title fallback or fix-like body);
  **D** — LLM-derived from the implementation diff, strictly opt-in via
  `instruction_generation:` (runs during `benchmark build` only — `analyze`
  stays token-free; the hidden test patch is never in the prompt and a
  deterministic validator rejects drafts that quote the solution). Restrict a
  benchmark to specific tiers with `benchmark.allowed_confidences`.
- **Contamination is surfaced, not hidden.** No network sandbox means an agent
  could search the web, and for public repositories the model may have seen the
  code before. Benchmark Health reports this limitation explicitly. Private
  repositories are the methodologically stronger setting.
- **Not a security sandbox.** Trials execute host-native. RepoBench reduces
  solution leakage — synthetic git workspace (one "RepoBench benchmark base"
  commit, no remotes, no history), gold patch and verifier kept outside the
  workspace until verification, `GH_TOKEN`/`GITHUB_TOKEN` scrubbed,
  `GH_CONFIG_DIR` pointed at an empty temp dir, `SSH_AUTH_SOCK` removed, git
  credential prompts disabled, no original repository path in the prompt — but
  a broad-permission agent can still touch your filesystem, network and
  processes.
- **No invented data.** Missing tokens or cost are reported as `unavailable`
  with a warning, never estimated into existence. There is no LLM judge and no
  generated tests.

## RepoBench vs public benchmarks

|                        | SWE-bench-style public suites       | RepoBench                          |
| ---------------------- | ----------------------------------- | ---------------------------------- |
| Tasks                  | Fixed, public, other people's repos | Mined from *your* merged PRs       |
| Contamination          | Solutions are in training data      | Private suite; warnings surfaced for public repos |
| What is compared       | Models on a fixed suite             | Whole Execution Targets on your workload |
| Infrastructure         | Container/cloud orchestration       | Local subprocess; no backend, no Docker |
| Harness                | The benchmark's own scaffolding     | Your installed CLIs, your configuration |
| Statistics             | Leaderboard percentages             | Wilson CIs, paired bootstrap, pass@k / pass^k — no winner without conclusive evidence |
| Verdict                | Public tests, public task list      | Hidden verifiers; validation history auditable |

These complement each other: public suites rank models in the abstract;
RepoBench ranks stacks on the work you actually do.

## Requirements

- **git** — history mining and workspace materialization.
- **Python 3.12+**.
- **gh** (optional) — GitHub CLI for PR/issue enrichment when your repository
  has a GitHub origin; without it, RepoBench degrades to local history metadata.
- **Harnesses** — installed and authenticated by you. RepoBench detects
  binaries and versions in `repobench doctor` but never probes auth, never runs
  inference outside a benchmark run, and never stores credentials.

## Roadmap

The roadmap lives in [CONTRIBUTING.md](CONTRIBUTING.md) as a set of concrete
issues. Next up: an official GitHub Action with PR-comment reports ([#20]),
benchmark bundle export/import for teammates and CI ([#22]), and a shareable
HTML report ([#21]).

## Contributing

Issues, pull requests and methodology critique are welcome — start with
[CONTRIBUTING.md](CONTRIBUTING.md).

## Documentation

The full product spec — methodology, statistics, security model, acceptance
criteria — lives in [docs/PRD.md](docs/PRD.md).

## License

Apache License 2.0 — see [LICENSE](LICENSE).

[#20]: https://github.com/felipcsousa/repobench/issues/20
[#21]: https://github.com/felipcsousa/repobench/issues/21
[#22]: https://github.com/felipcsousa/repobench/issues/22
