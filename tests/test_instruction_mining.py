"""Unit tests for instruction, leakage, and candidate mining."""

from __future__ import annotations

from datetime import datetime, timezone

from repobench.models import (
    RepoBenchConfig,
    CandidateTask,
    InstructionProvenance,
    PullRequest,
    RejectionReason,
    TaskStatus,
)
from repobench.tasks.instruction import extract_instruction
from repobench.tasks.leakage import scan_instruction_leakage
from repobench.mining.candidates import discover_candidates, _is_automated_maintenance


def make_pr(**kwargs) -> PullRequest:
    defaults = dict(
        pr_number=1,
        title="Fix bug",
        body=None,
        author="alice",
        labels=[],
        merged_at=datetime.now(timezone.utc),
        base_sha="base123",
        head_sha="head456",
        changed_files=["src/app.py", "tests/test_app.py"],
        additions=100,
        deletions=20,
    )
    defaults.update(kwargs)
    return PullRequest(**defaults)


class TestInstructionExtraction:
    def test_tier_a_linked_issue(self):
        pr = make_pr(
            body="Closes #10",
            linked_issue_number=10,
            linked_issue_body="Users see a crash when saving empty invoices.",
            linked_issue_created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        text, prov, conf = extract_instruction(pr)
        assert prov == InstructionProvenance.TIER_A
        assert "crash" in text
        assert conf >= 0.85

    def test_tier_b_pr_body(self):
        pr = make_pr(body="## Problem\nPayments fail when amount is zero.")
        text, prov, conf = extract_instruction(pr)
        assert prov == InstructionProvenance.TIER_B
        assert "amount is zero" in text

    def test_tier_c_title_only(self):
        pr = make_pr(title="fix: correct invoice total")
        text, prov, conf = extract_instruction(pr)
        assert prov == InstructionProvenance.TIER_C
        assert "invoice total" in text
        assert conf <= 0.6


class TestLeakageScanner:
    def test_detects_new_identifier(self):
        risk, warnings = scan_instruction_leakage(
            "Create IdempotencyWebhookRegistry class",
            gold_files={"src/registry.py": "class IdempotencyWebhookRegistry:"},
            base_files={"src/registry.py": ""},
        )
        assert risk > 0
        assert any("POTENTIAL_SOLUTION_LEAKAGE" in w for w in warnings)

    def test_no_leakage_for_base_identifier(self):
        risk, warnings = scan_instruction_leakage(
            "Fix the Invoice model",
            gold_files={"src/invoice.py": "class Invoice:"},
            base_files={"src/invoice.py": "class Invoice:"},
        )
        assert risk == 0.0
        assert warnings == []

    def test_empty_instruction(self):
        risk, warnings = scan_instruction_leakage("")
        assert risk == 0.0
        assert warnings == []


class TestCandidateDiscovery:
    def test_discovers_with_test_change(self):
        from repobench.repository.workload import build_workload_info

        pr = make_pr(labels=["bug"])
        info = build_workload_info(pr)
        cfg = RepoBenchConfig()
        candidates = discover_candidates([info], cfg)
        assert len(candidates) == 1
        assert candidates[0].status == TaskStatus.DISCOVERED
        assert candidates[0].eligibility.history is True

    def test_rejects_no_test_change(self):
        from repobench.repository.workload import build_workload_info

        pr = make_pr(changed_files=["src/app.py"])
        info = build_workload_info(pr)
        cfg = RepoBenchConfig()
        candidates = discover_candidates([info], cfg)
        assert candidates[0].rejection_reason == RejectionReason.NO_TEST_CHANGE
        assert candidates[0].status == TaskStatus.FILTERED

    def test_rejects_oversized(self):
        from repobench.repository.workload import build_workload_info

        pr = make_pr(additions=5000, deletions=100)
        info = build_workload_info(pr)
        cfg = RepoBenchConfig()
        candidates = discover_candidates([info], cfg)
        assert candidates[0].status == TaskStatus.FILTERED
        assert candidates[0].rejection_reason == RejectionReason.TASK_TOO_LARGE

    def test_automated_maintenance_detected(self):
        pr = make_pr(author="dependabot[bot]", title="Bump requests from 2.28 to 2.31")
        assert _is_automated_maintenance(pr)

    def test_automated_maintenance_lockfile_only(self):
        pr = make_pr(
            author="alice",
            title="Update lockfile",
            changed_files=["pnpm-lock.yaml"],
        )
        assert _is_automated_maintenance(pr)
