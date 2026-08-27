#!/usr/bin/env python3
"""
scripts/simulate_workload.py
----------------------------
Standalone Workload Simulator CLI for generating background traffic on PostgreSQL.

Simulates both:
1. Analytical SELECT queries -> populating `pg_stat_statements` for workload extraction.
2. Parameterized DML statements (INSERT, UPDATE, DELETE) -> driving `advisor_write_stats`
   for write penalty estimation.

Usage::

    # Run 50 random DMLs and 10 analytical SELECTs
    python scripts/simulate_workload.py --rounds 50

    # Run only DML writes
    python scripts/simulate_workload.py --rounds 50 --no-reads

    # Run in sequential mode through all templates
    python scripts/simulate_workload.py --mode sequential
"""

import argparse
import logging
import os
import random
import sys
from pathlib import Path

# Add project src to sys.path
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

import psycopg2
from dotenv import load_dotenv

# Add scripts dir to sys.path
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from dml_runner import DMLWorkloadRunner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("WorkloadSimulator")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Standalone Workload Simulator for Auto Index Selector"
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=50,
        help="Number of DML statements to execute in random mode (default: 50)"
    )
    parser.add_argument(
        "--mode",
        choices=["random", "sequential"],
        default="random",
        help="Execution mode: random or sequential (default: random)"
    )
    _DEFAULT_DML = str(Path(__file__).resolve().parent / "queries" / "dml")
    _DEFAULT_READS = str(Path(__file__).resolve().parent / "queries" / "reads")

    parser.add_argument(
        "--dml-dir",
        type=str,
        default=_DEFAULT_DML,
        help="Directory containing DML SQL templates (default: scripts/queries/dml)"
    )
    parser.add_argument(
        "--reads-dir",
        type=str,
        default=_DEFAULT_READS,
        help="Directory containing read SQL templates (default: scripts/queries/reads)"
    )
    parser.add_argument(
        "--no-reads",
        action="store_true",
        help="Disable executing read queries (DML writes only)"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Per-statement timeout in seconds (default: 30s, matching MTP)"
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="RNG seed for deterministic parameter generation (default: 42)"
    )
    return parser.parse_args()


def run_read_queries(conn, reads_dir: Path, timeout_s: int = 30, limit: int = 10) -> int:
    """Execute a batch of read queries to populate pg_stat_statements."""
    if not reads_dir.exists():
        return 0

    sql_files = sorted(list(reads_dir.glob("*.sql")))
    if not sql_files:
        return 0

    selected_files = sql_files[:limit]
    executed = 0

    for f in selected_files:
        try:
            with open(f, "r") as fp:
                sql = fp.read().strip()
            if not sql:
                continue

            with conn.cursor() as cur:
                if timeout_s > 0:
                    cur.execute(f"SET statement_timeout = {int(timeout_s * 1000)};")
                cur.execute(sql)
                cur.fetchmany(10)
            conn.commit()
            executed += 1
        except Exception as e:
            conn.rollback()
            logger.debug("Read query %s failed or timed out: %s", f.name, e)

    return executed


def main():
    args = parse_args()
    load_dotenv()

    dbname = os.getenv("DB_NAME", "tpch_db")
    user = os.getenv("DB_USER", "postgres")
    password = os.getenv("DB_PASSWORD", "123")
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")

    logger.info("Connecting to PostgreSQL [%s:%s/%s as %s]...", host, port, dbname, user)

    try:
        conn = psycopg2.connect(
            dbname=dbname,
            user=user,
            password=password,
            host=host,
            port=port
        )
    except Exception as e:
        logger.error("Failed to connect to PostgreSQL: %s", e)
        sys.exit(1)

    try:
        # 1. Execute Read Queries (populates pg_stat_statements)
        if not args.no_reads:
            reads_dir = Path(args.reads_dir)
            if not reads_dir.is_absolute():
                reads_dir = Path(__file__).resolve().parent.parent / reads_dir
            logger.info("Executing analytical read queries from %s...", reads_dir.name)
            read_count = run_read_queries(conn, reads_dir, timeout_s=args.timeout)
            logger.info("✓ Executed %d read queries (recorded in pg_stat_statements)", read_count)

        # 2. Execute DML Statements (populates advisor_write_stats)
        dml_dir = Path(args.dml_dir)
        if not dml_dir.is_absolute():
            dml_dir = Path(__file__).resolve().parent.parent / dml_dir

        logger.info("Starting DML simulation: mode=%s, rounds=%d, timeout=%ds, seed=%d",
                    args.mode, args.rounds, args.timeout, args.seed)

        runner = DMLWorkloadRunner(
            conn=conn,
            dml_dir=str(dml_dir),
            mode=args.mode,
            rounds=args.rounds,
            seed=args.seed,
            statement_timeout_ms=args.timeout * 1000 if args.timeout > 0 else 0
        )

        dml_count = runner.run()
        logger.info("✓ DML Simulation finished (%d statements executed, recorded in advisor_write_stats).", dml_count)

        logger.info("=" * 60)
        logger.info("Simulation complete! PostgreSQL is now loaded with active traffic.")
        logger.info("=" * 60)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
