"""Shadow mode: adaptive evaluated alongside hard_caps (PRD §77 Phase B).

A mixed repository (small PRs plus one 600-LOC PR that hard_caps rejects
TASK_TOO_LARGE) proves the contract: hard_caps keeps controlling storage and
summaries, the adaptive outcome is reduced to a reported delta, and the delta
counts the PRs the old caps were suppressing.
"""

from __future__ import annotations

import json
from pathlib import Path

from repobench.cli.services import analyze_repository, persist_candidates
from repobench.config import RepoBenchConfig
from repobench.core.types import RejectionCode, TaskStatus
from repobench.mining.shadow import compute_shadow_delta
from repobench.storage.db import Storage
from repobench.core.types import CandidateInfo, TaskStatus as TS
from tests.fixtures.gitutil import git, make_pr


def _lines(count: int) -> str:
    return "value = 1\n" * count


def _mixed_repo(tmp_path: Path) -> Path:
    """Small PRs that clear hard_caps plus one big PR that only adaptive
    surfaces (the PRD §1 field-test shape)."""
    repo = tmp_path / "mixed"
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("demo\n")
    git(repo, "init", "--quiet", "--initial-branch=main")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--quiet", "-m", "initial")
    for number, size in ((1, 30), (2, 60)):
        make_pr(
            repo,
            number,
            f"feat/grow-{number}",
            {
                f"src/a/grow{number}.py": _lines(size),
                f"tests/test_grow{number}.py": _lines(12),
            },
            f"fix: correct bulk import totals ({number})",
        )
    make_pr(
        repo,
        3,
        "feat/big",
        {
            "src/payments/big1.py": _lines(200),
            "src/payments/big2.py": _lines(200),
            "src/payments/big3.py": _lines(200),
            "tests/test_big.py": _lines(40),
        },
        "fix: correct bulk import totals (3)",
    )
    return repo


def _shadow_cfg() -> RepoBenchConfig:
    cfg = RepoBenchConfig()
    cfg.task_mining.size_policy = "shadow"
    return cfg


def test_shadow_keeps_hard_caps_controlling_and_reports_delta(tmp_path: Path) -> None:
    repo = _mixed_repo(tmp_path)
    outcome = analyze_repository(repo, _shadow_cfg())

    # Controlling: the persisted candidates are hard_caps decisions — the big
    # PR #3 stays FILTERED TASK_TOO_LARGE exactly as under the legacy default.
    by_number = {c.pr.number: c for c in outcome.candidates}
    assert by_number[3].status is TaskStatus.FILTERED
    assert by_number[3].rejection_code is RejectionCode.TASK_TOO_LARGE
    assert by_number[1].status is TaskStatus.DISCOVERED

    # Delta: adaptive surfaces exactly the big PR, recovered from a size cap.
    delta = outcome.shadow_delta
    assert delta is not None
    assert delta.controlling_policy == "hard_caps"
    assert delta.hard_caps_pool == 2  # PRs 1-2
    assert delta.adaptive_pool == 3  # PRs 1-3
    assert delta.additional_candidates == 1
    assert delta.recovered_from_size_caps == 1
    assert delta.by_complexity == {"large": 1}  # P100-ish in this tiny universe
    assert sum(delta.adaptive_class_counts.values()) == 3

    # Persisted rows follow the controlling pass — no shadow candidate leaks.
    storage = Storage(tmp_path / "unused.db")  # not used; assertion via outcome
    persist_candidates(storage, outcome.candidates)
    stored = {c.pr.number: c for c in storage.list_candidates()}
    assert stored[3].rejection_code is RejectionCode.TASK_TOO_LARGE


def test_shadow_delta_pure_math_on_hand_built_candidates() -> None:
    def cand(number: int, status: TS, code: RejectionCode | None = None) -> CandidateInfo:
        from repobench.core.types import Assessment, PRInfo

        return CandidateInfo(
            candidate_id=f"c{number}",
            pr=PRInfo(number=number),
            status=status,
            rejection_code=code,
            assessment=Assessment(),
        )

    hard = [
        cand(1, TS.DISCOVERED),
        cand(2, TS.FILTERED, RejectionCode.TASK_TOO_LARGE),
        cand(3, TS.FILTERED, RejectionCode.NO_TEST_CHANGE),
    ]
    adaptive = [
        cand(1, TS.DISCOVERED),
        cand(2, TS.DISCOVERED),
        cand(3, TS.FILTERED, RejectionCode.NO_TEST_CHANGE),
    ]
    delta = compute_shadow_delta(hard, adaptive)
    assert delta.additional_candidates == 1  # only PR 2
    assert delta.recovered_from_size_caps == 1  # its rejection was size-class
    assert delta.hard_caps_pool == 1 and delta.adaptive_pool == 2


def test_shadow_cli_renders_block_and_telemetry(
    tmp_path: Path, monkeypatch
) -> None:
    from typer.testing import CliRunner

    from repobench.cli.app import app

    repo = _mixed_repo(tmp_path / "cli")
    monkeypatch.chdir(repo)
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--yes"]).exit_code == 0
    cfg_path = repo / "repobench.yml"
    cfg = RepoBenchConfig.load(cfg_path)
    cfg.task_mining.size_policy = "shadow"
    cfg.save(cfg_path)

    result = runner.invoke(app, ["analyze"])
    assert result.exit_code == 0, result.output
    assert "Shadow mode — adaptive mining evaluated alongside hard_caps" in result.output
    assert "Additional candidates" in result.output
    assert "recovered from size caps" in result.output

    telemetry = json.loads(
        (repo / ".repobench" / "telemetry" / "analyze.json").read_text()
    )
    assert telemetry["shadow"]["additional_candidates"] == 1
    assert telemetry["shadow"]["controlling_policy"] == "hard_caps"
    # repo identity never enters the telemetry artifact (PRD §58)
    assert "mixed" not in json.dumps(telemetry)
