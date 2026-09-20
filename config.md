# Configuration Guide (`config.toml`)

This document provides a comprehensive reference for configuring the **Automatic Index Selector** pipeline via `config.toml`.

---

## 1. Pipeline Overview & Architecture

The pipeline is split into four decoupled, modular stages:

```
[3. Workload Ingestion] ──▶ [1. Candidate Generation] ──▶ [4. Write Penalty Estimator] ──▶ [2. Configuration Selection]
```

---

## 2. Pluggable Modules & Compatibility Matrix

Every candidate generator can be paired with any configuration selector:

| Candidate Generator (`[candidate_generation]`) | Configuration Selector (`[config_selection]`) | Supported? | Best Use Case |
|:---|:---|:---:|:---|
| `cg_rule_based` *(Recommended)* | `config_sel` *(Greedy m, k)* | **Yes** | General production workloads, multi-table joins, complex filters. |
| `cg_rule_based` *(Recommended)* | `cs_drop` *(Whang 1985)* | **Yes** | Environments with hard storage limits (e.g. 50 MB disk budget). |
| `cg_rule_based` *(Recommended)* | `cs_extend` *(ICDE 2019)* | **Yes** | Multi-attribute workloads benefiting from recursive index morphing. |
| `cg_auto_admin` | `config_sel` / `cs_drop` / `cs_extend` | **Yes** | Classic Microsoft AutoAdmin heuristic indexing. |
| `cg_dta` | `config_sel` / `cs_drop` / `cs_extend` | **Yes** | Database Tuning Advisor candidate access pattern heuristics. |
| `cg_naive` | `config_sel` / `cs_drop` / `cs_extend` | **Yes** | Baseline comparison with single-attribute indexing. |
| `dexter` | `config_sel` / `cs_drop` / `cs_extend` | **Yes** | Exhaustive permutation generation across table columns. |

---

## 3. Parameter Reference

### `[database]`
| Parameter | Type | Default | Description |
|:---|:---:|:---:|:---|
| `host` | `string` | `"localhost"` | PostgreSQL server hostname / IP address. Falls back to `DB_HOST` in `.env` if omitted. |
| `port` | `integer` | `5432` | PostgreSQL server port. Falls back to `DB_PORT` in `.env` if omitted. |
| `user` | `string` | `"postgres"` | Database username. Falls back to `DB_USER` in `.env` if omitted. |
| `password` | `string` | `""` | Database password. Falls back to `DB_PASSWORD` in `.env` if omitted. |
| `dbname` | `string` | `"tpch_db"` | Target database name to connect and run against. Falls back to `DB_NAME` in `.env` if omitted. |

---

### `[candidate_generation]`
| Parameter | Type | Default | Description |
|:---|:---:|:---:|:---|
| `module` | `string` | `"cg_rule_based"` | Active candidate generator module (`cg_rule_based`, `cg_auto_admin`, `cg_dta`, `cg_naive`, `dexter`). |

---

### `[config_selection]`
| Parameter | Type | Default | Applicable Modules | Description |
|:---|:---:|:---:|:---|:---|
| `module` | `string` | `"config_sel"` | All | Active selection algorithm (`config_sel`, `cs_drop`, `cs_extend`). |
| `m` | `integer` | `2` | `config_sel`, `cs_drop` | • For `config_sel`: Size of the exhaustively searched seed configuration.<br>• For `cs_drop`: Maximum group size dropped at once (`max_group`). |
| `k` | `integer` | `10` | `config_sel` | Maximum number of recommended indexes (index count budget). |
| `storage_budget` | `string` / `number` | `"inf"` | `cs_drop`, `cs_extend` | Maximum total on-disk memory footprint in bytes (e.g. `52428800` for 50 MB, or `"inf"` for unconstrained). |

---

### `[workload]`
| Parameter | Type | Default | Description |
|:---|:---:|:---:|:---|
| `mode` | `string` | `"custom"` | Pluggable workload observation mode: `"timer"` / `"live"` (passively monitors live traffic for `timer_seconds`), or `"custom"` (executes queries via `src/auto_index_selector/workload_runner/`). |
| `database` | `string` | `"tpch_db"` | Target database name to connect and run against (falls back to `DB_NAME` in `.env` if omitted). |
| `queries_path` | `string` | `"tpch"` | Workload queries to run when `mode = "custom"` (`"tpch"`, `"pgbench"`, or directory path). |
| `iterations` | `integer` | `5` | Number of continuous execution iterations/rounds when `mode = "custom"`. |
| `execute_dml` | `boolean` | `true` | When true, executes companion DML statements to drive write penalty statistics. |
| `timer_seconds` | `integer` | `60` | Duration in seconds to monitor live traffic when `mode = "timer"`. |
| `log_file` | `string` | `"auto"` | Path to `query_logger.log` or `"auto"` to auto-detect from PostgreSQL catalog. |


---

### `[write_penalty]`
| Parameter | Type | Default | Description |
|:---|:---:|:---:|:---|
| `enabled` | `boolean` | `true` | `true`: Uses B-tree analytical write model & C extension `advisor_write_stats`.<br>`false`: Disables write penalties (pure read-only cost optimization). |
| `write_scale` | `float` | `1.0` | Multiplier for observed write volume.<br>• `1.0`: Exact observed writes.<br>• `10.0` - `100.0`: Projects high-frequency OLTP write spikes to protect update-heavy tables. |
| `window_duration_seconds` | `integer` | `15` | Number of seconds to observe database traffic between before-and-after snapshots. |

---

## 4. Configuration Presets

### Preset 1: Default Balanced Production (Greedy $m, k$ + Write Penalties)
```toml
[candidate_generation]
module = "cg_rule_based"

[config_selection]
module = "config_sel"
m = 2
k = 10

[workload]
mode = "timer"

[write_penalty]
enabled = true
write_scale = 1.0
window_duration_seconds = 15
```

---

### Preset 2: Hard Storage Budget (Whang Drop Heuristic, 50 MB Limit)
```toml
[candidate_generation]
module = "cg_rule_based"

[config_selection]
module = "cs_drop"
m = 2
storage_budget = 52428800  # 50 MB in bytes

[workload]
mode = "timer"

[write_penalty]
enabled = true
write_scale = 1.0
window_duration_seconds = 15
```

---

### Preset 3: Recursive Index Morphing (Extend Algorithm, 100 MB Limit)
```toml
[candidate_generation]
module = "cg_rule_based"

[config_selection]
module = "cs_extend"
storage_budget = 104857600  # 100 MB in bytes

[workload]
mode = "timer"

[write_penalty]
enabled = true
write_scale = 1.0
window_duration_seconds = 15
```

---

### Preset 4: Aggressive Write-Heavy OLTP Workload (100x Write Projection)
```toml
[candidate_generation]
module = "cg_rule_based"

[config_selection]
module = "config_sel"
m = 2
k = 10

[workload]
mode = "timer"

[write_penalty]
enabled = true
write_scale = 100.0         # Heavy write penalty protects frequently updated tables
window_duration_seconds = 30
```

---

### Preset 5: Read-Only Analytics Baseline (Zero Write Penalty)
```toml
[candidate_generation]
module = "cg_rule_based"

[config_selection]
module = "config_sel"
m = 2
k = 10

[workload]
mode = "timer"

[write_penalty]
enabled = false             # Pure read optimization ignoring database writes
window_duration_seconds = 0
```
