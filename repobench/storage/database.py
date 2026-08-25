"""SQLite database for RepoBench state persistence."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

_DB_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pull_requests (
    pr_number       INTEGER PRIMARY KEY,
    title           TEXT NOT NULL,
    body            TEXT,
    author          TEXT NOT NULL,
    author_type     TEXT,
    labels          TEXT,  -- JSON array
    merged_at       TEXT,
    merge_sha       TEXT,
    base_sha        TEXT,
    head_sha        TEXT,
    changed_files   TEXT,  -- JSON array
    additions       INTEGER DEFAULT 0,
    deletions       INTEGER DEFAULT 0,
    linked_issue_number  INTEGER,
    linked_issue_body    TEXT,
    linked_issue_created_at TEXT,
    merge_commit_sha TEXT,
    head_commit_sha  TEXT,
    task_type       TEXT,
    task_type_confidence REAL DEFAULT 0.0,
    subsystem       TEXT DEFAULT 'unknown',
    complexity      TEXT DEFAULT 'medium',
    implementation_loc  INTEGER DEFAULT 0,
    implementation_files INTEGER DEFAULT 0,
    test_loc        INTEGER DEFAULT 0,
    test_files      INTEGER DEFAULT 0,
    languages       TEXT,  -- JSON array
    directories     TEXT,  -- JSON array
    status          TEXT DEFAULT 'discovered',
    rejection_reason TEXT,
    candidate_id    TEXT,
    pr_json         TEXT   -- full PR JSON for reconstructing
);

CREATE TABLE IF NOT EXISTS candidates (
    candidate_id        TEXT PRIMARY KEY,
    pr_number           INTEGER NOT NULL,
    pr_title            TEXT DEFAULT '',
    base_sha            TEXT DEFAULT '',
    gold_sha            TEXT DEFAULT '',
    merge_commit_sha    TEXT,
    head_commit_sha     TEXT,
    task_type           TEXT DEFAULT 'unknown',
    task_type_confidence REAL DEFAULT 0.0,
    subsystem           TEXT DEFAULT 'unknown',
    complexity          TEXT DEFAULT 'medium',
    implementation_loc  INTEGER DEFAULT 0,
    implementation_files INTEGER DEFAULT 0,
    test_loc            INTEGER DEFAULT 0,
    test_files          INTEGER DEFAULT 0,
    instruction_source  TEXT DEFAULT '',
    instruction_provenance TEXT,
    instruction_text    TEXT,
    status              TEXT DEFAULT 'discovered',
    rejection_reason    TEXT,
    eligibility_history     INTEGER DEFAULT 0,
    eligibility_instruction INTEGER DEFAULT 0,
    eligibility_verifier   INTEGER DEFAULT 0,
    eligibility_environment INTEGER,
    eligibility_oracle     INTEGER,
    eligibility_determinism INTEGER,
    eligibility_leakage    INTEGER,
    leakage_risk       REAL DEFAULT 0.0,
    leakage_warnings   TEXT,  -- JSON array
    network_isolation  TEXT DEFAULT 'NONE',
    created_at          TEXT,
    candidate_json      TEXT
);

CREATE TABLE IF NOT EXISTS benchmarks (
    benchmark_id            TEXT PRIMARY KEY,
    repository_remote       TEXT DEFAULT '',
    repository_private      INTEGER DEFAULT 1,
    created_at              TEXT,
    workload_window_days    INTEGER DEFAULT 180,
    workload_window_prs     INTEGER DEFAULT 0,
    health_overall          INTEGER DEFAULT 0,
    health_representativeness INTEGER DEFAULT 0,
    health_validation       INTEGER DEFAULT 0,
    health_leakage          INTEGER DEFAULT 0,
    health_recency          INTEGER DEFAULT 0,
    health_diversity        INTEGER DEFAULT 0,
    coverage_warnings       TEXT,  -- JSON array
    benchmark_json          TEXT
);

CREATE TABLE IF NOT EXISTS benchmark_tasks (
    benchmark_id    TEXT NOT NULL,
    task_id         TEXT NOT NULL,
    candidate_id    TEXT NOT NULL,
    PRIMARY KEY (benchmark_id, task_id),
    FOREIGN KEY (benchmark_id) REFERENCES benchmarks(benchmark_id),
    FOREIGN KEY (candidate_id) REFERENCES candidates(candidate_id)
);

CREATE TABLE IF NOT EXISTS agent_configs (
    config_name TEXT PRIMARY KEY,
    agent       TEXT NOT NULL,
    model       TEXT,
    reasoning   TEXT,
    extra_json  TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    benchmark_id    TEXT NOT NULL,
    agent_config    TEXT NOT NULL,
    agent_version   TEXT DEFAULT '',
    model_name      TEXT DEFAULT '',
    harbor_version  TEXT DEFAULT '',
    created_at      TEXT,
    FOREIGN KEY (benchmark_id) REFERENCES benchmarks(benchmark_id)
);

CREATE TABLE IF NOT EXISTS trials (
    trial_id            TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL,
    benchmark_id        TEXT NOT NULL,
    task_id             TEXT NOT NULL,
    agent_config        TEXT NOT NULL,
    solved              INTEGER DEFAULT 0,
    duration_ms         INTEGER,
    prompt_tokens       INTEGER,
    completion_tokens   INTEGER,
    cost_usd            REAL,
    verifier_task       INTEGER,
    verifier_regression INTEGER,
    error               TEXT,
    created_at          TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(run_id),
    FOREIGN KEY (benchmark_id, task_id) REFERENCES benchmark_tasks(benchmark_id, task_id)
);
"""


class Database:
    """SQLite persistence layer for RepoBench."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def initialize(self) -> None:
        """Create tables if they don't exist."""
        self.conn.executescript(_SCHEMA)
        self._set_meta("db_version", str(_DB_VERSION))

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── Meta ───────────────────────────────────────────────────────────────

    def _get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        self.conn.commit()

    # ── Pull Requests ──────────────────────────────────────────────────────

    def upsert_pr(self, data: dict[str, Any]) -> None:
        pr_number = data["pr_number"]
        labels_json = json.dumps(data.get("labels", []))
        files_json = json.dumps(data.get("changed_files", []))
        langs_json = json.dumps(data.get("languages", []))
        dirs_json = json.dumps(data.get("directories", []))

        self.conn.execute(
            """INSERT OR REPLACE INTO pull_requests
            (pr_number, title, body, author, author_type, labels, merged_at,
             merge_sha, base_sha, head_sha, changed_files, additions, deletions,
             linked_issue_number, linked_issue_body, linked_issue_created_at,
             merge_commit_sha, head_commit_sha,
             task_type, task_type_confidence, subsystem, complexity,
             implementation_loc, implementation_files, test_loc, test_files,
             languages, directories, status, rejection_reason, candidate_id, pr_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                pr_number,
                data.get("title", ""),
                data.get("body"),
                data.get("author", ""),
                data.get("author_type"),
                labels_json,
                data.get("merged_at"),
                data.get("merge_sha"),
                data.get("base_sha"),
                data.get("head_sha"),
                files_json,
                data.get("additions", 0),
                data.get("deletions", 0),
                data.get("linked_issue_number"),
                data.get("linked_issue_body"),
                data.get("linked_issue_created_at"),
                data.get("merge_commit_sha"),
                data.get("head_commit_sha"),
                data.get("task_type", "unknown"),
                data.get("task_type_confidence", 0.0),
                data.get("subsystem", "unknown"),
                data.get("complexity", "medium"),
                data.get("implementation_loc", 0),
                data.get("implementation_files", 0),
                data.get("test_loc", 0),
                data.get("test_files", 0),
                langs_json,
                dirs_json,
                data.get("status", "discovered"),
                data.get("rejection_reason"),
                data.get("candidate_id"),
                data.get("pr_json"),
            ),
        )
        self.conn.commit()

    def get_pr(self, pr_number: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM pull_requests WHERE pr_number = ?", (pr_number,)
        ).fetchone()
        return dict(row) if row else None

    def get_all_prs(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM pull_requests ORDER BY pr_number").fetchall()
        return [dict(r) for r in rows]

    def count_prs(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) as cnt FROM pull_requests").fetchone()
        return row["cnt"] if row else 0

    # ── Candidates ─────────────────────────────────────────────────────────

    def upsert_candidate(self, data: dict[str, Any]) -> None:
        cand_id = data["candidate_id"]
        leakage_warnings_json = json.dumps(data.get("leakage_warnings", []))
        elig = data.get("eligibility", {})
        created = data.get("created_at")
        if isinstance(created, datetime):
            created = created.isoformat()

        self.conn.execute(
            """INSERT OR REPLACE INTO candidates
            (candidate_id, pr_number, pr_title, base_sha, gold_sha,
             merge_commit_sha, head_commit_sha,
             task_type, task_type_confidence, subsystem, complexity,
             implementation_loc, implementation_files, test_loc, test_files,
             instruction_source, instruction_provenance, instruction_text,
             status, rejection_reason,
             eligibility_history, eligibility_instruction, eligibility_verifier,
             eligibility_environment, eligibility_oracle, eligibility_determinism,
             eligibility_leakage,
             leakage_risk, leakage_warnings, network_isolation,
             created_at, candidate_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                cand_id,
                data.get("pr_number", 0),
                data.get("pr_title", ""),
                data.get("base_sha", ""),
                data.get("gold_sha", ""),
                data.get("merge_commit_sha"),
                data.get("head_commit_sha"),
                data.get("task_type", "unknown"),
                data.get("task_type_confidence", 0.0),
                data.get("subsystem", "unknown"),
                data.get("complexity", "medium"),
                data.get("implementation_loc", 0),
                data.get("implementation_files", 0),
                data.get("test_loc", 0),
                data.get("test_files", 0),
                data.get("instruction_source", ""),
                data.get("instruction_provenance"),
                data.get("instruction_text"),
                data.get("status", "discovered"),
                data.get("rejection_reason"),
                1 if elig.get("history") else 0,
                1 if elig.get("instruction") else 0,
                1 if elig.get("verifier") else 0,
                1 if elig.get("environment") else (None if elig.get("environment") is None else 0),
                1 if elig.get("oracle") else (None if elig.get("oracle") is None else 0),
                1 if elig.get("determinism") else (None if elig.get("determinism") is None else 0),
                1 if elig.get("leakage") else (None if elig.get("leakage") is None else 0),
                data.get("leakage_risk", 0.0),
                leakage_warnings_json,
                data.get("network_isolation", "NONE"),
                created,
                data.get("candidate_json"),
            ),
        )
        self.conn.commit()

    def get_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_candidates_by_status(self, status: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM candidates WHERE status = ? ORDER BY candidate_id", (status,)
        ).fetchall()
        return [dict(r) for r in rows]

    def count_candidates_by_status(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) as cnt FROM candidates GROUP BY status"
        ).fetchall()
        return {r["status"]: r["cnt"] for r in rows}

    # ── Benchmarks ─────────────────────────────────────────────────────────

    def upsert_benchmark(self, data: dict[str, Any]) -> None:
        warnings_json = json.dumps(data.get("coverage_warnings", []))
        health = data.get("health", {})
        created = data.get("created_at")
        if isinstance(created, datetime):
            created = created.isoformat()

        self.conn.execute(
            """INSERT OR REPLACE INTO benchmarks
            (benchmark_id, repository_remote, repository_private, created_at,
             workload_window_days, workload_window_prs,
             health_overall, health_representativeness, health_validation,
             health_leakage, health_recency, health_diversity,
             coverage_warnings, benchmark_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data.get("benchmark_id", ""),
                data.get("repository_remote", ""),
                1 if data.get("repository_private", True) else 0,
                created,
                data.get("workload_window_days", 180),
                data.get("workload_window_prs", 0),
                health.get("overall", 0),
                health.get("representativeness", 0),
                health.get("validation", 0),
                health.get("leakage", 0),
                health.get("recency", 0),
                health.get("diversity", 0),
                warnings_json,
                data.get("benchmark_json"),
            ),
        )
        self.conn.commit()

    def upsert_benchmark_task(self, benchmark_id: str, task_id: str, candidate_id: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO benchmark_tasks (benchmark_id, task_id, candidate_id) VALUES (?,?,?)",
            (benchmark_id, task_id, candidate_id),
        )
        self.conn.commit()

    def get_benchmark_tasks(self, benchmark_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM benchmark_tasks WHERE benchmark_id = ? ORDER BY task_id",
            (benchmark_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Agent Configs ──────────────────────────────────────────────────────

    def upsert_agent_config(self, data: dict[str, Any]) -> None:
        extra_json = json.dumps(data.get("extra", {}))
        self.conn.execute(
            """INSERT OR REPLACE INTO agent_configs
            (config_name, agent, model, reasoning, extra_json)
            VALUES (?,?,?,?,?)""",
            (
                data.get("config_name", ""),
                data.get("agent", ""),
                data.get("model"),
                data.get("reasoning"),
                extra_json,
            ),
        )
        self.conn.commit()

    def get_agent_config(self, config_name: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM agent_configs WHERE config_name = ?", (config_name,)
        ).fetchone()
        return dict(row) if row else None

    def list_agent_configs(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM agent_configs ORDER BY config_name").fetchall()
        return [dict(r) for r in rows]

    # ── Runs & Trials ──────────────────────────────────────────────────────

    def insert_run(self, data: dict[str, Any]) -> None:
        created = data.get("created_at")
        if isinstance(created, datetime):
            created = created.isoformat()
        self.conn.execute(
            """INSERT OR REPLACE INTO runs
            (run_id, benchmark_id, agent_config, agent_version, model_name, harbor_version, created_at)
            VALUES (?,?,?,?,?,?,?)""",
            (
                data.get("run_id", ""),
                data.get("benchmark_id", ""),
                data.get("agent_config", ""),
                data.get("agent_version", ""),
                data.get("model_name", ""),
                data.get("harbor_version", ""),
                created,
            ),
        )
        self.conn.commit()

    def insert_trial(self, data: dict[str, Any]) -> None:
        created = data.get("created_at")
        if isinstance(created, datetime):
            created = created.isoformat()
        verifier = data.get("verifier", {})
        self.conn.execute(
            """INSERT OR REPLACE INTO trials
            (trial_id, run_id, benchmark_id, task_id, agent_config,
             solved, duration_ms, prompt_tokens, completion_tokens, cost_usd,
             verifier_task, verifier_regression, error, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data.get("trial_id", ""),
                data.get("run_id", ""),
                data.get("benchmark_id", ""),
                data.get("task_id", ""),
                data.get("agent_config", ""),
                1 if data.get("solved") else 0,
                data.get("duration_ms"),
                data.get("prompt_tokens"),
                data.get("completion_tokens"),
                data.get("cost_usd"),
                1 if verifier.get("task") else (0 if verifier.get("task") is False else None),
                1
                if verifier.get("regression")
                else (0 if verifier.get("regression") is False else None),
                data.get("error"),
                created,
            ),
        )
        self.conn.commit()

    def get_trials_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM trials WHERE run_id = ? ORDER BY task_id", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_trials_for_benchmark(self, benchmark_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM trials WHERE benchmark_id = ? ORDER BY agent_config, task_id",
            (benchmark_id,),
        ).fetchall()
        return [dict(r) for r in rows]
