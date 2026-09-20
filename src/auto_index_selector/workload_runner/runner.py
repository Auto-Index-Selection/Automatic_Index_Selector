"""
workload_runner/runner.py
-------------------------
Unified query runner engine for simulating analytical and DML workloads.
"""

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import psycopg2
from dotenv import load_dotenv
import tomllib

from .param_generator import ParameterGenerator

_MODULE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _MODULE_DIR.parent.parent.parent
DEFAULT_CONFIG_PATH = _MODULE_DIR / "config.toml"


def load_runner_config(config_path: Optional[Union[str, Path]] = None) -> dict:
    """Load configuration from workload_runner/config.toml or specified path."""
    target = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not target.exists():
        return {}
    with open(target, "rb") as f:
        return tomllib.load(f)


class WorkloadRunner:
    """Unified engine to execute workload read and DML queries against PostgreSQL."""

    def __init__(
        self,
        config_dict: Optional[dict] = None,
        config_path: Optional[Union[str, Path]] = None,
    ) -> None:
        base_cfg = load_runner_config(config_path)
        if config_dict:
            base_cfg.update(config_dict)
        self.config = base_cfg

        self.database: str = self.config.get("database", "tpch_db")
        self.iterations: int = int(self.config.get("iterations", 1))
        self.execute_dml: bool = bool(self.config.get("execute_dml", False))
        self.statement_timeout_ms: int = int(self.config.get("statement_timeout_ms", 30000))
        self.param_gen = ParameterGenerator()

        self.queries_dir = self._resolve_queries_dir(self.config.get("queries_path", "workloads/tpch"))
        self.read_files, self.dml_files = self._discover_queries(self.queries_dir)

    def _resolve_queries_dir(self, raw_path: str) -> Path:
        """Resolve queries_path relative to workload_runner, repo root, or as absolute path."""
        p = Path(raw_path)
        if p.is_absolute() and p.exists():
            return p

        # 1. Check relative to workload_runner/
        cand1 = _MODULE_DIR / p
        if cand1.exists():
            return cand1

        # 2. Check if name was just "tpch" or "pgbench" under workloads/
        cand2 = _MODULE_DIR / "workloads" / p
        if cand2.exists():
            return cand2

        # 3. Check relative to project root
        cand3 = _PROJECT_ROOT / p
        if cand3.exists():
            return cand3

        raise FileNotFoundError(
            f"Workload queries directory not found: '{raw_path}'. "
            f"Searched in: {cand1}, {cand2}, {cand3}"
        )

    def _discover_queries(self, queries_dir: Path) -> Tuple[List[Path], List[Path]]:
        """Discover read and companion DML SQL files."""
        read_dir = queries_dir / "reads"
        dml_dir = queries_dir / "dml"

        reads: List[Path] = []
        if read_dir.is_dir():
            reads = sorted(read_dir.glob("*.sql"))
        else:
            # Flat directory containing .sql queries
            reads = sorted([f for f in queries_dir.glob("*.sql") if not f.name.startswith("dml")])

        dml: List[Path] = []
        if dml_dir.is_dir():
            dml = sorted(dml_dir.glob("*.sql"))

        return reads, dml

    def _connect(self):
        """Create a dedicated connection using .env credentials and configured database."""
        load_dotenv()
        return psycopg2.connect(
            dbname=self.database,
            user=os.getenv("DB_USER", "postgres"),
            password=os.getenv("DB_PASSWORD", ""),
            host=os.getenv("DB_HOST", "localhost"),
            port=os.getenv("DB_PORT", "5432"),
        )

    def run(self, conn=None) -> Dict[str, Any]:
        """Execute the queries continuously for the configured iterations."""
        close_on_exit = False
        if conn is None:
            conn = self._connect()
            close_on_exit = True

        print(f"[WorkloadRunner] Target database: '{self.database}'")
        print(f"[WorkloadRunner] Loaded {len(self.read_files)} read queries from {self.queries_dir}")
        print(f"[WorkloadRunner] Executing {self.iterations} iterations continuously...")

        # Set statement timeout if requested
        if self.statement_timeout_ms > 0:
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SET statement_timeout = {int(self.statement_timeout_ms)};")
                conn.commit()
            except Exception as e:
                conn.rollback()

        executed_reads = 0
        executed_dml = 0

        try:
            for it in range(1, self.iterations + 1):
                round_reads = 0
                for sql_file in self.read_files:
                    sql = sql_file.read_text(encoding="utf-8").strip()
                    if not sql:
                        continue
                    try:
                        with conn.cursor() as cur:
                            cur.execute(sql)
                            cur.fetchall()
                        conn.commit()
                        round_reads += 1
                        executed_reads += 1
                    except Exception as e:
                        conn.rollback()
                        print(f"  [WorkloadRunner] Warning: {sql_file.name} failed: {e}")

                # Optional DML per round
                if self.execute_dml and self.dml_files:
                    round_dml = 0
                    for dml_file in self.dml_files:
                        sql = dml_file.read_text(encoding="utf-8").strip()
                        if not sql:
                            continue
                        params = self.param_gen.generate(dml_file.stem)
                        try:
                            with conn.cursor() as cur:
                                if params is not None:
                                    cur.execute(sql, params)
                                else:
                                    cur.execute(sql)
                            conn.commit()
                            round_dml += 1
                            executed_dml += 1
                        except Exception as e:
                            conn.rollback()
                            print(f"  [WorkloadRunner] Warning: DML {dml_file.name} failed: {e}")

            print(
                f"[WorkloadRunner] Completed {self.iterations} iterations: "
                f"{executed_reads} reads, {executed_dml} DML statements executed."
            )
            return {
                "iterations": self.iterations,
                "executed_reads": executed_reads,
                "executed_dml": executed_dml,
                "read_queries_count": len(self.read_files),
                "dml_queries_count": len(self.dml_files),
            }
        finally:
            if close_on_exit and conn:
                conn.close()


def main():
    """Standalone CLI entry point for testing and executing workloads."""
    print("=== Auto Index Selector: Standalone Workload Runner ===")
    runner = WorkloadRunner()
    runner.run()


if __name__ == "__main__":
    main()
