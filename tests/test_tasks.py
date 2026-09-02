"""Tests for task packaging: diff split (PRD §73), instruction rendering (§71-72),
package reconstruction (§36) and leakage scan (§87)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fixtures.gitutil import build_empty_base_repo, build_repo, git, make_candidate
from repobench.core import gitutil
from repobench.core.errors import ReconstructionError, RepoBenchError
from repobench.core.ids import new_task_id
from repobench.core.types import (
    Complexity,
    TaskMetadata,
    TaskPackage,
    TaskStatus,
    TaskType,
)
from repobench.tasks.instruction import render_instruction
from repobench.tasks.leakage import LeakageReport, scan_base_archive
from repobench.tasks.package import write_package
from repobench.tasks.reconstruction import build_task_package
from repobench.tasks.verifier import split_diff


# ------------------------------------------------------------------ split_diff

MIXED_DIFF = (
    "diff --git a/calculator.py b/calculator.py\n"
    "index 3f2a1bc..9c4de77 100644\n"
    "--- a/calculator.py\n"
    "+++ b/calculator.py\n"
    "@@ -2,7 +2,7 @@\n"
    "-        if x % 2 == 1:  # bug: picks odd numbers\n"
    "+        if x % 2 == 0:\n"
    "diff --git a/tests/test_sum_even.py b/tests/test_sum_even.py\n"
    "new file mode 100644\n"
    "index 0000000..8a23bc4\n"
    "--- /dev/null\n"
    "+++ b/tests/test_sum_even.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def test_sum_even():\n"
    "+    assert True\n"
    "diff --git a/tests/old_test.py b/tests/old_test.py\n"
    "deleted file mode 100644\n"
    "index 9aa4b21..0000000\n"
    "--- a/tests/old_test.py\n"
    "+++ /dev/null\n"
    "@@ -1,2 +0,0 @@\n"
    "-def test_old():\n"
    "-    assert True\n"
)

TEST_ONLY_DIFF = "".join(
    line + "\n" for line in MIXED_DIFF.splitlines() if "calculator.py" not in line
)

IMPL_ONLY_DIFF = (
    "diff --git a/calculator.py b/calculator.py\n"
    "index 3f2a1bc..9c4de77 100644\n"
    "--- a/calculator.py\n"
    "+++ b/calculator.py\n"
    "@@ -2,7 +2,7 @@\n"
    "-        if x % 2 == 1:\n"
    "+        if x % 2 == 0:\n"
)


class TestSplitDiff:
    def test_mixed_diff_routes_chunks_by_test_path(self) -> None:
        result = split_diff(MIXED_DIFF)
        assert result.implementation_files == ["calculator.py"]
        assert result.verifier_files == ["tests/test_sum_even.py", "tests/old_test.py"]
        assert "calculator.py" in result.implementation_patch
        assert "test_sum_even" not in result.implementation_patch
        assert "tests/test_sum_even.py" in result.verifier_patch
        assert "tests/old_test.py" in result.verifier_patch
        assert "calculator.py" not in result.verifier_patch
        assert result.unsafe_reason is None

    def test_chunks_preserve_original_order(self) -> None:
        result = split_diff(MIXED_DIFF)
        assert result.verifier_patch.index("test_sum_even") < result.verifier_patch.index(
            "old_test.py"
        )

    def test_new_test_file_uses_b_side_and_deletion_falls_back_to_a_side(self) -> None:
        result = split_diff(MIXED_DIFF)
        # +++ b/tests/test_sum_even.py (b side present)
        assert "tests/test_sum_even.py" in result.verifier_files
        # +++ /dev/null for the deletion -> classified from --- a/tests/old_test.py
        assert "tests/old_test.py" in result.verifier_files

    def test_falls_back_to_diff_git_header(self) -> None:
        diff = "diff --git a/core.py b/test_core.py\n"
        result = split_diff(diff)
        assert result.implementation_files == []
        assert result.verifier_files == ["test_core.py"]

    def test_empty_implementation_is_unsafe(self) -> None:
        result = split_diff(TEST_ONLY_DIFF)
        assert result.implementation_patch == ""
        assert result.implementation_files == []
        assert result.verifier_files == ["tests/test_sum_even.py", "tests/old_test.py"]
        assert result.unsafe_reason == "empty_implementation"

    def test_empty_verifier_is_unsafe(self) -> None:
        result = split_diff(IMPL_ONLY_DIFF)
        assert result.verifier_patch == ""
        assert result.implementation_files == ["calculator.py"]
        assert result.unsafe_reason == "empty_verifier"

    def test_empty_diff_is_unsafe(self) -> None:
        result = split_diff("")
        assert result.implementation_patch == ""
        assert result.verifier_patch == ""
        assert result.unsafe_reason == "empty_implementation"


# -------------------------------------------------------- render_instruction


def test_render_instruction_contains_title_and_text(tmp_path: Path) -> None:
    fx = build_repo(tmp_path)
    candidate = make_candidate(fx)
    text = render_instruction(candidate)
    assert candidate.pr.title in text
    assert candidate.assessment.instruction in text
    assert "bugfix" in text  # task type
    assert "math" in text  # subsystem
    assert text.lstrip().startswith("# ")


def test_render_instruction_falls_back_to_pr_body(tmp_path: Path) -> None:
    fx = build_repo(tmp_path)
    candidate = make_candidate(fx)
    candidate.assessment.instruction = ""
    text = render_instruction(candidate)
    assert candidate.pr.body in text


# --------------------------------------------------------- build_task_package


def test_build_task_package_full_layout(tmp_path: Path) -> None:
    fx = build_repo(tmp_path)
    candidate = make_candidate(fx)
    out_dir = tmp_path / "packages" / "one"
    package = build_task_package(fx["repo"], candidate, out_dir)

    for path in (
        package.base_tar,
        package.instruction_md,
        package.gold_patch,
        package.verifier_patch,
        package.metadata_json,
    ):
        assert path.is_file()

    gold = package.gold_patch.read_text()
    verifier = package.verifier_patch.read_text()
    assert "diff --git a/calculator.py b/calculator.py" in gold
    assert "test_sum_even" not in gold
    assert "diff --git a/tests/test_sum_even.py b/tests/test_sum_even.py" in verifier
    assert "calculator.py" not in verifier

    instruction = package.instruction_md.read_text()
    assert candidate.pr.title in instruction
    assert candidate.assessment.instruction in instruction

    metadata = TaskMetadata.model_validate_json(package.metadata_json.read_text())
    assert metadata.task_id.startswith("t_9_")
    assert metadata.task_id == new_task_id(9, fx["base_sha"], fx["merge_sha"])
    assert metadata.status == TaskStatus.VALIDATING
    assert metadata.rejection_code is None
    assert metadata.assessment == candidate.assessment
    assert metadata.assessment.task_type == TaskType.BUGFIX
    assert metadata.assessment.subsystem == "math"
    assert metadata.assessment.complexity == Complexity.SMALL
    assert metadata.assessment.language == "python"
    assert metadata.assessment.instruction == candidate.assessment.instruction
    assert metadata.assessment.instruction_confidence == "B"
    assert metadata.assessment.instruction_source == "pr_body"
    assert metadata.assessment.implementation_loc == 2
    assert metadata.assessment.test_loc == 7
    assert metadata.assessment.implementation_files == 1
    assert metadata.assessment.test_files == 1
    assert metadata.base_sha == fx["base_sha"]
    assert metadata.gold_sha == fx["merge_sha"]
    assert metadata.pr_number == 9
    assert metadata.created_at == candidate.pr.merged_at
    assert metadata.package_dir == str(out_dir)

    loaded = TaskPackage.load(out_dir)
    assert loaded.task_id == metadata.task_id
    assert loaded.metadata.model_dump(mode="json") == metadata.model_dump(mode="json")


def test_build_task_package_rejects_missing_history(tmp_path: Path) -> None:
    fx = build_repo(tmp_path)
    candidate = make_candidate(fx)
    candidate.pr.base_sha = None
    with pytest.raises(ReconstructionError, match="history not reconstructable"):
        build_task_package(fx["repo"], candidate, tmp_path / "p1")


def test_build_task_package_rejects_unknown_base_sha(tmp_path: Path) -> None:
    fx = build_repo(tmp_path)
    candidate = make_candidate(fx)
    candidate.pr.base_sha = "0" * 40
    with pytest.raises(ReconstructionError, match="history not reconstructable"):
        build_task_package(fx["repo"], candidate, tmp_path / "p2")


# ------------------------------------------------------- empty-tree base (issue #33)


def test_tree_is_empty_flags_only_the_empty_tree_commit(tmp_path: Path) -> None:
    built = build_empty_base_repo(tmp_path)
    repo = built["repo"]
    assert gitutil.tree_is_empty(repo, built["empty_fx"]["base_sha"]) is True
    # the healthy PR's base and every later commit carry a real tree
    assert gitutil.tree_is_empty(repo, built["empty_fx"]["merge_sha"]) is False
    assert gitutil.tree_is_empty(repo, built["healthy_fx"]["merge_sha"]) is False


def test_build_task_package_rejects_empty_tree_base(tmp_path: Path) -> None:
    built = build_empty_base_repo(tmp_path)
    candidate = make_candidate(built["empty_fx"])
    with pytest.raises(ReconstructionError, match="empty tree") as excinfo:
        build_task_package(built["repo"], candidate, tmp_path / "pkg")
    # a typed RepoBenchError, never a bare RuntimeError or tarfile error
    assert isinstance(excinfo.value, RepoBenchError)
    assert not isinstance(excinfo.value, RuntimeError)
    # the message names the PR and explains why it cannot become a task
    message = str(excinfo.value)
    assert f"PR #{candidate.pr.number}" in message
    assert "adds the whole repository" in message


def test_load_rejects_corrupt_metadata(tmp_path: Path) -> None:
    # No silent downgrade: a package with unparseable metadata.json is unusable.
    fx = build_repo(tmp_path)
    out_dir = tmp_path / "pkg"
    build_task_package(fx["repo"], make_candidate(fx), out_dir)
    (out_dir / "metadata.json").write_text("{not json")
    with pytest.raises(RepoBenchError, match="metadata.json.*corrupt"):
        TaskPackage.load(out_dir)


# ------------------------------------------------------------- package layout


def test_write_package_copies_base_and_returns_handle(tmp_path: Path) -> None:
    fx = build_repo(tmp_path)
    src_tar = tmp_path / "src" / "base.tar"
    assert gitutil.archive_commit(fx["repo"], fx["base_sha"], src_tar)
    metadata = TaskMetadata(
        task_id="t_1_abcdef01", base_sha=fx["base_sha"], gold_sha=fx["merge_sha"]
    )
    package = write_package(
        tmp_path / "pkg",
        base_tar=src_tar,
        instruction="do the thing",
        gold_patch="GOLD",
        verifier_patch="VERIFIER",
        metadata=metadata,
    )
    assert package.task_id == "t_1_abcdef01"
    assert package.base_tar.read_bytes() == src_tar.read_bytes()
    assert src_tar.is_file()  # copied, not moved
    assert package.instruction_md.read_text() == "do the thing"
    assert package.gold_patch.read_text() == "GOLD"
    assert package.verifier_patch.read_text() == "VERIFIER"
    assert package.metadata.task_id == "t_1_abcdef01"
    assert TaskMetadata.model_validate_json(package.metadata_json.read_text()).task_id == "t_1_abcdef01"


def test_write_package_accepts_base_tar_already_in_place(tmp_path: Path) -> None:
    fx = build_repo(tmp_path)
    dest = tmp_path / "pkg"
    dest.mkdir(parents=True)
    assert gitutil.archive_commit(fx["repo"], fx["base_sha"], dest / "base.tar")
    metadata = TaskMetadata(task_id="t_2_abcdef01", base_sha="a", gold_sha="b")
    package = write_package(
        dest,
        base_tar=dest / "base.tar",  # already archived into place: no copy needed
        instruction="i",
        gold_patch="g",
        verifier_patch="v",
        metadata=metadata,
    )
    assert package.base_tar.is_file()


# -------------------------------------------------------------------- leakage


def test_scan_base_archive_clean_scores_78(tmp_path: Path) -> None:
    fx = build_repo(tmp_path)
    package = build_task_package(fx["repo"], make_candidate(fx), tmp_path / "pkg")
    report = scan_base_archive(package.metadata, package.base_tar)
    assert set(report.checks) == {
        "history_isolation",
        "gold_isolation",
        "verifier_isolation",
        "github_credentials",
        "network_isolation",
    }
    assert report.checks["network_isolation"] is False
    assert all(report.checks[key] for key in report.checks if key != "network_isolation")
    assert report.findings == []
    assert report.score == 78  # 100 - 22: no network sandbox in host-native V1


def test_scan_base_archive_detects_pr_reference(tmp_path: Path) -> None:
    fx = build_repo(tmp_path, readme="# calc\n\nSee #9 for the original discussion.\n")
    package = build_task_package(fx["repo"], make_candidate(fx), tmp_path / "pkg")
    report = scan_base_archive(package.metadata, package.base_tar)
    assert report.findings and "README.md" in report.findings[0]
    assert report.score == 78 - 15


def test_scan_base_archive_detects_gold_sha_prefix(tmp_path: Path) -> None:
    fx = build_repo(tmp_path)
    package = build_task_package(fx["repo"], make_candidate(fx), tmp_path / "pkg")
    leaked_sha = "cafe1234cafe1234cafe1234cafe1234cafe1234"
    (fx["repo"] / "NOTES.txt").write_text("internal build id: cafe1234cafe1234\n")
    git(fx["repo"], "add", "-A")
    git(fx["repo"], "commit", "--quiet", "--no-gpg-sign", "-m", "add notes")
    notes_base = git(fx["repo"], "rev-parse", "HEAD")
    assert gitutil.archive_commit(fx["repo"], notes_base, tmp_path / "leaky.tar")

    # The needles derive from the metadata: a gold SHA whose 12-char prefix
    # appears in the archive is a leak.
    metadata = package.metadata.model_copy(update={"gold_sha": leaked_sha})
    report = scan_base_archive(metadata, tmp_path / "leaky.tar")
    assert report.findings and "NOTES.txt" in report.findings[0]
    assert report.score == 78 - 15


def test_leakage_report_model_shape() -> None:
    report = LeakageReport(
        checks={"history_isolation": True, "network_isolation": False},
        score=78,
        findings=[],
    )
    assert report.score == 78
