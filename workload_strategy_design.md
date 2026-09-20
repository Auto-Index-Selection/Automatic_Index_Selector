# Workload Strategy Design: `query_logger` Integration

> **Status**: Design Phase — Do NOT implement until approved.

---

## 1. Problem Statement

The current AIS pipeline extracts workload via `pg_stat_statements` delta snapshots. This approach has fundamental limitations:

| Limitation | Impact |
|:---|:---|
| **Parameter values are lost** | `pg_stat_statements` normalizes constants into `$1`, `$2`, ... The `PlaceholderResolver` substitutes dummy literals (`1`, `'A'`, `DATE '1998-01-01'`), which produces **inaccurate selectivity estimates** in PostgreSQL's query planner. |
| **Cluster-wide scope** | `pg_stat_statements` tracks all databases; filtering requires manual `dbid` matching. |
| **No workload type control** | Cannot deterministically run TPC-H, pgbench, or custom workloads within the observation window. |
| **Counter wraps / resets** | `pg_stat_statements` counters can wrap or be reset externally, corrupting deltas. |

### Goal
Replace `pg_stat_statements`-based workload extraction with `query_logger`-based extraction that:
1. Captures **actual parameter values and type OIDs** per query execution.
2. Supports **pluggable workload types** (timer/live, TPC-H simulation, pgbench simulation, custom SQL files).
3. Uses **`PREPARE` / `EXECUTE`** for HypoPG cost estimation with real parameters → accurate selectivity.
4. Keeps `advisor_write_stats` write penalty estimation **completely unchanged**.

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        AIS Pipeline (__main__.py)                   │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  Phase 0: Setup                                              │   │
│  │  ─────────                                                   │   │
│  │  1. Load config.toml                                         │   │
│  │  2. Connect to PostgreSQL                                    │   │
│  │  3. Enable query_logger for this database                    │   │
│  │  4. Truncate query_logger.log                                │   │
│  │  5. Reset query_logger_stats()                               │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                              │                                      │
│                              ▼                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  Phase 1: Observation Window                                 │   │
│  │  ───────────────────────                                     │   │
│  │  Write Penalty:                                              │   │
│  │    snap_before_writes → [wait] → snap_after_writes           │   │
│  │    (advisor_write_stats — UNCHANGED)                         │   │
│  │                                                              │   │
│  │  Read Workload (NEW):                                        │   │
│  │    One of:                                                   │   │
│  │    ┌─────────────────────┐  ┌───────────────────────────┐    │   │
│  │    │ Option A: Timer     │  │ Option B: Simulate        │    │   │
│  │    │ Sleep N seconds     │  │ Run workload module:      │    │   │
│  │    │ (live app traffic)  │  │  - tpch                   │    │   │
│  │    │                     │  │  - pgbench                │    │   │
│  │    │                     │  │  - custom_sql             │    │   │
│  │    └─────────────────────┘  └───────────────────────────┘    │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                              │                                      │
│                              ▼                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  Phase 2: Workload Extraction                                │   │
│  │  ────────────────────────                                    │   │
│  │  1. Flush worker (sleep 0.2s)                                │   │
│  │  2. Parse query_logger.log (JSONL)                           │   │
│  │  3. Filter by database, SELECT only for read workload        │   │
│  │  4. Resolve type OIDs → canonical names                      │   │
│  │  5. Build W = [(query, params), ...] with weights            │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                              │                                      │
│                              ▼                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  Phase 3: Candidate Generation & Configuration Selection     │   │
│  │  ───────────────────────────────────────────────────────────  │   │
│  │  1. generateCandidateIndexes(W_queries, schema)              │   │
│  │  2. For each candidate config C:                             │   │
│  │     a. hypopg_create_index(...)                              │   │
│  │     b. For each (query, params) in W:                        │   │
│  │        PREPARE → EXPLAIN EXECUTE → DEALLOCATE                │   │
│  │     c. hypopg_reset()                                        │   │
│  │     d. Add write_penalty(C)                                  │   │
│  │  3. Return C* = argmin(TotalCost)                            │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. Detailed Design

### 3.1 Configuration Changes (`config.toml`)

```toml
# --------------------------------------------------------------------------
# 3. WORKLOAD INGESTION STAGE
# --------------------------------------------------------------------------
[workload]
# Available modules:
#   - "queryLoggerWorkload"        : Uses query_logger C extension to capture
#                                    actual queries with real parameter values.
#                                    (Recommended — accurate selectivity)
#   - "pgStatStatementsWorkload"   : Legacy pg_stat_statements delta snapshots.
#                                    (Fallback — dummy parameter substitution)
module = "queryLoggerWorkload"

# Path to query_logger log file (absolute or relative to $PGDATA)
log_file = "/var/lib/postgresql/16/main/query_logger.log"

# Observation mode:
#   - "timer"    : Sleep for window_duration_seconds, capture live app traffic.
#   - "tpch"     : Run TPC-H analytical queries from scripts/queries/reads/
#   - "pgbench"  : Run pgbench DML+read queries from tests/pgbench/queries/
#   - "custom"   : Run custom SQL files from a user-specified directory.
observation_mode = "timer"

# Directory containing custom SQL files (only used when observation_mode = "custom")
custom_queries_dir = ""

# Number of rounds to execute workload queries (for simulate modes)
workload_rounds = 3
```

> [!NOTE]
> `window_duration_seconds` remains under `[write_penalty]` since it controls the
> write stats observation window. For `observation_mode = "timer"`, the same duration
> is reused. For simulate modes, the workload runs to completion regardless of the timer.

---

### 3.2 New Workload Module: `queryLoggerWorkload.py`

**Location**: `src/auto_index_selector/Workload/queryLoggerWorkload.py`

#### 3.2.1 Data Structures

```python
@dataclass
class QueryParam:
    """A single resolved query parameter."""
    position: int       # 1-based ($1, $2, ...)
    pg_type: str        # canonical type name (e.g. "int4", "float8", "text")
    value: Any          # Python-native value (int, float, str, bool, None)

@dataclass
class CapturedQuery:
    """A query captured from query_logger with its actual parameters."""
    query: str                  # Raw SQL with $1, $2, ...
    params: List[QueryParam]    # Resolved parameters
    tag: str                    # SELECT, UPDATE, INSERT, DELETE
    duration_ms: float
    rows: int

@dataclass  
class QueryLoggerWorkload:
    """Extracted workload from query_logger log."""
    queries: List[CapturedQuery]         # All captured SELECT queries
    query_weights: Dict[str, float]      # normalized_query → execution count
    schema: Dict[str, Dict[str, str]]    # table → {col: type}
```

#### 3.2.2 Core Functions

```python
def setup_query_logger(conn, db_name: str) -> None:
    """Enable query_logger extension for the target database."""
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS query_logger")
        # Enable only for this database
        cur.execute(f"ALTER DATABASE {db_name} SET query_logger.enabled = on")
    conn.commit()

def truncate_log(log_file: str) -> None:
    """Truncate query_logger.log to 0 bytes in-place."""
    with open(log_file, "w") as f:
        f.truncate(0)

def reset_stats(conn) -> None:
    """Reset query_logger shared memory counters."""
    with conn.cursor() as cur:
        cur.execute("SELECT query_logger_reset_stats()")
    conn.commit()

def flush_and_wait(delay: float = 0.3) -> None:
    """Wait for background worker to drain ring buffer and flush to disk."""
    time.sleep(delay)

def extract_workload(
    log_file: str,
    db_name: str,
    conn,
) -> QueryLoggerWorkload:
    """Parse query_logger.log and extract the read workload."""
    # 1. Read JSONL file line by line
    # 2. Filter: db == db_name, tag == "SELECT"
    # 3. Exclude internal queries (hypopg, advisor, pg_catalog, etc.)
    # 4. Resolve type OIDs using pg_types lookup
    # 5. Deduplicate by normalized query text
    # 6. Compute query_weights (execution count per unique query)
    # 7. Fetch live schema via information_schema
    ...
```

---

### 3.3 Workload Runners (Pluggable Observation Modes)

**Location**: `src/auto_index_selector/Workload/runners/`

```python
# runners/__init__.py
# runners/timer_runner.py      — Sleep for N seconds (live traffic)
# runners/tpch_runner.py       — Execute TPC-H read queries
# runners/pgbench_runner.py    — Execute pgbench read + DML queries
# runners/custom_runner.py     — Execute user-provided .sql files
```

Each runner implements a simple interface:

```python
class WorkloadRunner(ABC):
    @abstractmethod
    def run(self, conn, config: dict) -> None:
        """Execute workload queries against the database.
        
        The query_logger extension captures everything automatically.
        This method just needs to execute the queries.
        """
        pass

class TimerRunner(WorkloadRunner):
    def run(self, conn, config):
        duration = int(config.get("write_penalty", {}).get("window_duration_seconds", 10))
        print(f"[Observer] Monitoring live traffic for {duration}s...")
        time.sleep(duration)

class TpchRunner(WorkloadRunner):
    def run(self, conn, config):
        queries_dir = Path(__file__).parent.parent.parent.parent.parent / "scripts" / "queries" / "reads"
        rounds = int(config.get("workload", {}).get("workload_rounds", 3))
        # Load and execute each .sql file `rounds` times
        ...

class PgbenchRunner(WorkloadRunner):
    def run(self, conn, config):
        queries_dir = Path(__file__).parent.parent.parent.parent.parent / "tests" / "pgbench" / "queries" / "reads"
        rounds = int(config.get("workload", {}).get("workload_rounds", 3))
        ...

class CustomRunner(WorkloadRunner):
    def run(self, conn, config):
        queries_dir = Path(config["workload"]["custom_queries_dir"])
        rounds = int(config.get("workload", {}).get("workload_rounds", 3))
        ...
```

**Runner Registry**:
```python
RUNNERS = {
    "timer":   TimerRunner,
    "tpch":    TpchRunner,
    "pgbench": PgbenchRunner,
    "custom":  CustomRunner,
}
```

---

### 3.4 Cost Estimator Changes: PREPARE / EXECUTE with HypoPG

This is the most critical design decision. There are **3 options**:

---

#### **Option A: PREPARE / EXECUTE (Recommended ✅)**

```python
def getQueryCostWithParams(conn, query: str, params: List[QueryParam], 
                           fallback_cost: float = 1e9) -> float:
    """
    Cost-estimate a parameterized query using PREPARE + EXPLAIN EXECUTE.
    
    PostgreSQL's planner uses actual parameter values for selectivity estimation
    when executing a prepared statement, producing significantly more accurate
    cost estimates than dummy literal substitution.
    """
    stmt_name = f"ais_cost_{id(query) % 100000}"
    
    try:
        with conn.cursor() as cur:
            # Build type list for PREPARE
            type_list = ", ".join(p.pg_type for p in params)
            prepare_sql = f"PREPARE {stmt_name}({type_list}) AS {query}"
            cur.execute(prepare_sql)
            
            # Build parameter values for EXECUTE
            value_literals = []
            for p in params:
                if p.value is None:
                    value_literals.append("NULL")
                elif p.pg_type in ("text", "varchar", "bpchar", "char", "name"):
                    escaped = str(p.value).replace("'", "''")
                    value_literals.append(f"'{escaped}'")
                elif p.pg_type in ("date",):
                    value_literals.append(f"DATE '{p.value}'")
                elif p.pg_type in ("timestamp", "timestamptz"):
                    value_literals.append(f"TIMESTAMP '{p.value}'")
                elif p.pg_type == "bool":
                    value_literals.append("TRUE" if p.value else "FALSE")
                else:
                    value_literals.append(str(p.value))
            
            params_str = ", ".join(value_literals)
            explain_sql = f"EXPLAIN (FORMAT JSON) EXECUTE {stmt_name}({params_str})"
            cur.execute(explain_sql)
            result = cur.fetchone()
            
            cur.execute(f"DEALLOCATE {stmt_name}")
            
            if result and result[0]:
                return float(result[0][0]["Plan"]["Total Cost"])
                
    except Exception as exc:
        conn.rollback()
        try:
            with conn.cursor() as cur:
                cur.execute(f"DEALLOCATE {stmt_name}")
        except Exception:
            conn.rollback()
        logger.warning("Parameterized cost estimation failed: %s", exc)
        return fallback_cost
    
    return fallback_cost
```

**Pros**:
- ✅ **Exact selectivity**: PostgreSQL uses actual parameter values for histogram lookups
- ✅ **No SQL injection risk**: Parameters are passed through PostgreSQL's type system
- ✅ **Handles all types**: Type OIDs from query_logger map directly to PREPARE type declarations
- ✅ **Standard PostgreSQL mechanism**: No custom query rewriting

**Cons**:
- ⚠️ Slightly more SQL round-trips (PREPARE + EXPLAIN EXECUTE + DEALLOCATE = 3 statements per query)
- ⚠️ Need to handle DEALLOCATE cleanup on errors

---

#### **Option B: Direct $N → Literal Substitution in Query Text**

```python
def getQueryCostSubstituted(conn, query: str, params: List[QueryParam],
                            fallback_cost: float = 1e9) -> float:
    """Replace $1, $2 with actual literal values inline, then EXPLAIN."""
    resolved = query
    for p in sorted(params, key=lambda x: x.position, reverse=True):
        # Replace $N with literal (reverse order to avoid $1 matching $10)
        literal = format_literal(p)
        resolved = resolved.replace(f"${p.position}", literal)
    
    return getQueryCost(conn, resolved, fallback_cost)
```

**Pros**:
- ✅ Simple implementation
- ✅ Single EXPLAIN call (fast)

**Cons**:
- ❌ **Fragile regex/replace**: `$1` can appear inside string literals, comments, or `$10` can be partially matched
- ❌ **SQL injection risk**: Must carefully quote/escape all string values
- ❌ **Type casting issues**: Some expressions like `CAST($1 AS int4)` need the cast removed or the literal must match

---

#### **Option C: Use psycopg2's Native Parameterization with `%s`**

```python
def getQueryCostNative(conn, query: str, params: List[QueryParam],
                       fallback_cost: float = 1e9) -> float:
    """Use psycopg2 native parameterization with EXPLAIN."""
    # Convert $1, $2 → %s 
    explain_query, ordered_values = convert_dollar_to_pct(query, params)
    explain_sql = f"EXPLAIN (FORMAT JSON) {explain_query}"
    
    with conn.cursor() as cur:
        cur.execute(explain_sql, ordered_values)
        result = cur.fetchone()
        ...
```

**Pros**:
- ✅ Clean parameterization via psycopg2
- ✅ Automatic type handling

**Cons**:
- ❌ **PostgreSQL EXPLAIN does not support parameterized queries via `%s`**: `EXPLAIN (FORMAT JSON) SELECT * FROM t WHERE id = %s` with `cur.execute(sql, [42])` **does NOT work**. PostgreSQL's `EXPLAIN` command does not accept bind parameters in the SQL text being explained. The `%s` substitution happens at the psycopg2 level for DML, but EXPLAIN treats the entire string as literal SQL.
- ❌ This approach is **fundamentally broken** for cost estimation.

---

### 3.4.1 Recommendation

> [!IMPORTANT]
> **Use Option A: PREPARE / EXECUTE**
> 
> This is the only approach that:
> 1. Uses PostgreSQL's native prepared statement mechanism
> 2. Passes actual parameter values through the type system
> 3. Produces accurate selectivity estimates from real histograms
> 4. Works correctly with HypoPG hypothetical indexes
> 5. Has no SQL injection or regex fragility risks
>
> Option C is **broken** (EXPLAIN doesn't support bind parameters).
> Option B is fragile and error-prone.

---

### 3.5 Updated `estimateWorkloadCostForConfig`

```python
def estimateWorkloadCostForConfig(conn, W, configuration, 
                                  query_weights=None, 
                                  write_penalties=None) -> float:
    """
    Computes total workload cost = ReadCost + WritePenalty.
    
    W can be either:
      - List[str]           (legacy: resolved query strings, no params)
      - List[CapturedQuery] (new: queries with actual parameters)
    """
    clearHypotheticalIndexes(conn)
    if configuration:
        createCompositeHypoIndexes(conn, configuration)

    read_cost = 0.0
    for entry in W:
        if isinstance(entry, CapturedQuery) and entry.params:
            # New path: PREPARE / EXECUTE with real params
            weight = float(query_weights.get(entry.query, 1.0)) if query_weights else 1.0
            read_cost += weight * getQueryCostWithParams(conn, entry.query, entry.params)
        else:
            # Legacy path: direct EXPLAIN (no params or already resolved)
            query = entry.query if isinstance(entry, CapturedQuery) else entry
            weight = float(query_weights.get(query, 1.0)) if query_weights else 1.0
            read_cost += weight * getQueryCost(conn, query)

    clearHypotheticalIndexes(conn)

    # Write penalty calculation (UNCHANGED)
    write_cost = 0.0
    if write_penalties and configuration and callable(write_penalties):
        for table, cols in configuration:
            write_cost += write_penalties(table, tuple(cols))

    total_cost = read_cost + write_cost
    return total_cost
```

---

### 3.6 Updated Pipeline Flow (`__main__.py`)

```python
def observe_workload(conn, cfg, verbose=True):
    """Phase 1: Observation window with query_logger."""
    
    workload_cfg = cfg.get("workload", {})
    wp_config = cfg.get("write_penalty", {})
    wp_enabled = wp_config.get("enabled", False)
    module_name = workload_cfg.get("module", "queryLoggerWorkload")
    
    # --- 0. Setup query_logger ---
    if module_name == "queryLoggerWorkload":
        log_file = workload_cfg.get("log_file", "query_logger.log")
        db_name = conn.info.dbname  # Get current database name
        
        setup_query_logger(conn, db_name)
        truncate_log(log_file)
        reset_stats(conn)
        if verbose:
            print(f"[QueryLogger] Enabled for {db_name}, log truncated, stats reset")
    
    # --- 1. Write penalty before-snapshot (UNCHANGED) ---
    wp_estimator = None
    snap_before_writes = None
    if wp_enabled:
        wp_estimator = WritePenaltyEstimator(conn, ...)
        wp_estimator.ensure_extension()
        snap_before_writes = wp_estimator.snapshot()
    
    # --- 2. Run observation (workload runner or timer) ---
    observation_mode = workload_cfg.get("observation_mode", "timer")
    runner = RUNNERS[observation_mode]()
    runner.run(conn, cfg)
    
    # --- 3. Write penalty after-snapshot (UNCHANGED) ---
    snap_after_writes = None
    if wp_enabled and wp_estimator and snap_before_writes:
        snap_after_writes = wp_estimator.snapshot()
    
    # --- 4. Extract workload from query_logger ---
    if module_name == "queryLoggerWorkload":
        flush_and_wait(0.3)
        workload = extract_workload(log_file, db_name, conn)
        W = workload.queries
        schema = workload.schema
        query_weights = workload.query_weights
    else:
        # Legacy pg_stat_statements path
        ...
    
    # --- 5. Compute write penalties (UNCHANGED) ---
    write_penalties = None
    if wp_enabled and wp_estimator and snap_before_writes and snap_after_writes:
        write_delta = wp_estimator.compute_delta(snap_before_writes, snap_after_writes)
        write_penalties = wp_estimator.get_penalty_function(write_delta)
    
    return WorkloadObservation(W, schema, query_weights, write_penalties)
```

---

## 4. Candidate Generation Compatibility

The candidate generation modules (`cg_rule_based`, `cg_auto_admin`, etc.) expect:
```python
generateCandidateIndexes(W: List[str], schema: Dict[str, Dict[str, str]])
```

Where `W` is a list of SQL query strings. With `CapturedQuery` objects, we need to extract the raw query text:

```python
# Extract plain query strings for candidate generation
W_queries = [q.query for q in W] if isinstance(W[0], CapturedQuery) else W
raw_cands = cg_module.generateCandidateIndexes(W_queries, schema)
```

The candidate generators parse SQL AST to find WHERE, JOIN, ORDER BY columns — they don't need parameter values. The parameterized `$1`, `$2` syntax is fine for AST parsing since the generators look at column references, not literal values.

---

## 5. Type OID Resolution (`pg_types.py`)

Reuse the existing `pg_types.py` from `workload-sampling-mtp/workload-extractor/`:

```python
# Copy to: src/auto_index_selector/Workload/pg_types.py

PG_BUILTIN_TYPES = {
    16: "bool",
    20: "int8",
    21: "int2",
    23: "int4",
    25: "text",
    26: "oid",
    700: "float4",
    701: "float8",
    1042: "bpchar",
    1043: "varchar",
    1082: "date",
    1114: "timestamp",
    1184: "timestamptz",
    1700: "numeric",
    # ... 140+ types
}

def resolve_type_oid(oid: int) -> str:
    return PG_BUILTIN_TYPES.get(oid, f"text")  # fallback to text
```

---

## 6. File Structure (New / Modified)

```
src/auto_index_selector/
├── __main__.py                          [MODIFY] Pipeline flow changes
├── Workload/
│   ├── pgStatStatementsWorkload.py      [KEEP]   Legacy module (unchanged)
│   ├── queryLoggerWorkload.py           [NEW]    query_logger workload extraction
│   ├── pg_types.py                      [NEW]    PostgreSQL type OID catalog
│   └── runners/                         [NEW]    Pluggable workload runners
│       ├── __init__.py
│       ├── base.py                      [NEW]    WorkloadRunner ABC
│       ├── timer_runner.py              [NEW]    Sleep-based live monitoring
│       ├── tpch_runner.py               [NEW]    TPC-H query execution
│       ├── pgbench_runner.py            [NEW]    pgbench query execution
│       └── custom_runner.py             [NEW]    Custom SQL file execution
├── CostEstimator/
│   ├── costEstimator.py                 [MODIFY] Add getQueryCostWithParams()
│   └── write_penalty_estimator.py       [KEEP]   Completely unchanged
├── CandidateGeneration/                 [KEEP]   Completely unchanged
└── ConfigSelection/                     [KEEP]   Completely unchanged
```

---

## 7. Execution Example

### Example 1: Timer Mode (Live Traffic Monitoring)
```bash
# config.toml:
#   [workload]
#   module = "queryLoggerWorkload"
#   observation_mode = "timer"
#   [write_penalty]
#   window_duration_seconds = 30

PYTHONPATH=src python -m auto_index_selector
# Output:
# [QueryLogger] Enabled for tpch_db, log truncated, stats reset
# [WritePenalty] Captured write before-snapshot (scale=1.0)
# [Observer] Monitoring live traffic for 30s...
# [QueryLogger] Flushing worker buffer...
# [Workload] Extracted 47 unique SELECT queries from query_logger.log
# [Workload] 32 queries have actual parameter values (PREPARE/EXECUTE path)
# [Workload] 15 queries are non-parameterized (direct EXPLAIN path)
# ...
```

### Example 2: TPC-H Simulation
```bash
# config.toml:
#   [workload]
#   module = "queryLoggerWorkload"
#   observation_mode = "tpch"
#   workload_rounds = 5

PYTHONPATH=src python -m auto_index_selector
# Output:
# [QueryLogger] Enabled for tpch_db, log truncated, stats reset
# [WritePenalty] Captured write before-snapshot (scale=1.0)
# [Workload] Running TPC-H workload (5 rounds × 22 queries = 110 executions)...
# [QueryLogger] Flushing worker buffer...
# [Workload] Extracted 22 unique SELECT queries with actual parameters
# ...
```

---

## 8. Open Questions

> [!IMPORTANT]
> **Q1**: For candidate generation, the current `cg_rule_based` module parses SQL 
> to extract indexable columns from WHERE, JOIN, ORDER BY. With `$1`, `$2` still in 
> the query text, this should work fine since the module looks at column references 
> not literals. **Confirm: Is this correct for all CG modules?**

> [!IMPORTANT]
> **Q2**: Should we support **mixed-mode** workloads where the DML traffic 
> (for write penalty) runs alongside the read queries in the same observation window?
> Currently the write penalty uses `advisor_write_stats` independently.
> If `observation_mode = "tpch"`, should the TPC-H runner also execute DML 
> statements for write penalty, or should we keep DML simulation separate 
> (via `scripts/simulate_workload.py`)?

> [!IMPORTANT]
> **Q3**: For PREPARE/EXECUTE, PostgreSQL's planner behavior depends on the 
> `plan_cache_mode` setting. After 5 executions of a prepared statement, 
> PostgreSQL may switch to a "generic plan" that ignores parameter values.
> Since we only execute EXPLAIN EXECUTE once per query, this should not be an issue.
> **Confirm: We are fine with single-execution PREPARE/EXECUTE.**

> [!IMPORTANT]
> **Q4**: The `query_logger.log` file path — should we auto-detect it from 
> PostgreSQL's `SHOW data_directory` + `query_logger.log_filename` GUC, 
> or require explicit configuration in `config.toml`?

> [!IMPORTANT]
> **Q5**: When using `observation_mode = "tpch"` or `"pgbench"`, the runner 
> executes queries via psycopg2's `cur.execute(sql, params)`. psycopg2 uses 
> **server-side prepared statements** which send parameters as `$1`, `$2`, etc.
> The `query_logger` extension will capture these with their actual parameter 
> values. **Confirm: This is the desired behavior.**

---

## 9. Migration & Backward Compatibility

- `pgStatStatementsWorkload.py` remains **fully functional** as a fallback.
- Setting `module = "pgStatStatementsWorkload"` in `config.toml` uses the legacy path.
- All existing tests continue to work unchanged.
- The `PlaceholderResolver` class is **not deleted** — it remains available for the legacy module.

---

## 10. Summary: What Changes vs What Stays

| Component | Status | Notes |
|:---|:---|:---|
| `advisor_write_stats` extension | **UNCHANGED** | Write penalty snapshots and delta computation |
| `WritePenaltyEstimator` | **UNCHANGED** | B-tree cost model, HOT detection |
| `CandidateGeneration/*` | **UNCHANGED** | All CG modules parse SQL AST |
| `ConfigSelection/*` | **UNCHANGED** | All CS modules call `estimateWorkloadCostForConfig` |
| `PlaceholderResolver` | **UNCHANGED** | Kept for legacy `pgStatStatementsWorkload` |
| `pgStatStatementsWorkload.py` | **UNCHANGED** | Legacy fallback module |
| `costEstimator.py` | **MODIFIED** | Add `getQueryCostWithParams()` using PREPARE/EXECUTE |
| `__main__.py` | **MODIFIED** | New observation flow with query_logger setup/extraction |
| `queryLoggerWorkload.py` | **NEW** | query_logger log parsing and workload extraction |
| `pg_types.py` | **NEW** | Type OID catalog (copied from workload-sampling-mtp) |
| `runners/*` | **NEW** | Pluggable workload execution (timer, tpch, pgbench, custom) |
| `config.toml` | **MODIFIED** | New workload options |
