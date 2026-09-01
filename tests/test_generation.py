"""Tier-D instruction generation tests (PRD §71-72, opt-in): the anti-solution
prompt, the deterministic validator, generation through a fake command target,
the config surface, and the build_benchmark integration (success, failure
fallback, tier filter)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from textwrap import dedent

import pytest
from typer.testing import CliRunner

from repobench.cli.app import app
from repobench.config import (
    InstructionGenerationConfig,
    ProjectConfig,
    RepoBenchConfig,
)
from repobench.core.ids import sha256_hex
from repobench.core.types import ExecutionTarget, TaskPackage
from repobench.storage.db import Storage
from repobench.tasks.generation import (
    build_generation_prompt,
    generate_instruction,
    validate_generated_instruction,
)
from repobench.tasks.reconstruction import build_task_package
from tests.fixtures.gitutil import build_repo, make_candidate

runner = CliRunner()


def _invoke(*args: str):
    return runner.invoke(app, list(args))


# ----------------------------------------------------------------- fixtures

# A patch whose added lines carry long content and snake_case identifiers, so
# both validator leak rules can actually fire.
LEAKY_PATCH = (
    "diff --git a/pipeline.py b/pipeline.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/pipeline.py\n"
    "+++ b/pipeline.py\n"
    "@@ -1,3 +1,4 @@\n"
    "+    summary_rows = build_summary_rows(input_rows)\n"
    "+    return render_table(summary_rows, title=\"summary\")\n"
)

CLEAN_INSTRUCTION = (
    "The exported summary can disagree with the underlying records for some "
    "inputs. The task is to make the reported output match the documented "
    "expectations in every supported case, keeping unrelated behavior stable."
)

# Written inside the PR branch so the fixture gold patch has code-like
# identifiers and long added lines (render_summary, format_heading, ...).
REPORT_MODULE = (
    'def render_summary(rows):\n'
    '    heading = format_heading("summary")\n'
    '    return f"{heading}\\n" + "\\n".join(rows)\n'
)


def _write_script(tmp_path: Path, name: str, body: str) -> Path:
    script = tmp_path / name
    script.write_text(dedent(body).lstrip())
    return script


def _command_target(name: str, script: Path) -> ExecutionTarget:
    return ExecutionTarget(
        id=name, harness="command", command=[sys.executable, str(script), "{prompt_file}"]
    )


def _package(tmp_path: Path, **build_repo_kwargs):
    fx = build_repo(tmp_path, **build_repo_kwargs)
    return fx, build_task_package(fx["repo"], make_candidate(fx), tmp_path / "pkg")


# ---------------------------------------------------- build_generation_prompt


class TestBuildGenerationPrompt:
    def test_prompt_contains_title_patch_and_anti_solution_rules(self, tmp_path: Path) -> None:
        prompt = build_generation_prompt(
            "totals are wrong for mixed input", LEAKY_PATCH, ["pipeline.py"]
        )
        assert "totals are wrong for mixed input" in prompt
        assert "build_summary_rows(input_rows)" in prompt  # implementation patch included
        assert "- pipeline.py" in prompt  # changed file list included
        # The anti-solution rules are stated.
        assert "Do NOT name any function, variable, identifier" in prompt
        assert "never the diff itself" in prompt
        assert "Do NOT quote, copy, or paraphrase any code" in prompt

    def test_prompt_never_contains_verifier_content(self, tmp_path: Path) -> None:
        _fx, package = _package(tmp_path)
        gold = package.gold_patch.read_text()
        verifier = package.verifier_patch.read_text()
        assert verifier, "fixture must have a hidden verifier patch"
        prompt = build_generation_prompt("some title", gold, ["calculator.py"])
        assert "x % 2 == 0" in prompt  # the gold patch is the intended input
        # The hidden answer key never enters the prompt: by construction only
        # package.gold_patch is fed in.
        assert "test_sum_even" not in prompt
        for line in verifier.splitlines():
            if len(line.strip()) >= 10:  # skip diff noise ('+', blanks)
                assert line not in prompt


# ------------------------------------------- validate_generated_instruction


class TestValidateGeneratedInstruction:
    def test_quoting_an_added_patch_line_is_a_violation(self) -> None:
        text = (
            "The current code path is "
            "return render_table(summary_rows, title=\"summary\")"
            " and the exported view must match the underlying records."
        )
        violations = validate_generated_instruction(text, LEAKY_PATCH)
        assert violations
        assert "quotes an added patch line" in violations[0]

    def test_mentioning_a_snake_case_identifier_is_a_violation(self) -> None:
        text = (
            "The pipeline must build its output even when "
            "build_summary_rows receives an unusual period selection."
        )
        violations = validate_generated_instruction(text, LEAKY_PATCH)
        assert violations
        assert "build_summary_rows" in violations[0]

    def test_clean_symptom_description_has_no_violations(self) -> None:
        violations = validate_generated_instruction(CLEAN_INSTRUCTION, LEAKY_PATCH)
        assert violations == []

    def test_plain_english_words_do_not_trigger_the_identifier_rule(self) -> None:
        text = (
            "The function that assembles the exported summary table should "
            "tolerate empty input and keep the column ordering stable when "
            "values repeat across repeated runs."
        )
        assert validate_generated_instruction(text, LEAKY_PATCH) == []

    def test_length_bounds_are_enforced(self) -> None:
        assert len(validate_generated_instruction("too short", LEAKY_PATCH)) == 1
        assert validate_generated_instruction("x" * 80, LEAKY_PATCH) == []
        assert validate_generated_instruction("x" * 4000, LEAKY_PATCH) == []
        assert len(validate_generated_instruction("x" * 4001, LEAKY_PATCH)) == 1


# ------------------------------------------------------- generate_instruction


class TestGenerateInstruction:
    def _candidate_and_package(self, tmp_path: Path):
        fx = build_repo(tmp_path, extra_files={"report.py": REPORT_MODULE})
        candidate = make_candidate(fx)
        package = build_task_package(fx["repo"], candidate, tmp_path / "pkg")
        return candidate, package

    def _cfg(self) -> InstructionGenerationConfig:
        return InstructionGenerationConfig(enabled=True, target="gen", timeout_minutes=1)

    def test_clean_generator_yields_instruction_and_metadata(self, tmp_path: Path) -> None:
        candidate, package = self._candidate_and_package(tmp_path)
        script = _write_script(
            tmp_path,
            "clean_gen.py",
            f"""
            import sys
            from pathlib import Path

            prompt = Path(sys.argv[1]).read_text()
            assert "Implementation diff" in prompt
            assert "render_summary" in prompt  # the gold patch is the input
            print({CLEAN_INSTRUCTION!r})
            """,
        )
        outcome = generate_instruction(
            candidate, package, _command_target("gen", script), cfg=self._cfg()
        )
        assert outcome.text == CLEAN_INSTRUCTION
        assert outcome.violations == []
        assert outcome.failed_reason is None
        assert outcome.attempts == 1
        assert outcome.metadata["target"] == "gen"
        assert outcome.metadata["harness"] == "command"
        assert outcome.attempts == outcome.metadata["attempts"]
        assert len(outcome.metadata["generation_prompt_sha256"]) == 64
        assert (
            outcome.metadata["generation_prompt_sha256"]
            == sha256_hex(
                build_generation_prompt(
                    candidate.pr.title,
                    package.gold_patch.read_text(),
                    ["calculator.py", "report.py"],
                )
            )
        )

    def test_structured_json_output_result_field_is_used(self, tmp_path: Path) -> None:
        candidate, package = self._candidate_and_package(tmp_path)
        script = _write_script(
            tmp_path,
            "json_gen.py",
            f"""
            import json, sys

            print(json.dumps({{"result": {CLEAN_INSTRUCTION!r}}}))
            """,
        )
        outcome = generate_instruction(
            candidate, package, _command_target("gen", script), cfg=self._cfg()
        )
        assert outcome.text == CLEAN_INSTRUCTION
        assert outcome.attempts == 1

    def test_contaminated_output_is_rejected_after_one_retry(self, tmp_path: Path) -> None:
        candidate, package = self._candidate_and_package(tmp_path)
        contaminated = (
            "Fix the report: render_summary must call format_heading so the "
            "heading renders exactly as the current implementation produces it."
        )
        script = _write_script(
            tmp_path,
            "contaminated_gen.py",
            f"""
            print({contaminated!r})
            """,
        )
        outcome = generate_instruction(
            candidate, package, _command_target("gen", script), cfg=self._cfg()
        )
        assert outcome.text is None
        assert outcome.failed_reason is None
        assert outcome.attempts == 2  # initial attempt + 1 retry
        assert outcome.violations
        assert any(
            "render_summary" in violation or "format_heading" in violation
            for violation in outcome.violations
        )

    def test_failing_generator_records_failed_reason(self, tmp_path: Path) -> None:
        candidate, package = self._candidate_and_package(tmp_path)
        script = _write_script(
            tmp_path,
            "failing_gen.py",
            """
            import sys

            print("boom", file=sys.stderr)
            sys.exit(1)
            """,
        )
        outcome = generate_instruction(
            candidate, package, _command_target("gen", script), cfg=self._cfg()
        )
        assert outcome.text is None
        assert outcome.attempts == 1  # hard execution failure: no retry
        assert outcome.failed_reason is not None
        assert "exit code 1" in outcome.failed_reason

    def test_spawn_failure_records_failed_reason(self, tmp_path: Path) -> None:
        candidate, package = self._candidate_and_package(tmp_path)
        target = ExecutionTarget(
            id="gen",
            harness="command",
            command=["definitely-not-a-real-binary-repobench", "{prompt_file}"],
        )
        outcome = generate_instruction(candidate, package, target, cfg=self._cfg())
        assert outcome.text is None
        assert outcome.attempts == 1
        assert outcome.failed_reason is not None
        assert "spawn failed" in outcome.failed_reason


# ----------------------------------------------------------------- config


def test_config_roundtrip_instruction_generation(tmp_path: Path) -> None:
    cfg = RepoBenchConfig()
    cfg.targets["gen"] = ExecutionTarget(
        id="gen", harness="command", command=["my-agent", "{prompt_file}"]
    )
    cfg.instruction_generation = InstructionGenerationConfig(
        enabled=True, target="gen", timeout_minutes=2
    )
    cfg.benchmark.allowed_confidences = ["A", "B"]
    path = tmp_path / "repobench.yml"
    cfg.save(path)
    loaded = RepoBenchConfig.load(path)
    assert loaded.instruction_generation.enabled is True
    assert loaded.instruction_generation.target == "gen"
    assert loaded.instruction_generation.timeout_minutes == 2
    assert loaded.benchmark.allowed_confidences == ["A", "B"]

    # Defaults keep current behavior: generation off, every tier allowed.
    fresh = RepoBenchConfig()
    assert fresh.instruction_generation.enabled is False
    assert fresh.instruction_generation.target == ""
    assert fresh.instruction_generation.timeout_minutes == 5
    assert fresh.benchmark.allowed_confidences is None


# --------------------------------------------------- build_benchmark wiring


def _configure(
    repo: Path,
    *,
    generator: list[str] | None,
    gen_enabled: bool = True,
    allowed: list[str] | None = None,
) -> None:
    """Simulate the user editing repobench.yml after `repobench init`."""
    cfg = RepoBenchConfig.load(repo / "repobench.yml")
    pytest_cmd = f'"{sys.executable}" -m pytest -q'
    cfg.project = ProjectConfig(
        language="python",
        test_command=pytest_cmd,
        regression_command=pytest_cmd,
    )
    target_id = "missing-target"
    if generator is not None:
        cfg.targets["gen"] = ExecutionTarget(
            id="gen", harness="command", command=generator
        )
        target_id = "gen"
    cfg.instruction_generation = InstructionGenerationConfig(
        enabled=gen_enabled, target=target_id
    )
    if allowed is not None:
        cfg.benchmark.allowed_confidences = allowed
    cfg.save(repo / "repobench.yml")


def _init_and_analyze(fixture_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(fixture_repo)
    assert _invoke("init", "--yes").exit_code == 0
    analyzed = _invoke("analyze")
    assert analyzed.exit_code == 0, analyzed.output
    assert "No inference tokens were consumed." in analyzed.output


def _benchmark_task_id(storage: Storage) -> str:
    benchmark_id = storage.list_benchmarks()[0]["benchmark_id"]
    task_ids = storage.benchmark_task_ids(benchmark_id)
    assert len(task_ids) == 1
    return task_ids[0]


CLEAN_GENERATOR_BODY = f"""
import sys
from pathlib import Path

prompt = Path(sys.argv[1]).read_text()
assert "Implementation diff" in prompt
# the hidden answer key never reaches the generator
assert "test_sum_even" not in prompt
print({CLEAN_INSTRUCTION!r})
"""


def test_build_generates_tier_d_instruction(
    fixture_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_and_analyze(fixture_repo, monkeypatch)
    script = _write_script(tmp_path, "clean_gen.py", CLEAN_GENERATOR_BODY)
    _configure(fixture_repo, generator=[sys.executable, str(script), "{prompt_file}"])

    built = _invoke("benchmark", "build")
    assert built.exit_code == 0, built.output
    assert "Instruction tiers" in built.output
    assert "D×1" in built.output  # the D presence is always called out

    storage = Storage(fixture_repo / ".repobench" / "state.db")
    candidate = next(c for c in storage.list_candidates() if c.pr.number == 7)
    assert candidate.assessment.instruction_confidence == "D"
    assert candidate.assessment.instruction_source == "llm"
    assert candidate.status.value == "VALID"

    package = TaskPackage.load(
        fixture_repo / ".repobench" / "tasks" / _benchmark_task_id(storage)
    )
    assert package.metadata.assessment.instruction_confidence == "D"
    assert package.metadata.assessment.instruction_source == "llm"
    generation = getattr(package.metadata, "generation", None)
    assert isinstance(generation, dict)
    assert generation["target"] == "gen"
    assert generation["harness"] == "command"
    assert generation["attempts"] >= 1
    assert len(generation["generation_prompt_sha256"]) == 64
    assert "generation_failed" not in package.metadata.model_dump()

    instruction = package.instruction_text()
    assert "The exported summary can disagree" in instruction
    assert candidate.assessment.instruction in instruction
    assert "## Context" in instruction


def test_build_generation_failure_falls_back_to_title(
    fixture_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_and_analyze(fixture_repo, monkeypatch)
    script = _write_script(
        tmp_path,
        "failing_gen.py",
        """
        import sys

        sys.exit(1)
        """,
    )
    _configure(fixture_repo, generator=[sys.executable, str(script), "{prompt_file}"])

    built = _invoke("benchmark", "build")
    assert built.exit_code == 0, built.output
    assert "instruction generation failed" in built.output
    assert "C×1" in built.output  # the title-derived candidate was kept

    storage = Storage(fixture_repo / ".repobench" / "state.db")
    candidate = next(c for c in storage.list_candidates() if c.pr.number == 7)
    assert candidate.assessment.instruction_confidence == "C"
    assert candidate.assessment.instruction_source == "title"
    assert candidate.status.value == "VALID"  # validation proceeded normally

    package = TaskPackage.load(
        fixture_repo / ".repobench" / "tasks" / _benchmark_task_id(storage)
    )
    generation_failed = getattr(package.metadata, "generation_failed", None)
    assert isinstance(generation_failed, dict)
    assert "exit code 1" in (generation_failed["reason"] or "")
    assert generation_failed["attempts"] == 1
    assert "generation" not in package.metadata.model_dump()
    # the instruction is still the title-derived one
    assert candidate.pr.title in package.instruction_text()


def test_build_rejects_generation_target_not_configured(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_and_analyze(fixture_repo, monkeypatch)
    _configure(fixture_repo, generator=None, gen_enabled=True)

    built = _invoke("benchmark", "build")
    assert built.exit_code == 1
    assert "instruction_generation.target" in built.output


def test_allowed_confidences_filters_pool(
    fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_and_analyze(fixture_repo, monkeypatch)
    # Offline fixture: PR #7 is title-derived (C) — an A/B-only filter leaves
    # nothing, and the build refuses before running any validation.
    _configure(
        fixture_repo, generator=None, gen_enabled=False, allowed=["A", "B"]
    )
    built = _invoke("benchmark", "build")
    assert built.exit_code == 1
    assert "allowed_confidences" in built.output
    assert not Storage(fixture_repo / ".repobench" / "state.db").list_benchmarks()

    # The matching-tier case keeps the candidate in the pool.
    _configure(fixture_repo, generator=None, gen_enabled=False, allowed=["C"])
    built = _invoke("benchmark", "build")
    assert built.exit_code == 0, built.output
    assert "C×1" in built.output
