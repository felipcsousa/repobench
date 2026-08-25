"""Unit tests for workload classification and verifier detection."""

from __future__ import annotations

from datetime import datetime, timezone

from repobench.models import PullRequest, TaskType, Complexity
from repobench.repository.workload import (
    classify_task_type,
    detect_subsystem,
    calculate_complexity,
    build_workload_info,
)
from repobench.tasks.verifier import (
    detect_test_files,
    split_gold_verifier,
    has_test_change,
)


def make_pr(
    title: str = "Fix bug in payments",
    labels: list[str] | None = None,
    files: list[str] | None = None,
    body: str | None = None,
) -> PullRequest:
    return PullRequest(
        pr_number=1,
        title=title,
        body=body,
        author="alice",
        labels=labels or [],
        merged_at=datetime.now(timezone.utc),
        base_sha="abc",
        head_sha="def",
        changed_files=files or ["src/payments/invoice.py", "tests/test_invoice.py"],
        additions=100,
        deletions=50,
    )


class TestTaskTypeClassification:
    def test_bug_label_wins(self):
        pr = make_pr(labels=["bug"])
        task_type, conf = classify_task_type(pr)
        assert task_type == TaskType.BUGFIX
        assert conf >= 0.9

    def test_feature_title_detection(self):
        pr = make_pr(title="feat: add payment webhook support", labels=[])
        task_type, conf = classify_task_type(pr)
        assert task_type == TaskType.FEATURE
        assert conf >= 0.6

    def test_unknown_when_no_signal(self):
        pr = make_pr(title="Update README wording", labels=[])
        task_type, conf = classify_task_type(pr)
        assert task_type == TaskType.UNKNOWN
        assert conf == 0.0

    def test_fix_keyword_in_title(self):
        pr = make_pr(title="fix: correct invoice total calculation", labels=[])
        task_type, _ = classify_task_type(pr)
        assert task_type == TaskType.BUGFIX


class TestSubsystemDetection:
    def test_packages_directory(self):
        files = ["packages/payments/src/core.py", "packages/payments/tests/test_core.py"]
        assert detect_subsystem(files) == "payments"

    def test_src_subdirectory(self):
        files = ["src/payments/invoice.ts", "src/payments/invoice.test.ts"]
        assert detect_subsystem(files) == "payments"

    def test_unknown_for_root_files(self):
        assert detect_subsystem(["README.md"]) == "unknown"

    def test_codeowners_preference(self):
        files = ["src/auth/login.py"]
        codeowners = {"src/auth/": ["auth-team"]}
        result = detect_subsystem(files, codeowners)
        assert result == "auth"  # CODEOWNERS pattern takes priority


class TestComplexity:
    def test_small_change(self):
        assert calculate_complexity(20, 1, 0, 10) == Complexity.SMALL

    def test_large_change(self):
        assert calculate_complexity(500, 10, 5, 200) == Complexity.LARGE

    def test_medium_range(self):
        result = calculate_complexity(150, 4, 2, 80)
        assert result in (Complexity.SMALL, Complexity.MEDIUM)


class TestWorkloadInfo:
    def test_build_info(self):
        pr = make_pr(labels=["bug"])
        info = build_workload_info(pr)
        assert info.task_type == TaskType.BUGFIX
        assert info.subsystem == "payments"
        assert info.test_files >= 1
        assert info.languages == ["python"]


class TestVerifier:
    def test_detect_test_files_python(self):
        files = ["src/app.py", "tests/test_app.py", "test_helper.py"]
        result = detect_test_files(files)
        assert "tests/test_app.py" in result
        assert "test_helper.py" in result
        assert "src/app.py" not in result

    def test_detect_test_files_ts(self):
        files = ["src/app.ts", "src/app.test.ts", "src/app.spec.tsx"]
        result = detect_test_files(files)
        assert "src/app.test.ts" in result
        assert "src/app.spec.tsx" in result
        assert "src/app.ts" not in result

    def test_has_test_change(self):
        assert has_test_change(["src/app.py", "tests/test_app.py"])
        assert not has_test_change(["src/app.py", "README.md"])

    def test_split_gold_verifier(self):
        diff = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1 @@
-old
+new
diff --git a/tests/test_app.py b/tests/test_app.py
--- a/tests/test_app.py
+++ b/tests/test_app.py
@@ -1 +1 @@
-old test
+new test
"""
        impl_patch, verifier_patch = split_gold_verifier(diff, ["tests/test_app.py"])
        assert "src/app.py" in impl_patch
        assert "tests/test_app.py" in verifier_patch
        assert "src/app.py" not in verifier_patch
        assert "tests/test_app.py" not in impl_patch
