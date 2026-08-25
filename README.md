# RepoBench

**Living repository-native evals for coding agents.**

Find which coding agent actually works for your codebase.

## Quick Start

```bash
# Install
pipx install repobench

# Initialize in your repo
cd my-project
repobench init

# Analyze workload
repobench analyze

# Build benchmark
repobench benchmark build

# Run agents
repobench run codex-local claude-local

# Get results
repobench report
```

## What is RepoBench?

RepoBench transforms your repository's real engineering history into a private, reproducible, representative benchmark for coding agents.

Instead of relying on public benchmarks that don't match your work, RepoBench mines your actual merged PRs to create evals that reflect what your team actually ships.

### The Pipeline

```
Repository history
        │
        ▼
   DISCOVER    What happened here?
        │
        ▼
     FILTER    Can it become an eval?
        │
        ▼
    VALIDATE   Can we prove correctness?
        │
        ▼
  REPRESENT    Does the benchmark match
               the actual workload?
        │
        ▼
      RUN      Execute agent configurations
        │
        ▼
    MEASURE    Quality / cost / latency
        │
        ▼
    DECIDE     What should we use?
```

## Commands

| Command | Description |
|---|---|
| `repobench doctor` | Check prerequisites |
| `repobench init` | Initialize in current repo |
| `repobench analyze` | Analyze repository workload |
| `repobench candidates` | View mined candidate tasks |
| `repobench task inspect <id>` | Inspect a candidate task |
| `repobench benchmark build` | Build representative benchmark |
| `repobench benchmark list` | List existing benchmarks |
| `repobench run <configs...>` | Run agents via Harbor |
| `repobench report` | Generate comparison report |
| `repobench config show` | Show current configuration |

## Configuration

RepoBench creates `repobench.yml` in your repo:

```yaml
version: 1

repository:
  provider: github
  lookback_days: 180

project:
  languages:
    - python
    - go
    - java
  install_command: pip install -e ".[dev]"
  test_command: pytest

benchmark:
  size: 24
  dimensions:
    task_type: 0.30
    subsystem: 0.40
    complexity: 0.30

execution:
  environment: docker
  concurrency: 4

agents:
  codex-default:
    agent: codex
    model: openai/gpt-4o

  claude-default:
    agent: claude-code
    model: anthropic/claude-opus-4
```

## Supported Languages

- Python
- JavaScript / TypeScript
- Go
- Java (Maven / Gradle)

## How it Works

### 1. Workload Analysis

RepoBench fetches your merged PRs via GitHub API and classifies each by:
- **Task type**: bugfix, feature, refactor
- **Subsystem**: payments, auth, frontend, etc.
- **Complexity**: small, medium, large

### 2. Candidate Mining

From the workload, RepoBench identifies PRs that can become reliable evals:
- Have linked issues or clear PR descriptions
- Include test changes (verifier evidence)
- Are within configurable size bounds
- Don't require unsupported environments

### 3. Validation Pipeline

Each candidate passes rigorous validation:
- **Base health**: tests pass before the change
- **No-op validation**: new tests fail without the fix
- **Oracle validation**: tests pass with the fix
- **Determinism**: tests produce consistent results
- **Leakage protection**: agent can't access gold solution

### 4. Representative Sampling

RepoBench selects benchmark tasks using stratified optimization to match your actual workload distribution across task type, subsystem, and complexity.

### 5. Execution via Harbor

Benchmarks are exported in Harbor format and executed via `harbor run`:

```bash
repobench run codex-default claude-default
```

### 6. Decision Report

```bash
repobench report
```

```
REPOBENCH REPORT
────────────────────────────────────────

Repository:    my-org/payments
Benchmark:     rb_b_20260825_a84f
Tasks:         24
Health:        87/100

                      Solve    $/Solve
Codex                  82%       $0.71
Claude                 86%       $1.52

Quality
Claude +4pp vs Codex
95% CI: -7pp → +14pp

No conclusive quality difference.

Recommendation: Codex
Reason: lowest cost among statistically
indistinguishable configurations.
```

## Development

```bash
# Clone
git clone https://github.com/felipesousa/repobench.git
cd repobench

# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Build package
python -m build

# Publish to PyPI
twine upload dist/*
```

## License

Apache 2.0
