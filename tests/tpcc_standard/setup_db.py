"""
tests/tpcc_standard/setup_db.py
Creates and populates tpcc_standard_db with the official 9 TPC-C tables:
  1. warehouse
  2. district
  3. customer
  4. history
  5. new_orders
  6. orders
  7. order_line
  8. item
  9. stock

Usage:
  PYTHONPATH=src python tests/tpcc_standard/setup_db.py --warehouses 2
"""
from __future__ import annotations

import argparse
import io
import os
import random
import string
import sys
import time
from pathlib import Path
from typing import List

import psycopg2
from dotenv import load_dotenv

# Set random seed for reproducibility
random.seed(42)

DDL_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS hypopg;

DROP TABLE IF EXISTS order_line CASCADE;
DROP TABLE IF EXISTS new_orders CASCADE;
DROP TABLE IF EXISTS orders CASCADE;
DROP TABLE IF EXISTS history CASCADE;
DROP TABLE IF EXISTS customer CASCADE;
DROP TABLE IF EXISTS district CASCADE;
DROP TABLE IF EXISTS stock CASCADE;
DROP TABLE IF EXISTS item CASCADE;
DROP TABLE IF EXISTS warehouse CASCADE;

CREATE TABLE warehouse (
    w_id        INTEGER,
    w_name      VARCHAR(10),
    w_street_1  VARCHAR(20),
    w_street_2  VARCHAR(20),
    w_city      VARCHAR(20),
    w_state     CHAR(2),
    w_zip       CHAR(9),
    w_tax       DECIMAL(4,4),
    w_ytd       DECIMAL(12,2)
);

CREATE TABLE district (
    d_id        INTEGER,
    d_w_id      INTEGER,
    d_name      VARCHAR(10),
    d_street_1  VARCHAR(20),
    d_street_2  VARCHAR(20),
    d_city      VARCHAR(20),
    d_state     CHAR(2),
    d_zip       CHAR(9),
    d_tax       DECIMAL(4,4),
    d_ytd       DECIMAL(12,2),
    d_next_o_id INTEGER
);

CREATE TABLE customer (
    c_id            INTEGER,
    c_d_id          INTEGER,
    c_w_id          INTEGER,
    c_first         VARCHAR(16),
    c_middle        CHAR(2),
    c_last          VARCHAR(16),
    c_street_1      VARCHAR(20),
    c_street_2      VARCHAR(20),
    c_city          VARCHAR(20),
    c_state         CHAR(2),
    c_zip           CHAR(9),
    c_phone         CHAR(16),
    c_since         TIMESTAMP,
    c_credit        CHAR(2),
    c_credit_lim    DECIMAL(12,2),
    c_discount      DECIMAL(4,4),
    c_balance       DECIMAL(12,2),
    c_ytd_payment   DECIMAL(12,2),
    c_payment_cnt   INTEGER,
    c_delivery_cnt  INTEGER,
    c_data          VARCHAR(500)
);

CREATE TABLE history (
    h_c_id      INTEGER,
    h_c_d_id    INTEGER,
    h_c_w_id    INTEGER,
    h_d_id      INTEGER,
    h_w_id      INTEGER,
    h_date      TIMESTAMP,
    h_amount    DECIMAL(6,2),
    h_data      VARCHAR(24)
);

CREATE TABLE orders (
    o_id            INTEGER,
    o_d_id          INTEGER,
    o_w_id          INTEGER,
    o_c_id          INTEGER,
    o_entry_d       TIMESTAMP,
    o_carrier_id    INTEGER,
    o_ol_cnt        INTEGER,
    o_all_local     INTEGER
);

CREATE TABLE new_orders (
    no_o_id     INTEGER,
    no_d_id     INTEGER,
    no_w_id     INTEGER
);

CREATE TABLE item (
    i_id        INTEGER,
    i_im_id     INTEGER,
    i_name      VARCHAR(24),
    i_price     DECIMAL(5,2),
    i_data      VARCHAR(50)
);

CREATE TABLE stock (
    s_i_id          INTEGER,
    s_w_id          INTEGER,
    s_quantity      INTEGER,
    s_dist_01       CHAR(24),
    s_dist_02       CHAR(24),
    s_dist_03       CHAR(24),
    s_dist_04       CHAR(24),
    s_dist_05       CHAR(24),
    s_dist_06       CHAR(24),
    s_dist_07       CHAR(24),
    s_dist_08       CHAR(24),
    s_dist_09       CHAR(24),
    s_dist_10       CHAR(24),
    s_ytd           INTEGER,
    s_order_cnt     INTEGER,
    s_remote_cnt    INTEGER,
    s_data          VARCHAR(50)
);

CREATE TABLE order_line (
    ol_o_id         INTEGER,
    ol_d_id         INTEGER,
    ol_w_id         INTEGER,
    ol_number       INTEGER,
    ol_i_id         INTEGER,
    ol_supply_w_id  INTEGER,
    ol_delivery_d   TIMESTAMP,
    ol_quantity     INTEGER,
    ol_amount       DECIMAL(6,2),
    ol_dist_info    CHAR(24)
);
"""

LAST_NAMES = [
    "BAR", "OUGHT", "ABLE", "PRI", "PRES", "ESE", "ANTI", "CALLY", "ATION", "EING"
]

def make_last_name(num: int) -> str:
    return LAST_NAMES[num // 100] + LAST_NAMES[(num // 10) % 10] + LAST_NAMES[num % 10]

def rand_str(length: int) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))

def rand_num_str(length: int) -> str:
    return "".join(random.choices(string.digits, k=length))


def copy_from_string_buffer(cur, table: str, columns: List[str], buffer: io.StringIO) -> None:
    buffer.seek(0)
    col_str = ", ".join(columns)
    cur.copy_expert(f"COPY {table} ({col_str}) FROM STDIN WITH (FORMAT CSV, DELIMITER '\t', NULL '')", buffer)


def generate_tpcc_data(conn, num_warehouses: int = 2) -> None:
    print(f"Generating TPC-C dataset for {num_warehouses} warehouse(s)...")
    cur = conn.cursor()

    # 1. ITEM (100,000 items global)
    print("  Generating 100,000 items...")
    item_buf = io.StringIO()
    for i_id in range(1, 100001):
        i_im_id = random.randint(1, 10000)
        i_name = rand_str(14)
        i_price = round(random.uniform(1.0, 100.0), 2)
        i_data = rand_str(random.randint(26, 50))
        item_buf.write(f"{i_id}\t{i_im_id}\t{i_name}\t{i_price}\t{i_data}\n")
    copy_from_string_buffer(cur, "item", ["i_id", "i_im_id", "i_name", "i_price", "i_data"], item_buf)
    conn.commit()

    # Per Warehouse Data
    for w_id in range(1, num_warehouses + 1):
        print(f"\n--- Loading Warehouse {w_id}/{num_warehouses} ---")

        # WAREHOUSE
        w_name = rand_str(8)
        w_street_1 = rand_str(15)
        w_street_2 = rand_str(15)
        w_city = rand_str(12)
        w_state = "CA"
        w_zip = "941071111"
        w_tax = round(random.uniform(0.05, 0.15), 4)
        w_ytd = 300000.00
        cur.execute(
            "INSERT INTO warehouse VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (w_id, w_name, w_street_1, w_street_2, w_city, w_state, w_zip, w_tax, w_ytd)
        )

        # STOCK (100,000 per warehouse)
        print(f"  Generating 100,000 stock rows for warehouse {w_id}...")
        stock_buf = io.StringIO()
        for i_id in range(1, 100001):
            s_qty = random.randint(10, 100)
            dists = [rand_str(24) for _ in range(10)]
            stock_buf.write(f"{i_id}\t{w_id}\t{s_qty}\t" + "\t".join(dists) + f"\t0\t0\t0\t{rand_str(30)}\n")
        copy_from_string_buffer(
            cur, "stock",
            ["s_i_id", "s_w_id", "s_quantity", "s_dist_01", "s_dist_02", "s_dist_03", "s_dist_04", "s_dist_05",
             "s_dist_06", "s_dist_07", "s_dist_08", "s_dist_09", "s_dist_10", "s_ytd", "s_order_cnt", "s_remote_cnt", "s_data"],
            stock_buf
        )
        conn.commit()

        # DISTRICTS (10 per warehouse)
        for d_id in range(1, 11):
            d_name = rand_str(8)
            d_tax = round(random.uniform(0.05, 0.15), 4)
            d_next_o_id = 3001
            cur.execute(
                "INSERT INTO district VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (d_id, w_id, d_name, rand_str(15), rand_str(15), rand_str(12), "CA", "941071111", d_tax, 30000.0, d_next_o_id)
            )

            # CUSTOMER (3,000 per district)
            print(f"  District {d_id}: Generating 3,000 customers, orders, and order_lines...")
            cust_buf = io.StringIO()
            hist_buf = io.StringIO()
            for c_id in range(1, 3001):
                c_last = make_last_name(c_id - 1 if c_id <= 1000 else random.randint(0, 999))
                c_first = rand_str(10)
                c_credit = "BC" if random.random() < 0.1 else "GC"
                c_disc = round(random.uniform(0.0, 0.5), 4)
                cust_buf.write(
                    f"{c_id}\t{d_id}\t{w_id}\t{c_first}\tOE\t{c_last}\t"
                    f"{rand_str(15)}\t{rand_str(15)}\t{rand_str(12)}\tCA\t941071111\t"
                    f"1234567890123456\t2026-01-01 00:00:00\t{c_credit}\t50000.00\t"
                    f"{c_disc}\t-10.00\t10.00\t1\t0\t{rand_str(100)}\n"
                )
                hist_buf.write(f"{c_id}\t{d_id}\t{w_id}\t{d_id}\t{w_id}\t2026-01-01 00:00:00\t10.00\t{rand_str(18)}\n")

            copy_from_string_buffer(
                cur, "customer",
                ["c_id", "c_d_id", "c_w_id", "c_first", "c_middle", "c_last", "c_street_1", "c_street_2", "c_city",
                 "c_state", "c_zip", "c_phone", "c_since", "c_credit", "c_credit_lim", "c_discount", "c_balance",
                 "c_ytd_payment", "c_payment_cnt", "c_delivery_cnt", "c_data"],
                cust_buf
            )
            copy_from_string_buffer(
                cur, "history",
                ["h_c_id", "h_c_d_id", "h_c_w_id", "h_d_id", "h_w_id", "h_date", "h_amount", "h_data"],
                hist_buf
            )

            # ORDERS (3,000 per district) & NEW_ORDERS & ORDER_LINE
            orders_buf = io.StringIO()
            new_orders_buf = io.StringIO()
            order_line_buf = io.StringIO()

            cust_perm = list(range(1, 3001))
            random.shuffle(cust_perm)

            for o_id in range(1, 3001):
                o_c_id = cust_perm[o_id - 1]
                o_carrier_id = random.randint(1, 10) if o_id < 2101 else ""
                o_ol_cnt = 10
                orders_buf.write(
                    f"{o_id}\t{d_id}\t{w_id}\t{o_c_id}\t2026-01-01 00:00:00\t"
                    f"{o_carrier_id}\t{o_ol_cnt}\t1\n"
                )
                if o_id >= 2101:
                    new_orders_buf.write(f"{o_id}\t{d_id}\t{w_id}\n")

                for ol_num in range(1, o_ol_cnt + 1):
                    ol_i_id = random.randint(1, 100000)
                    ol_del_d = "2026-01-01 00:00:00" if o_id < 2101 else ""
                    ol_amount = 0.0 if o_id < 2101 else round(random.uniform(1.0, 9999.99), 2)
                    order_line_buf.write(
                        f"{o_id}\t{d_id}\t{w_id}\t{ol_num}\t{ol_i_id}\t{w_id}\t"
                        f"{ol_del_d}\t5\t{ol_amount}\t{rand_str(24)}\n"
                    )

            copy_from_string_buffer(
                cur, "orders",
                ["o_id", "o_d_id", "o_w_id", "o_c_id", "o_entry_d", "o_carrier_id", "o_ol_cnt", "o_all_local"],
                orders_buf
            )
            copy_from_string_buffer(cur, "new_orders", ["no_o_id", "no_d_id", "no_w_id"], new_orders_buf)
            copy_from_string_buffer(
                cur, "order_line",
                ["ol_o_id", "ol_d_id", "ol_w_id", "ol_number", "ol_i_id", "ol_supply_w_id", "ol_delivery_d", "ol_quantity", "ol_amount", "ol_dist_info"],
                order_line_buf
            )
            conn.commit()

    print("\nRunning ANALYZE across all 9 tables...")
    cur.execute("ANALYZE;")
    conn.commit()
    print("Database population complete!")


def main():
    parser = argparse.ArgumentParser(description="Setup TPC-C Standard Database")
    parser.add_argument("--warehouses", "-w", type=int, default=2, help="Number of warehouses (Scale Factor)")
    parser.add_argument("--dbname", type=str, default="tpcc_standard_db")
    args = parser.parse_args()

    load_dotenv()
    db_user = os.getenv("DB_USER", "postgres")
    db_pass = os.getenv("DB_PASSWORD", "123")
    db_host = os.getenv("DB_HOST", "localhost")
    db_port = os.getenv("DB_PORT", "5432")

    # Connect to default postgres to create database if not present
    conn = psycopg2.connect(dbname="postgres", user=db_user, password=db_pass, host=db_host, port=db_port)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(f"SELECT 1 FROM pg_database WHERE datname = '{args.dbname}'")
    if not cur.fetchone():
        print(f"Creating database {args.dbname}...")
        cur.execute(f"CREATE DATABASE {args.dbname};")
    conn.close()

    # Connect to target database and build schema
    target_conn = psycopg2.connect(dbname=args.dbname, user=db_user, password=db_pass, host=db_host, port=db_port)
    target_cur = target_conn.cursor()
    print(f"Applying official 9-table TPC-C DDL schema to {args.dbname}...")
    target_cur.execute(DDL_SCHEMA)
    target_conn.commit()

    t0 = time.time()
    generate_tpcc_data(target_conn, num_warehouses=args.warehouses)
    elapsed = time.time() - t0
    print(f"\n[DONE] Built {args.dbname} (Scale={args.warehouses} W) in {elapsed:.2f} seconds.")
    target_conn.close()


if __name__ == "__main__":
    main()
