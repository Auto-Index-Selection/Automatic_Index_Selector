"""
cs_extend.py — Extend Algorithm for Configuration Selection.

Reference: Schlosser, Kossmann, Boissier.
"Efficient Scalable Multi-Attribute Index Selection Using Recursive Strategies."
ICDE 2019 (Algorithm 1, Heuristic H6).

Builds a multi-attribute index configuration step by step:
  - Option A: Add a new single-attribute index.
  - Option B: Morph an existing index by appending an attribute (k -> k + [col]).
Picks the action with highest marginal cost reduction per unit of additional memory (ΔCost / ΔSize).
Integrated with HypoPG, write penalties, and query frequency weights.
"""

from itertools import combinations
import logging
from typing import Dict, List, Optional, Set, Tuple, Any
import psycopg2

from ..CostEstimator.costEstimator import estimateWorkloadCostForConfig
from .cs_drop import estimateIndexSize, buildSizeMap

logger = logging.getLogger(__name__)

Index = Tuple[str, Tuple[str, ...]]          # (table_name, (col1, col2, ...))
IndexSet = List[Index]
CandidatePool = List[Tuple[str, str]]        # [(table, col), ...]


def extractCandidatePool(candidate_dict: Any) -> CandidatePool:
    """Extract unique (table, single_column) candidate attributes from candidate structure."""
    pool: List[Tuple[str, str]] = []
    seen: Set[Tuple[str, str]] = set()

    if isinstance(candidate_dict, dict):
        for table, col_lists in candidate_dict.items():
            for cols in col_lists:
                for col in cols:
                    pair = (table, col)
                    if pair not in seen:
                        seen.add(pair)
                        pool.append(pair)
    elif isinstance(candidate_dict, (list, tuple, set)):
        for item in candidate_dict:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                table, cols = item
                if isinstance(cols, (list, tuple)):
                    for col in cols:
                        pair = (table, col)
                        if pair not in seen:
                            seen.add(pair)
                            pool.append(pair)
                elif isinstance(cols, str):
                    pair = (table, cols)
                    if pair not in seen:
                        seen.add(pair)
                        pool.append(pair)
    return pool


class ExtendAlgorithm:
    """
    Implements Algorithm 1 (Extend / H6) from the ICDE 2019 paper.
    """

    def __init__(
        self,
        conn,
        W: List[str],
        budget_mb: float = 500.0,
        max_index_width: int = 3,
        min_cost_improvement: float = 1.003,
        write_penalties: Optional[Dict[Tuple[str, Tuple[str, ...]], float]] = None,
        query_weights: Optional[Dict[str, float]] = None,
        cost_cache: Optional[Dict[Any, float]] = None,
        size_cache: Optional[Dict[Any, float]] = None,
        verbose: bool = False,
    ):
        self.conn = conn
        self.W = W
        self.budget_mb = float(budget_mb)
        self.max_index_width = max_index_width
        self.min_cost_improvement = min_cost_improvement
        self.write_penalties = write_penalties or {}
        self.query_weights = query_weights or {}
        self.cost_cache = cost_cache if cost_cache is not None else {}
        self.size_cache = size_cache if size_cache is not None else {}
        self.verbose = verbose

    def _estimate_size_mb(self, table: str, cols: Tuple[str, ...]) -> float:
        idx_key = (table, cols)
        if idx_key not in self.size_cache:
            size_bytes = estimateIndexSize(self.conn, table, cols)
            self.size_cache[idx_key] = size_bytes / (1024.0 * 1024.0)
        return self.size_cache[idx_key]

    def _calculate_total_size_mb(self, I: IndexSet) -> float:
        return sum(self._estimate_size_mb(table, cols) for table, cols in I)

    def _calculate_cost(self, I: IndexSet) -> float:
        key = frozenset((table, tuple(cols)) for table, cols in I)
        if key not in self.cost_cache:
            read_cost = estimateWorkloadCostForConfig(
                self.conn, self.W, key, query_weights=self.query_weights
            )
            write_cost = 0.0
            if self.write_penalties:
                for table, cols in key:
                    write_cost += self.write_penalties.get((table, tuple(cols)), 0.0)
            self.cost_cache[key] = read_cost + write_cost
        return self.cost_cache[key]

    def run(self, candidates: CandidatePool) -> Tuple[IndexSet, float, float]:
        """Execute Extend algorithm."""
        if not self.W or not candidates:
            return [], self._calculate_cost([]), 0.0

        # Step 1: Baseline cost F0 = Cost(empty set)
        I: IndexSet = []
        F0 = self._calculate_cost(I)
        logger.info(f"[Extend] Baseline workload cost F(∅) = {F0:.4f}")

        # Step 2: Seed Selection
        I, F_current, total_size = self._seed_selection(candidates, F0)
        if not I:
            logger.info("[Extend] No beneficial seed found. Returning empty configuration.")
            return [], F0, 0.0

        logger.info(f"[Extend] Seed chosen: {I[0][0]}({",".join(I[0][1])}) | Cost={F_current:.2f} | Size={total_size:.2f} MB")

        # Step 3: Recursive Expansion Loop (Add & Morph)
        I, F_current, total_size = self._expansion_loop(candidates, I, F_current, total_size)
        return I, F_current, total_size

    def _seed_selection(self, candidates: CandidatePool, F0: float) -> Tuple[IndexSet, float, float]:
        best_seed: Optional[Index] = None
        best_ratio = -float("inf")
        best_Fi = F0
        best_size = 0.0

        for table, col in candidates:
            cols = (col,)
            size_mb = self._estimate_size_mb(table, cols)
            if size_mb > self.budget_mb:
                continue

            candidate_I: IndexSet = [(table, cols)]
            Fi = self._calculate_cost(candidate_I)
            delta_cost = F0 - Fi

            if delta_cost <= 0:
                continue

            ratio = delta_cost / (size_mb if size_mb > 0 else 0.001)
            if ratio > best_ratio:
                best_ratio = ratio
                best_seed = (table, cols)
                best_Fi = Fi
                best_size = size_mb

        if best_seed is None:
            return [], F0, 0.0

        return [best_seed], best_Fi, best_size

    def _expansion_loop(
        self,
        candidates: CandidatePool,
        I: IndexSet,
        F_current: float,
        total_size: float
    ) -> Tuple[IndexSet, float, float]:
        iteration = 0
        while True:
            iteration += 1
            best_new_I: Optional[IndexSet] = None
            best_new_cost: Optional[float] = None
            best_new_size: Optional[float] = None
            best_ratio = -float("inf")
            best_action = ""

            for table, col in candidates:
                # Option A: Add new single-attribute index
                if self._can_add_new(I, table, col):
                    new_size_a = total_size + self._estimate_size_mb(table, (col,))
                    if new_size_a <= self.budget_mb:
                        new_I_a = I + [(table, (col,))]
                        F_a = self._calculate_cost(new_I_a)
                        ratio_a = self._compute_ratio(F_current - F_a, new_size_a - total_size)
                        if (
                            ratio_a is not None
                            and ratio_a > best_ratio
                            and self._passes_threshold(F_a, F_current)
                        ):
                            best_ratio = ratio_a
                            best_new_I = new_I_a
                            best_new_cost = F_a
                            best_new_size = new_size_a
                            best_action = f"ADD {table}({col})"

                # Option B: Morph existing index (append col)
                for k_idx, (k_table, k_cols) in enumerate(I):
                    if k_table != table or len(k_cols) >= self.max_index_width or col in k_cols:
                        continue

                    new_cols = k_cols + (col,)
                    new_I_b = list(I)
                    new_I_b[k_idx] = (k_table, new_cols)

                    old_size = self._estimate_size_mb(k_table, k_cols)
                    new_idx_size = self._estimate_size_mb(k_table, new_cols)
                    new_size_b = total_size - old_size + new_idx_size

                    if new_size_b > self.budget_mb:
                        continue

                    F_b = self._calculate_cost(new_I_b)
                    ratio_b = self._compute_ratio(F_current - F_b, new_size_b - total_size)
                    if (
                        ratio_b is not None
                        and ratio_b > best_ratio
                        and self._passes_threshold(F_b, F_current)
                    ):
                        best_ratio = ratio_b
                        best_new_I = new_I_b
                        best_new_cost = F_b
                        best_new_size = new_size_b
                        best_action = f"MORPH {k_table}({",".join(k_cols)}) -> ({",".join(new_cols)})"

            if best_new_I is None:
                break

            logger.info(f"  [Extend] {best_action} -> Cost: {best_new_cost:.2f} | Size: {best_new_size:.2f} MB")
            I = best_new_I
            F_current = best_new_cost
            total_size = best_new_size

        return I, F_current, total_size

    def _can_add_new(self, I: IndexSet, table: str, col: str) -> bool:
        for k_table, k_cols in I:
            if k_table == table and k_cols[0] == col:
                return False
        return True

    def _compute_ratio(self, delta_cost: float, delta_size: float) -> Optional[float]:
        if delta_cost <= 0:
            return None
        if delta_size <= 0:
            return float("inf")
        return delta_cost / delta_size

    def _passes_threshold(self, F_candidate: float, F_current: float) -> bool:
        return F_candidate * self.min_cost_improvement < F_current


def extendAlgorithm(
    conn,
    W: List[str],
    candidate_dict: Any,
    budget_mb: float = 500.0,
    storage_budget: Optional[float] = None,
    max_index_width: int = 3,
    min_cost_improvement: float = 1.003,
    write_penalties: Optional[Dict] = None,
    query_weights: Optional[Dict] = None,
    cost_cache: Optional[Dict] = None,
    size_cache: Optional[Dict] = None,
    verbose: bool = False,
    **kwargs
) -> frozenset:
    """
    Public entry point for Extend Algorithm.
    Returns frozenset[(table, (col1, ...))] matching standard configuration format.
    """
    if storage_budget is not None and storage_budget != float("inf"):
        budget_mb = float(storage_budget) / (1024.0 * 1024.0)

    candidates = extractCandidatePool(candidate_dict)
    algo = ExtendAlgorithm(
        conn=conn,
        W=W,
        budget_mb=budget_mb,
        max_index_width=max_index_width,
        min_cost_improvement=min_cost_improvement,
        write_penalties=write_penalties,
        query_weights=query_weights,
        cost_cache=cost_cache,
        size_cache=size_cache,
        verbose=verbose,
    )
    final_indexes, _, _ = algo.run(candidates)
    return frozenset((table, tuple(cols)) for table, cols in final_indexes)


def selectConfiguration(conn, W, candidate_dict, **kwargs) -> frozenset:
    """Standard interface for configuration selection."""
    return extendAlgorithm(conn, W, candidate_dict, **kwargs)


def greedyMK(conn, W, candidate_dict, **kwargs) -> frozenset:
    """Drop-in alias for greedyMK."""
    return extendAlgorithm(conn, W, candidate_dict, **kwargs)
