"""
workload_runner/param_generator.py
----------------------------------
Generates repeatable bind parameters for named DML SQL templates (TPC-H and pgbench).
"""

import random
import string
from typing import Any, Callable, Dict, Optional, Sequence, Tuple


_STEM_ALIASES: Dict[str, str] = {
    "insert_suppplier": "insert_supplier",
}


class ParameterGenerator:
    """Generates repeatable bind parameters for named DML SQL templates."""

    def __init__(self, seed: int = 42) -> None:
        self._rng = random.Random(seed)
        self._generators: Dict[str, Callable[[], Sequence[Any]]] = self._build_generators()

    def generate(self, query_name: str) -> Optional[Sequence[Any]]:
        canonical = _STEM_ALIASES.get(query_name, query_name)
        handler = self._generators.get(canonical)
        if handler is not None:
            return handler()

        # Fallback for auto-generated updates (e.g. update_lineitem_l_orderkey)
        if query_name.startswith("update_") and "_" in query_name[7:]:
            return (self._rng.randint(1, 150_000),)

        return None

    def _build_generators(self) -> Dict[str, Callable[[], Sequence[Any]]]:
        return {
            # --- Single & Multi-column UPDATEs ---
            "update_customer_balance": self._update_customer_balance,
            "update_customer_phone": self._update_customer_phone,
            "update_customer_comment": self._update_customer_comment,
            "update_customer_multi": self._update_customer_multi,
            "update_customer_c_custkey": lambda: (self._rng.randint(1, 150_000),),
            "update_customer_c_nationkey": lambda: (self._rng.randint(0, 24), self._rng.randint(1, 150_000)),
            "update_customer_c_mktsegment": lambda: (self._rng.choice(['BUILDING', 'AUTOMOBILE', 'MACHINERY', 'HOUSEHOLD', 'FURNITURE']), self._rng.randint(1, 150_000)),
            "update_part_retailprice": self._update_part_retailprice,
            "update_part_p_partkey": lambda: (self._rng.randint(1, 200_000),),
            "update_part_p_brand": lambda: (f"Brand#{self._rng.randint(10, 99)}", self._rng.randint(1, 200_000)),
            "update_part_p_type": lambda: ("STANDARD ANODIZED TIN", self._rng.randint(1, 200_000)),
            "update_part_p_size": lambda: (self._rng.randint(1, 50), self._rng.randint(1, 200_000)),
            "update_part_p_container": lambda: ("SM CAN", self._rng.randint(1, 200_000)),
            "update_supplier_comment": self._update_supplier_comment,
            "update_supplier_s_suppkey": lambda: (self._rng.randint(1, 10_000),),
            "update_supplier_s_nationkey": lambda: (self._rng.randint(0, 24), self._rng.randint(1, 10_000)),
            "update_orders_totalprice": self._update_orders_totalprice,
            "update_orders_multi": self._update_orders_multi,
            "update_orders_o_orderkey": lambda: (self._rng.randint(1, 1_500_000),),
            "update_orders_o_custkey": lambda: (self._rng.randint(1, 150_000), self._rng.randint(1, 1_500_000)),
            "update_orders_o_orderstatus": lambda: (self._rng.choice(['O', 'F', 'P']), self._rng.randint(1, 1_500_000)),
            "update_orders_o_orderdate": lambda: ("1996-01-01", self._rng.randint(1, 1_500_000)),
            "update_orders_o_orderpriority": lambda: ("1-URGENT", self._rng.randint(1, 1_500_000)),
            "update_orders_o_clerk": lambda: ("Clerk#000000123", self._rng.randint(1, 1_500_000)),
            "update_orders_o_shippriority": lambda: (0, self._rng.randint(1, 1_500_000)),
            "update_lineitem_quantity": self._update_lineitem_quantity,
            "update_lineitem_shipdate": self._update_lineitem_shipdate,
            "update_lineitem_multi": self._update_lineitem_multi,
            "update_lineitem_l_orderkey": lambda: (self._rng.randint(1, 1_500_000),),
            "update_lineitem_l_partkey": lambda: (self._rng.randint(1, 200_000), self._rng.randint(1, 1_500_000), self._rng.randint(1, 7)),
            "update_lineitem_l_suppkey": lambda: (self._rng.randint(1, 10_000), self._rng.randint(1, 1_500_000), self._rng.randint(1, 7)),
            "update_lineitem_l_extendedprice": lambda: (round(self._rng.uniform(100.0, 50_000.0), 2), self._rng.randint(1, 1_500_000), self._rng.randint(1, 7)),
            "update_lineitem_l_discount": lambda: (round(self._rng.uniform(0.0, 0.1), 2), self._rng.randint(1, 1_500_000), self._rng.randint(1, 7)),
            "update_lineitem_l_tax": lambda: (round(self._rng.uniform(0.0, 0.08), 2), self._rng.randint(1, 1_500_000), self._rng.randint(1, 7)),
            "update_lineitem_l_returnflag": lambda: (self._rng.choice(['R', 'A', 'N']), self._rng.randint(1, 1_500_000), self._rng.randint(1, 7)),
            "update_lineitem_l_linestatus": lambda: (self._rng.choice(['O', 'F']), self._rng.randint(1, 1_500_000), self._rng.randint(1, 7)),
            "update_lineitem_l_commitdate": lambda: ("1996-01-01", self._rng.randint(1, 1_500_000), self._rng.randint(1, 7)),
            "update_lineitem_l_receiptdate": lambda: ("1996-01-01", self._rng.randint(1, 1_500_000), self._rng.randint(1, 7)),
            "update_lineitem_l_shipinstruct": lambda: ("DELIVER IN PERSON", self._rng.randint(1, 1_500_000), self._rng.randint(1, 7)),
            "update_lineitem_l_shipmode": lambda: ("TRUCK", self._rng.randint(1, 1_500_000), self._rng.randint(1, 7)),
            "update_nation_n_nationkey": lambda: (self._rng.randint(0, 24),),
            "update_nation_n_regionkey": lambda: (self._rng.randint(0, 4), self._rng.randint(0, 24)),
            "update_region_r_regionkey": lambda: (self._rng.randint(0, 4),),
            # --- INSERTs ---
            "insert_region": self._insert_region,
            "insert_nation": self._insert_nation,
            "insert_supplier": self._insert_supplier,
            "insert_lineitem": self._insert_lineitem,
            "insert_orders": self._insert_orders,
            # --- DELETEs ---
            "delete_region": lambda: (self._rng.randint(100, 100_000),),
            "delete_nation": lambda: (self._rng.randint(100, 100_000),),
            "delete_supplier": lambda: (self._rng.randint(100_000, 200_000),),
            "delete_lineitem": lambda: (self._rng.randint(1, 1_500_000), self._rng.randint(1, 7)),
            "delete_orders": lambda: (self._rng.randint(2_000_000, 3_000_000),),
        }

    def _update_customer_balance(self) -> Tuple[float, int]:
        return (round(self._rng.uniform(-100.0, 100.0), 2), self._rng.randint(1, 150_000))

    def _update_customer_phone(self) -> Tuple[str, int]:
        p = f"{self._rng.randint(10, 99)}-{self._rng.randint(100, 999)}-{self._rng.randint(100, 999)}-{self._rng.randint(1000, 9999)}"
        return (p, self._rng.randint(1, 150_000))

    def _update_customer_comment(self) -> Tuple[str, int]:
        comment = "".join(self._rng.choices(string.ascii_letters + " ", k=30))
        return (comment, self._rng.randint(1, 150_000))

    def _update_customer_multi(self) -> Tuple:
        p = f"{self._rng.randint(10, 99)}-{self._rng.randint(100, 999)}-{self._rng.randint(100, 999)}-{self._rng.randint(1000, 9999)}"
        segments = ['AUTOMOBILE', 'BUILDING', 'FURNITURE', 'HOUSEHOLD', 'MACHINERY']
        return (round(self._rng.uniform(-100.0, 100.0), 2), p, self._rng.choice(segments), self._rng.randint(1, 150_000))

    def _update_part_retailprice(self) -> Tuple[float, int]:
        return (round(self._rng.uniform(-50.0, 50.0), 2), self._rng.randint(1, 200_000))

    def _update_supplier_comment(self) -> Tuple[str, int]:
        c = "".join(self._rng.choices(string.ascii_letters + " ", k=30))
        return (c, self._rng.randint(1, 10_000))

    def _update_orders_totalprice(self) -> Tuple[float, int]:
        return (round(self._rng.uniform(-100.0, 100.0), 2), self._rng.randint(1, 1_500_000))

    def _update_orders_multi(self) -> Tuple:
        return (round(self._rng.uniform(-100.0, 100.0), 2), self._rng.choice(['O', 'F', 'P']), f"Clerk#{self._rng.randint(1, 1000):09d}", self._rng.randint(1, 1_500_000))

    def _update_lineitem_quantity(self) -> Tuple[float, int, int]:
        return (round(self._rng.uniform(-5.0, 5.0), 2), self._rng.randint(1, 1_500_000), self._rng.randint(1, 7))

    def _update_lineitem_shipdate(self) -> Tuple[int, int, int]:
        return (self._rng.randint(-5, 5), self._rng.randint(1, 1_500_000), self._rng.randint(1, 7))

    def _update_lineitem_multi(self) -> Tuple:
        return (round(self._rng.uniform(-5.0, 5.0), 2), round(self._rng.uniform(-0.05, 0.05), 4), round(self._rng.uniform(-100.0, 100.0), 2), self._rng.randint(1, 1_500_000), self._rng.randint(1, 7))

    def _insert_region(self) -> Tuple[int, str, str]:
        key = self._rng.randint(100, 100_000)
        return (key, f"Region_{key}", "Generated Region")

    def _insert_nation(self) -> Tuple[int, str, int, str]:
        key = self._rng.randint(100, 100_000)
        return (key, f"Nation_{key}", self._rng.randint(0, 4), "Generated Nation")

    def _insert_supplier(self) -> Tuple[int, str, str, int, str, float, str]:
        key = self._rng.randint(100_000, 200_000)
        return (key, f"Supplier_{key}", "Address", self._rng.randint(0, 24), "11-222-333-4444", round(self._rng.uniform(0.0, 10_000.0), 2), "Generated Supplier")

    def _insert_lineitem(self) -> Tuple:
        return (self._rng.randint(1, 1_500_000), self._rng.randint(1, 200_000), self._rng.randint(1, 10_000), self._rng.randint(1, 7), round(self._rng.uniform(1.0, 50.0), 2), round(self._rng.uniform(100.0, 50_000.0), 2), round(self._rng.uniform(0.0, 0.1), 2), round(self._rng.uniform(0.0, 0.08), 2), "N", "O", "1996-01-01", "1996-01-01", "1996-01-01", "DELIVER IN PERSON", "TRUCK", "Generated Lineitem")

    def _insert_orders(self) -> Tuple:
        return (self._rng.randint(2_000_000, 3_000_000), self._rng.randint(1, 150_000), "O", round(self._rng.uniform(100.0, 50_000.0), 2), "1996-01-01", "1-URGENT", "Clerk#000000123", 0, "Generated Order")
