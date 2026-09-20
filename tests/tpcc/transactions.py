"""
tests/tpcc/transactions.py
--------------------------
The three canonical TPC-C write transactions, used to drive the observation
window so the write-penalty model sees a realistic OLTP workload.

Parameters come from a *seeded* RNG, so the writes spread across the tables
while staying reproducible run to run. This is deliberate: the pgbench suite's
DML only ever touched `aid=1` / `tid=1` / `bid=1`, so every candidate index came
back with `write_penalty = 0.0000`. Here the UPDATEs carry multi-column SET
lists across many rows, which is what `advisor_get_column_set_stats` tracks and
what the HOT-update branch of the penalty model needs in order to distinguish
an index that a write touches from one it does not.
"""
from __future__ import annotations

import logging
import random
from typing import Callable, Dict, List

logger = logging.getLogger(__name__)

# Cardinalities of the loaded tpcc_standard_db (10 warehouses).
NUM_WAREHOUSES = 10
DISTRICTS_PER_WAREHOUSE = 10
CUSTOMERS_PER_DISTRICT = 3000
NUM_ITEMS = 100000


def new_order(cur, rng: random.Random) -> str:
    """
    New-Order: the write-heaviest TPC-C transaction.

    Touches district (d_next_o_id), orders, new_orders, order_line and stock —
    five tables, three of them with multi-column SET lists.
    """
    w_id = rng.randint(1, NUM_WAREHOUSES)
    d_id = rng.randint(1, DISTRICTS_PER_WAREHOUSE)
    c_id = rng.randint(1, CUSTOMERS_PER_DISTRICT)
    ol_cnt = rng.randint(5, 15)

    # Reserve the next order id the way TPC-C does, so o_id stays unique even
    # when the primary keys are present.
    cur.execute(
        "SELECT d_next_o_id FROM district WHERE d_w_id = %s AND d_id = %s",
        (w_id, d_id),
    )
    row = cur.fetchone()
    if row is None:
        return "new_order"
    o_id = int(row[0])

    cur.execute(
        "UPDATE district SET d_next_o_id = d_next_o_id + 1 "
        "WHERE d_w_id = %s AND d_id = %s",
        (w_id, d_id),
    )
    cur.execute(
        "INSERT INTO orders (o_id, o_d_id, o_w_id, o_c_id, o_entry_d, "
        "o_carrier_id, o_ol_cnt, o_all_local) "
        "VALUES (%s, %s, %s, %s, now(), NULL, %s, 1)",
        (o_id, d_id, w_id, c_id, ol_cnt),
    )
    cur.execute(
        "INSERT INTO new_orders (no_o_id, no_d_id, no_w_id) VALUES (%s, %s, %s)",
        (o_id, d_id, w_id),
    )

    for ol_number in range(1, ol_cnt + 1):
        i_id = rng.randint(1, NUM_ITEMS)
        qty = rng.randint(1, 10)
        cur.execute(
            "INSERT INTO order_line (ol_o_id, ol_d_id, ol_w_id, ol_number, "
            "ol_i_id, ol_supply_w_id, ol_delivery_d, ol_quantity, ol_amount, "
            "ol_dist_info) VALUES (%s, %s, %s, %s, %s, %s, NULL, %s, %s, %s)",
            (o_id, d_id, w_id, ol_number, i_id, w_id, qty,
             round(rng.uniform(1.0, 100.0), 2), f"dist-{d_id:02d}-{ol_number:03d}"),
        )
        # Multi-column SET: exactly the shape the write-penalty model reasons about.
        cur.execute(
            "UPDATE stock SET s_quantity = CASE WHEN s_quantity > %s "
            "THEN s_quantity - %s ELSE s_quantity + 91 - %s END, "
            "s_ytd = s_ytd + %s, s_order_cnt = s_order_cnt + 1 "
            "WHERE s_i_id = %s AND s_w_id = %s",
            (qty + 10, qty, qty, qty, i_id, w_id),
        )
    return "new_order"


def payment(cur, rng: random.Random) -> str:
    """
    Payment: updates warehouse, district and customer YTD totals, then records
    the payment in history. Three tables updated, one inserted.
    """
    w_id = rng.randint(1, NUM_WAREHOUSES)
    d_id = rng.randint(1, DISTRICTS_PER_WAREHOUSE)
    c_id = rng.randint(1, CUSTOMERS_PER_DISTRICT)
    amount = round(rng.uniform(1.0, 5000.0), 2)

    cur.execute("UPDATE warehouse SET w_ytd = w_ytd + %s WHERE w_id = %s",
                (amount, w_id))
    cur.execute("UPDATE district SET d_ytd = d_ytd + %s "
                "WHERE d_w_id = %s AND d_id = %s",
                (amount, w_id, d_id))
    cur.execute(
        "UPDATE customer SET c_balance = c_balance - %s, "
        "c_ytd_payment = c_ytd_payment + %s, c_payment_cnt = c_payment_cnt + 1 "
        "WHERE c_w_id = %s AND c_d_id = %s AND c_id = %s",
        (amount, amount, w_id, d_id, c_id),
    )
    cur.execute(
        "INSERT INTO history (h_c_id, h_c_d_id, h_c_w_id, h_d_id, h_w_id, "
        "h_date, h_amount, h_data) VALUES (%s, %s, %s, %s, %s, now(), %s, %s)",
        (c_id, d_id, w_id, d_id, w_id, amount, f"pay-{w_id}-{d_id}"),
    )
    return "payment"


def delivery(cur, rng: random.Random) -> str:
    """
    Delivery: clears the oldest undelivered order for a district — a DELETE plus
    three UPDATEs, including a range UPDATE over order_line.
    """
    w_id = rng.randint(1, NUM_WAREHOUSES)
    d_id = rng.randint(1, DISTRICTS_PER_WAREHOUSE)
    carrier = rng.randint(1, 10)

    cur.execute(
        "SELECT no_o_id FROM new_orders WHERE no_w_id = %s AND no_d_id = %s "
        "ORDER BY no_o_id ASC LIMIT 1",
        (w_id, d_id),
    )
    row = cur.fetchone()
    if row is None:
        return "delivery"
    o_id = int(row[0])

    cur.execute(
        "DELETE FROM new_orders WHERE no_w_id = %s AND no_d_id = %s AND no_o_id = %s",
        (w_id, d_id, o_id),
    )
    cur.execute(
        "UPDATE orders SET o_carrier_id = %s "
        "WHERE o_w_id = %s AND o_d_id = %s AND o_id = %s",
        (carrier, w_id, d_id, o_id),
    )
    cur.execute(
        "UPDATE order_line SET ol_delivery_d = now() "
        "WHERE ol_w_id = %s AND ol_d_id = %s AND ol_o_id = %s",
        (w_id, d_id, o_id),
    )
    cur.execute(
        "SELECT COALESCE(SUM(ol_amount), 0), COALESCE(MIN(ol_o_id), 0) FROM order_line "
        "WHERE ol_w_id = %s AND ol_d_id = %s AND ol_o_id = %s",
        (w_id, d_id, o_id),
    )
    total = cur.fetchone()[0] or 0
    cur.execute(
        "UPDATE customer SET c_balance = c_balance + %s, "
        "c_delivery_cnt = c_delivery_cnt + 1 "
        "WHERE c_w_id = %s AND c_d_id = %s AND c_id = "
        "(SELECT o_c_id FROM orders WHERE o_w_id = %s AND o_d_id = %s AND o_id = %s)",
        (total, w_id, d_id, w_id, d_id, o_id),
    )
    return "delivery"


# TPC-C's transaction mix, minus the two read-only ones (Order-Status and
# Stock-Level) which are covered by the read queries in queries/reads/.
TRANSACTION_MIX: List[tuple] = [
    (new_order, 10),
    (payment, 10),
    (delivery, 2),
]


def run_transaction_round(conn, rng: random.Random) -> Dict[str, int]:
    """
    Execute one round of the transaction mix.

    Each transaction commits on its own: batching them would mean a single
    failure rolls back every write accumulated so far, and the observation
    window would then measure a truncated write workload.
    """
    counts: Dict[str, int] = {}
    for fn, weight in TRANSACTION_MIX:
        for _ in range(weight):
            try:
                with conn.cursor() as cur:
                    name = fn(cur, rng)
                conn.commit()
                counts[name] = counts.get(name, 0) + 1
            except Exception as e:
                conn.rollback()
                logger.debug("Transaction %s failed: %s", fn.__name__, e)
    return counts
