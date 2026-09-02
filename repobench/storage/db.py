"""SQLite persistence (PRD §114). Thin layer: domain modules stay pure, the CLI orchestrates persistence."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterator

from repobench.core.types import CandidateInfo, TrialResult, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates(
  candidate_id TEXT PRIMARY KEY,
  pr_number INTEGER,
  status TEXT,
  rejection_code TEXT,
  task_type TEXT,
  subsystem TEXT,
  complexity TEXT,
  data_json TEXT,
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS tasks(
  task_id TEXT PRIMARY KEY,
  candidate_id TEXT,
  version INTEGER,
  status TEXT,
  package_path TEXT,
  data_json TEXT,
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS task_validations(
  id INTEGER PRIMARY KEY,
  task_id TEXT,
  kind TEXT,
  result TEXT,
  details_json TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS benchmarks(
  benchmark_id TEXT PRIMARY KEY,
  created_at TEXT,
  size INTEGER,
  health_json TEXT,
  manifest_path TEXT,
  methodology_version TEXT
);
CREATE TABLE IF NOT EXISTS benchmark_tasks(
  benchmark_id TEXT,
  task_id TEXT,
  position INTEGER,
  PRIMARY KEY(benchmark_id, task_id)
);
CREATE TABLE IF NOT EXISTS execution_targets(
  target_id TEXT PRIMARY KEY,
  definition_json TEXT,
  fingerprint_json TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY,
  benchmark_id TEXT,
  status TEXT,
  config_json TEXT,
  started_at TEXT,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS trials(
  trial_id TEXT PRIMARY KEY,
  run_id TEXT,
  benchmark_id TEXT,
  task_id TEXT,
  target_id TEXT,
  rollout INTEGER DEFAULT 1,
  outcome TEXT,
  data_json TEXT,
  created_at TEXT
);
"""


class Storage:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_schema(self) -> None:
        with closing(self.connect()) as conn, conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """In-place migrations for databases created by older versions.

        Issue #13: trials.rollout was added in wave 2; databases from before it
        keep working through the DEFAULT 1 backfill (data_json stays the source
        of truth for reads).
        """
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(trials)")}
        if "rollout" not in columns:
            conn.execute("ALTER TABLE trials ADD COLUMN rollout INTEGER DEFAULT 1")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with closing(self.connect()) as conn, conn:
            yield conn

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with closing(self.connect()) as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self.tx() as conn:
            conn.execute(sql, params)

    def upsert(self, table: str, data: dict[str, Any]) -> None:
        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        self.execute(
            f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({placeholders})",
            tuple(data.values()),
        )

    # -- candidates --

    def save_candidate(self, candidate: CandidateInfo) -> None:
        now = utcnow().isoformat()
        self.upsert("candidates", {
            "candidate_id": candidate.candidate_id,
            "pr_number": candidate.pr.number,
            "status": candidate.status.value,
            "rejection_code": candidate.rejection_code.value if candidate.rejection_code else None,
            "task_type": candidate.assessment.task_type.value,
            "subsystem": candidate.assessment.subsystem,
            "complexity": candidate.assessment.complexity.value,
            "data_json": candidate.model_dump_json(),
            "created_at": now,
            "updated_at": now,
        })

    def list_candidates(self, status: str | None = None) -> list[CandidateInfo]:
        if status:
            rows = self.query(
                "SELECT data_json FROM candidates WHERE status = ? ORDER BY pr_number",
                (status,),
            )
        else:
            rows = self.query("SELECT data_json FROM candidates ORDER BY pr_number")
        return [CandidateInfo.model_validate_json(r["data_json"]) for r in rows]

    # -- tasks --

    def save_task(
        self,
        task_id: str,
        data: dict[str, Any],
        *,
        candidate_id: str | None = None,
        version: int = 1,
        status: str = "VALIDATING",
        package_path: str | None = None,
    ) -> None:
        now = utcnow().isoformat()
        existing = self.query("SELECT created_at FROM tasks WHERE task_id = ?", (task_id,))
        self.upsert("tasks", {
            "task_id": task_id,
            "candidate_id": candidate_id,
            "version": version,
            "status": status,
            "package_path": package_path,
            "data_json": json.dumps(data),
            "created_at": existing[0]["created_at"] if existing else now,
            "updated_at": now,
        })

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        rows = self.query("SELECT data_json FROM tasks WHERE task_id = ?", (task_id,))
        if not rows:
            return None
        return json.loads(rows[0]["data_json"])

    def list_tasks(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.query("SELECT data_json FROM tasks WHERE status = ?", (status,))
        else:
            rows = self.query("SELECT data_json FROM tasks")
        return [json.loads(r["data_json"]) for r in rows]

    def task_ids_with_status(self, status: str) -> list[str]:
        """Task ids holding a status — reuse lookups (issue #16) need no data_json."""
        rows = self.query("SELECT task_id FROM tasks WHERE status = ?", (status,))
        return [r["task_id"] for r in rows]

    def save_validation(
        self, task_id: str, kind: str, result: str, details_json: str | None = None
    ) -> None:
        # Append log (PRD §114): validation history is never rewritten, so a plain
        # INSERT — not an upsert — even when the same task/kind is re-validated.
        self.execute(
            "INSERT INTO task_validations (task_id, kind, result, details_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, kind, result, details_json, utcnow().isoformat()),
        )

    # -- benchmarks --

    def save_benchmark(
        self,
        benchmark_id: str,
        size: int,
        health_json: str | None,
        manifest_path: str | None,
        methodology_version: str,
    ) -> None:
        self.upsert("benchmarks", {
            "benchmark_id": benchmark_id,
            "created_at": utcnow().isoformat(),
            "size": size,
            "health_json": health_json,
            "manifest_path": manifest_path,
            "methodology_version": methodology_version,
        })

    def get_benchmark(self, benchmark_id: str) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM benchmarks WHERE benchmark_id = ?", (benchmark_id,))
        return rows[0] if rows else None

    def list_benchmarks(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM benchmarks ORDER BY created_at DESC")

    def save_benchmark_task(self, benchmark_id: str, task_id: str, position: int) -> None:
        self.upsert("benchmark_tasks", {
            "benchmark_id": benchmark_id,
            "task_id": task_id,
            "position": position,
        })

    def benchmark_task_ids(self, benchmark_id: str) -> list[str]:
        rows = self.query(
            "SELECT task_id FROM benchmark_tasks WHERE benchmark_id = ? ORDER BY position",
            (benchmark_id,),
        )
        return [r["task_id"] for r in rows]

    # -- runs & trials --

    def create_run(self, run_id: str, benchmark_id: str, config_json: str | None = None) -> None:
        self.upsert("runs", {
            "run_id": run_id,
            "benchmark_id": benchmark_id,
            "status": "RUNNING",
            "config_json": config_json,
            "started_at": utcnow().isoformat(),
            "finished_at": None,
        })

    def finish_run(self, run_id: str, status: str = "COMPLETED") -> None:
        self.execute(
            "UPDATE runs SET status = ?, finished_at = ? WHERE run_id = ?",
            (status, utcnow().isoformat(), run_id),
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        return rows[0] if rows else None

    def list_runs(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM runs ORDER BY started_at DESC")

    def save_trial(self, trial: TrialResult) -> None:
        # Usage already lives inside data_json (usage field of TrialResult) — no
        # separate usage_records write (PRD §53-54).
        self.upsert("trials", {
            "trial_id": trial.trial_id,
            "run_id": trial.run_id,
            "benchmark_id": trial.benchmark_id,
            "task_id": trial.task_id,
            "target_id": trial.target_id,
            "rollout": trial.rollout,
            "outcome": trial.outcome.value,
            "data_json": trial.model_dump_json(),
            "created_at": utcnow().isoformat(),
        })

    def get_trial(self, trial_id: str) -> TrialResult | None:
        rows = self.query("SELECT data_json FROM trials WHERE trial_id = ?", (trial_id,))
        return TrialResult.model_validate_json(rows[0]["data_json"]) if rows else None

    def list_trials(self, run_id: str | None = None) -> list[TrialResult]:
        if run_id:
            rows = self.query(
                "SELECT data_json FROM trials WHERE run_id = ? ORDER BY created_at", (run_id,)
            )
        else:
            rows = self.query("SELECT data_json FROM trials ORDER BY created_at")
        return [TrialResult.model_validate_json(r["data_json"]) for r in rows]

    def trials_by_run(self) -> dict[str, dict[str, dict[str, int]]]:
        """run_id -> target_id -> {n, solved, timeouts, errors} in one grouped query."""
        rows = self.query(
            "SELECT run_id, target_id, outcome, COUNT(*) AS n FROM trials "
            "GROUP BY run_id, target_id, outcome"
        )
        result: dict[str, dict[str, dict[str, int]]] = {}
        for row in rows:
            if not row["run_id"]:
                continue
            target = result.setdefault(row["run_id"], {}).setdefault(
                row["target_id"], {"n": 0, "solved": 0, "timeouts": 0, "errors": 0}
            )
            target["n"] += row["n"]
            if row["outcome"] == "SOLVED":
                target["solved"] += row["n"]
            elif row["outcome"] == "TIMEOUT":
                target["timeouts"] += row["n"]
            elif row["outcome"] in ("HARNESS_ERROR", "SETUP_ERROR", "VERIFIER_ERROR"):
                target["errors"] += row["n"]
        return result

    # -- targets --

    def save_target(
        self, target_id: str, definition_json: str, fingerprint_json: str | None = None
    ) -> None:
        self.upsert("execution_targets", {
            "target_id": target_id,
            "definition_json": definition_json,
            "fingerprint_json": fingerprint_json,
            "created_at": utcnow().isoformat(),
        })

    def get_target(self, target_id: str) -> dict[str, Any] | None:
        """Decoded definition_json of a registered target, or None (PRD §26/§29)."""
        rows = self.query(
            "SELECT definition_json FROM execution_targets WHERE target_id = ?", (target_id,)
        )
        if not rows or not rows[0]["definition_json"]:
            return None
        try:
            decoded = json.loads(rows[0]["definition_json"])
        except json.JSONDecodeError:
            return None
        return decoded if isinstance(decoded, dict) else None
