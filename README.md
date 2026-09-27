# Automatic Index Selector (AIS) for PostgreSQL

An automated, workload-driven database index recommendation system for PostgreSQL. The selector passively observes active query traffic (or executes synthetic benchmarks), analyzes query access patterns using SQL AST parsing, models write maintenance overhead using a buffer-cache-aware B-tree cost model, and recommends the optimal index configuration under user-specified storage and count budgets.

---

## Architecture Overview

The system operates as a modular, decoupled pipeline:

```
┌────────────────────────┐      ┌─────────────────────────┐
│  Workload Ingestion    │ ───▶ │  Candidate Generation   │
│  (Live / Custom DML)   │      │  (Rule-based AST, DTA)  │
└────────────────────────┘      └─────────────────────────┘
            │                                │
            ▼                                ▼
┌────────────────────────┐      ┌─────────────────────────┐
│ Write Penalty Modeler  │ ───▶ │ Configuration Selection │
│ (B-Tree + Buffer Hit)  │      │ (Greedy m,k / Drop / Ex)│
└────────────────────────┘      └─────────────────────────┘
```

1. **Workload Ingestion**: Captures active queries (`SELECT`, `UPDATE`, `DELETE`, `INSERT ... SELECT`) from the PostgreSQL log with parameter-aware execution tracking.
2. **Candidate Generation**: Analyzes predicates, joins, and sorting expressions to synthesize candidate single and composite indexes.
3. **Write Penalty Estimator**: Accurately computes write maintenance costs for index candidates using an analytical B-tree cost model (with HOT update detection and buffer cache modeling).
4. **Configuration Selection**: Searches the candidate space to pick the highest-benefit index configuration subject to constraints (Greedy $(m, k)$, Whang Drop, or Extend).

---

## Prerequisites & PostgreSQL Setup

### 1. PostgreSQL Extensions
The following extensions must be installed in PostgreSQL:

- **[HypoPG](https://hypopg.readthedocs.io/)**: Provides hypothetical indexes for zero-overhead query plan cost estimation.
  ```sql
  CREATE EXTENSION IF NOT EXISTS hypopg;
  ```
- **`advisor_write_stats`**: C extension for column-set level update tracking (HOT vs non-HOT updates). Must be loaded via `shared_preload_libraries` in `postgresql.conf`:
  ```ini
  shared_preload_libraries = 'advisor_write_stats, query_logger'
  ```
- **`query_logger`**: PostgreSQL background worker that records executed queries and parameter values into a structured log file (e.g. `/var/lib/postgresql/16/main/query_logger.log`).

### 2. Python Environment
Requires **Python 3.10+**.

```bash
# 1. Clone repository & navigate to directory
cd Automatic_Index_Selector

# 2. Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt
```

---

## Quick Start

### 1. Configure Connection & Parameters
Edit [`config.toml`](file:///home/pratik/mtp-integration/Automatic_Index_Selector/config.toml) with your database credentials and path to the query logger:

```toml
[database]
host = "localhost"
port = 5432
user = "postgres"
password = "your_password"
dbname = "tpcc_db"

[workload]
log_file = "/var/lib/postgresql/16/main/query_logger.log"
```

### 2. Run the Index Selector
Run directly from the root repository:

```bash
python3 -m auto_index_selector
```

Sample output:
```text
Connection established successfully!
[WritePenalty] Captured write before-snapshot (scale=1.0)
[Observer] Running custom workload runner (pgbench, 5 iterations)...
[Workload] Saved 18 unique queries (total execution weight: 45.0) to workload.json
[WritePenalty] Initialized dynamic penalty evaluator across 4 tables (1200 DML modifications).
[CandidateGen] Generated 14 candidate indexes.
[ConfigSel] Executing selection algorithm (cs_drop)...

Selected Index Configuration:
  CREATE INDEX ON pgbench_accounts(bid, abalance);  [write_penalty = 12.4500]
  CREATE INDEX ON pgbench_history(aid);             [write_penalty = 0.0000]
```

### 3. Programmatic Usage (Python API)
You can embed the selector directly into your Python benchmarks or CI/CD pipelines:

```python
import psycopg2
from auto_index_selector import run_auto_index_selector

# Connect to database
conn = psycopg2.connect("dbname=tpcc_db user=postgres password=123 host=localhost")

# Run index selector pipeline with config overrides
selected_indexes, workload, weights, write_penalties = run_auto_index_selector(
    conn=conn,
    config_override={
        "config_selection": {"module": "config_sel", "k": 5},
        "workload": {"mode": "timer", "timer_seconds": 30},
    },
    verbose=True
)

for table, cols in selected_indexes:
    print(f"Recommended Index: ON {table}({', '.join(cols)})")
```

---

## Configuration Guide (`config.toml`)

All behaviors of the pipeline are controlled via [`config.toml`](file:///home/pratik/mtp-integration/Automatic_Index_Selector/config.toml).

### 1. `[database]`
Defines PostgreSQL connection credentials:

| Option | Type | Default | Description |
|:---|:---:|:---:|:---|
| `host` | `string` | `"localhost"` | PostgreSQL hostname or IP address. |
| `port` | `int` | `5432` | PostgreSQL port number. |
| `user` | `string` | `"postgres"` | Database user. |
| `password` | `string` | `""` | Database password. |
| `dbname` | `string` | `"tpch_db"` | Target database name. |

> **Note**: Any field left empty (`""`) will automatically fall back to environment variables (`DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`) or `.env`.

---

### 2. `[candidate_generation]`
Selects the algorithm used to identify potential index candidates:

```toml
[candidate_generation]
module = "cg_rule_based"
```

| Module | Description | Best For |
|:---|:---|:---|
| `cg_rule_based` *(Recommended)* | Parses SQL ASTs (WHERE, JOIN, ORDER BY) to generate single and composite index permutations. | Production workloads with complex multi-table joins. |
| `cg_auto_admin` | Microsoft AutoAdmin heuristic indexing using single-column candidate pools. | Classic benchmark evaluation. |
| `cg_dta` | Database Tuning Advisor heuristic candidate generator. | Query access pattern matching. |
| `cg_naive` | Simple single-attribute index generator. | Baseline comparisons. |
| `dexter` | Exhaustive multi-column combination candidate generator. | Small schemas with few columns. |

---

### 3. `[config_selection]`
Selects the algorithm and budget constraints used to choose the final index set:

```toml
[config_selection]
module = "cs_drop"
m = 2
k = 10
storage_budget = "inf"
```

| Option | Type | Default | Description |
|:---|:---:|:---:|:---|
| `module` | `string` | `"config_sel"` | Selection algorithm: `"config_sel"`, `"cs_drop"`, or `"cs_extend"`. |
| `m` | `int` | `2` | Exhaustive seed size for `config_sel`, or max group size dropped at once for `cs_drop`. |
| `k` | `int` | `10` | Maximum number of recommended indexes (index count budget for `config_sel`). |
| `storage_budget` | `int` / `string` | `"inf"` | Max index footprint in bytes (e.g. `52428800` for 50 MB, or `"inf"` for unlimited). Used by `cs_drop` and `cs_extend`. |

#### Selection Algorithms:
- **`config_sel` (Greedy $m, k$)**: Chaudhuri & Narasayya greedy enumeration. Exhaustively checks combinations up to size $m$, then greedily expands up to $k$ indexes.
- **`cs_drop` (Whang Drop Heuristic)**: Starts with all candidate indexes hypothetically created and iteratively drops the index (or group of $m$ indexes) that contributes the least benefit until the `storage_budget` is satisfied.
- **`cs_extend` (Extend Algorithm - ICDE 2019)**: Starts empty and iteratively morphs and extends existing indexes (prefix expansion $k \to k + [\text{col}]$) while respecting memory/storage budgets.

---

### 4. `[workload]`
Controls how query workloads are observed and captured:

```toml
[workload]
mode = "timer"
timer_seconds = 60
log_file = "/var/lib/postgresql/16/main/query_logger.log"
workload_output = "workload.json"
```

| Option | Type | Default | Description |
|:---|:---:|:---:|:---|
| `mode` | `string` | `"custom"` | `"timer"` (or `"live"`): Passively monitors live database traffic.<br>`"custom"`: Executes queries using the internal workload runner. |
| `timer_seconds` | `int` | `60` | Number of seconds to observe traffic when `mode = "timer"`. |
| `queries_path` | `string` | `"tpch"` | Workload directory or built-in benchmark (`"tpch"` or `"pgbench"`) for `mode = "custom"`. |
| `iterations` | `int` | `5` | Repetitions/rounds when running custom workloads. |
| `execute_dml` | `boolean` | `true` | If true, companion DML updates/inserts are executed to induce write penalty stats. |
| `log_file` | `string` | *(Mandatory)* | Path to the `query_logger.log` file. If missing or path is invalid, execution aborts immediately. |
| `workload_output` | `string` | `"workload.json"` | Path to write the extracted workload in structured JSON format. |

#### Workload Deduplication & Parameter Sensitivity
Queries observed in `query_logger.log` are parsed and deduplicated **sensitively with their parameter values**:
- If `SELECT * FROM tbl WHERE id = $1` is executed with `$1 = 10` twice, it is saved as one query with `weight = 2.0`.
- If the same query template is executed with different parameters (`$1 = 10` vs `$1 = 99999`), both are kept as distinct entries because different parameter values may yield vastly different cardinalities and execution costs.

---

### 5. `[write_penalty]`
Configures the buffer-cache-aware B-tree write maintenance penalty model:

```toml
[write_penalty]
enabled = true
write_scale = 1.0
window_duration_seconds = 60
```

| Option | Type | Default | Description |
|:---|:---:|:---:|:---|
| `enabled` | `boolean` | `true` | When true, tracks DML activity via `advisor_write_stats` and deducts write penalty from candidate benefit. Set to `false` for read-only optimization. |
| `write_scale` | `float` | `1.0` | Multiplier applied to observed write counts (`1.0` = exact observed writes; `10.0+` = projected write-heavy OLTP spikes). |
| `window_duration_seconds` | `int` | `60` | Observation window duration between write before-and-after snapshots. |

#### Analytical Cost Model
The write penalty estimator models PostgreSQL B-tree overhead as:
- **Internal node traversal**: Handled entirely in `shared_buffers` as CPU operator cost:
  $$\text{traversal\_cost} = (\text{btree\_height} - 1) \times (10 \times \text{cpu\_operator\_cost} + \text{cpu\_index\_tuple\_cost})$$
- **Leaf-level I/O**: Scaled by the cache hit ratio $p_{\text{hit}}$ (auto-detected from `pg_statio_user_indexes`):
  $$\text{effective\_leaf\_cost} = (1 - p_{\text{hit}}) \times \text{random\_page\_cost} + p_{\text{hit}} \times \text{seq\_page\_cost}$$
- **HOT Update Optimization**: If an `UPDATE` does not touch any column indexed by the candidate, write penalty is 0 (PostgreSQL Heap-Only Tuples optimization).

---

## Common Usage Scenarios & Presets

### Scenario A: Monitor Live Application Traffic (Production)
Monitor existing live application queries or external benchmarks (e.g. `pgbench` or web servers) for 2 minutes:

```toml
[config_selection]
module = "config_sel"
m = 2
k = 8

[workload]
mode = "timer"
timer_seconds = 120
log_file = "/var/lib/postgresql/16/main/query_logger.log"

[write_penalty]
enabled = true
write_scale = 1.0
```

### Scenario B: Hard Disk Budget Limit (Whang Drop)
Enforce a strict 50 MB total index size budget using `cs_drop`:

```toml
[config_selection]
module = "cs_drop"
m = 2
storage_budget = 52428800  # 50 MB in bytes

[workload]
mode = "timer"
timer_seconds = 60
log_file = "/var/lib/postgresql/16/main/query_logger.log"
```

### Scenario C: High-Volume OLTP Protection (Aggressive Write Penalty)
Discourage adding indexes on volatile tables that suffer heavy update/insert rates:

```toml
[config_selection]
module = "config_sel"
k = 5

[write_penalty]
enabled = true
write_scale = 20.0  # 20x write projection penalizes indexes on write-heavy tables
```

### Scenario D: Read-Only Analytical Workload (OLAP)
Optimize strictly for query performance ignoring all write maintenance:

```toml
[write_penalty]
enabled = false
```

---

## Running Tests

Execute the automated test suite with `pytest`:

```bash
# Run all unit and integration tests
pytest tests/

# Run with verbose test names
pytest -v tests/

# Run specific test suites
pytest tests/test_write_penalty_stats.py
pytest tests/test_dml_workload_and_json.py
pytest tests/test_config_selection_kwargs.py
```

---

## File Structure

```text
├── config.toml                     # Master configuration file
├── config.md                       # Configuration reference documentation
├── pyproject.toml                  # Python package and test configuration
├── requirements.txt                # Python dependencies
├── src/
│   └── auto_index_selector/
│       ├── __main__.py             # Pipeline orchestrator and entry point
│       ├── candidate_generation/   # Candidate index generators (rule-based, AutoAdmin, DTA)
│       ├── ConfigSelection/        # Selection algorithms (greedyMK, cs_drop, cs_extend)
│       ├── CostEstimator/          # HypoPG interface and B-tree WritePenaltyEstimator
│       ├── Workload/               # query_logger log parser and snapshot manager
│       └── workload_runner/        # Built-in synthetic workload runner (TPC-H, pgbench)
├── tests/                          # Automated pytest test suites
└── workload.json                   # Exported active query workload with parameters & weights
```
