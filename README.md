# RepoBench

**Benchmark the coding agents you actually use on the code you actually work on.**

> **Your repo. Your agents. Your benchmark.**

RepoBench turns a repository's real engineering history into a private, reproducible
benchmark suite, then runs the coding agent CLIs already installed and configured on
your machine — Claude Code, Codex CLI, OpenCode, Gemini CLI, or any custom command —
against it, verifying every attempt with hidden tests.

Public benchmarks answer *"which model scores highest on a fixed suite?"*.
That is not the question your team faces. You need to answer:

> **Which agent stack works best for the work that actually happens in this codebase?**

RepoBench compares whole **Execution Targets** — harness + model + provider +
configuration + repository instructions — on tasks mined from your own merged PRs.
It never manages API keys, never implements an agent loop, and never calls a model
API: it runs the CLIs you already have, the way you already have them configured.

```console
$ repobench init

1,842 PRs analyzed (last 180 days)
43 high-confidence eval candidates

$ repobench benchmark build

24 representative tasks
Benchmark Health: 86/100

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

That is the entire product: your history in, a defensible comparison out.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/):

```bash
# run inside a checkout of this repository
uv sync

# or install as a tool
uv tool install .
```

## Quickstart

```bash
cd your-repository

repobench doctor            # git, project tooling, installed harnesses
repobench init              # detect project environment, write repobench.yml
repobench analyze           # mine merged PRs into eval candidates (no tokens used)
repobench candidates        # inspect what was found and what was filtered
repobench benchmark build   # reconstruct + validate tasks, sample a benchmark
                             #   --reuse-valid skips revalidating tasks that already
                             #   passed (content-derived ids; --force-revalidate overrides)
repobench benchmark refresh # re-analyze, report coverage drift, build the next version
repobench run claude codex glm   # or: repobench run --all
                             #   --rollouts N adds pass@k / pass^k reliability
                             #   (cost ×N — shown in the run preview)
repobench runs              # list recorded runs; --show <id> for per-target detail
repobench report            # text; --format json | jsonl | csv for CI/dashboards
repobench compare RUN_A RUN_B   # target regression between two runs of the same
                             #   benchmark: solve deltas + CI, cost, segments
repobench clean --runs 5 --apply   # GC old runs/workspaces (dry-run by default)
```

`repobench.yml` starts from detected suggestions — test commands, package manager,
benchmark size — and is yours to edit:

```yaml
version: 1

project:
  language: python
  install_command: uv sync --frozen
  test_command: python -m pytest
  regression_command: python -m pytest

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

## How it works

- **Mining** — every merged PR (GitHub merge-commit convention) is assessed as a
  potential retrospective task: base/gold SHAs, implementation vs test changes,
  task type, subsystem and complexity. Hard filters (no test change, no instruction
  provenance, too small/too large) reject candidates with stable codes — `repobench
  candidates` shows exactly what was filtered and why.
- **Validation** — each candidate is reconstructed into a task package
  (`base.tar`, `instruction.md`, `gold.patch`, `verifier.patch`, `metadata.json`)
  and proven with real test runs: the baseline must pass, the hidden verifier must
  fail on the untouched base, must pass with the gold solution, and must pass
  deterministically. Tasks that fail are rejected, never silently kept.
- **Benchmark sampling** — validated tasks are sampled greedily so the benchmark's
  task-type/subsystem/complexity distribution matches your Workload Universe.
  Coverage and a composite Benchmark Health score (with honest warnings) are
  computed and stored; benchmarks are immutable and versioned.
- **Local execution** — each trial materializes the base tree into a fresh
  workspace with a **synthetic git repository** (a single "RepoBench benchmark
  base" commit, no remotes, no history, no gold), runs the target's harness CLI
  with a timeout and process-group cleanup, then captures the final tree diff.
- **Hidden verifier + stats** — after the agent finishes, the verifier patch is
  applied on a copy and the hidden tests decide SOLVED/UNSOLVED (exit codes never
  do). Results are aggregated with Wilson confidence intervals and paired
  bootstrap comparisons; a "winner" is only declared when the difference is
  statistically conclusive, and the cost-effective recommendation never invents
  cost data it does not have.

## Instruction tiers

Every task carries an instruction-confidence tier describing where its
instruction came from (PRD §71-72):

- **A** — a pre-existing linked issue: intent recorded before the change.
- **B** — a strong PR problem statement (no fix details detected).
- **C** — possibly solution-contaminated: the PR title fallback (the offline
  case) or a PR body that reads like a fix description.
- **D** — LLM-derived from the implementation diff. Strictly opt-in via
  `instruction_generation` in `repobench.yml`: during `benchmark build` (never
  during `analyze`, which stays token-free) RepoBench asks one of *your*
  configured targets to draft a problem description from the gold
  implementation diff — the hidden test patch is never part of that prompt,
  and a deterministic validator rejects drafts that quote the solution. A D
  instruction is derived from the solution by construction, making it
  methodologically the weakest tier (ranked below C); if you enable it,
  report the tier mix of your benchmark (printed at build time). PRD §71
  originally ruled this out for V1; it was vetted as an explicit opt-in.

```yaml
instruction_generation:
  enabled: true
  target: claude        # any target in `targets:`; spends real tokens
  timeout_minutes: 5
```

You can also restrict a benchmark to specific tiers with
`benchmark.allowed_confidences` (e.g. `[A, B]`); the default accepts all tiers.

## Requirements

- **git** — history mining and workspace materialization.
- **Python 3.12+** with [uv](https://docs.astral.sh/uv/).
- **gh** (optional) — GitHub CLI for PR/issue enrichment when your repository has a
  GitHub origin. Without it, RepoBench degrades to local history metadata.
- **Harnesses** — installed and authenticated by you, exactly as you normally use
  them (`claude`, `codex`, `opencode`, `gemini`, or a custom command). RepoBench
  detects binaries and versions in `repobench doctor` but never probes auth, never
  runs inference outside a benchmark run, and never stores credentials.

## Security model (read this)

**RepoBench is not a security sandbox.** Trials execute host-native on your machine.
A coding agent with broad permissions can access the filesystem, the network and
processes. What RepoBench *does* is reduce solution leakage:

- synthetic git repository: one commit, no remotes, no original history or branches;
- gold patch and hidden verifier kept outside the workspace until verification;
- `GH_TOKEN` / `GITHUB_TOKEN` scrubbed, `GH_CONFIG_DIR` pointed at an empty temp dir,
  `SSH_AUTH_SOCK` removed, git credential prompts disabled;
- no original repository path in the prompt.

**Network isolation: none.** Without a network sandbox an agent could search the web,
and for public repositories the model may have seen the code before. Benchmark Health
reports this limitation explicitly instead of hiding it. Private repositories are the
methodologically stronger setting.

## Any harness, including yours

The generic command adapter (PRD §25) runs any local CLI. Placeholders:
`{workspace}` `{prompt}` `{prompt_file}` `{task_id}` `{target_id}` — substituted into
a plain argv list, never a shell:

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

Custom commands are explicit local code, so the first execution is gated: the run
preview always shows the exact template, and the run requires
`--trust-custom-command` (or `execution.trust_custom_commands: true` in
repobench.yml). The exact template that already ran once is trusted thereafter;
any change to it re-arms the gate.

## Reproducibility

Native Mode measures your local configuration, so every run records what ran:
`runs/<id>/manifest.json` captures the RepoBench version, OS/arch, harness
versions, a config hash per target and the hashes of repository instruction files
(AGENTS.md/CLAUDE.md/GEMINI.md) — never credential contents. Each trial persists
its prompt, stdout/stderr and agent patch under `runs/<id>/trials/<trial>/`.

## Documentation

The full product spec — methodology, statistics, security model, acceptance criteria,
roadmap — lives in [docs/PRD.md](docs/PRD.md).

## Development

```bash
uv sync
uv run pytest -q        # unit + integration + end-to-end (hermetic, no network)
uv run repobench --help
```

## License

Apache License 2.0 — see [LICENSE](LICENSE).
