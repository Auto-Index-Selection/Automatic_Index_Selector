CREATE INDEX IF NOT EXISTS ais_test_pgbench_history_aid ON pgbench_history (aid);
CREATE INDEX IF NOT EXISTS ais_test_pgbench_tellers_bid ON pgbench_tellers (bid);
CREATE INDEX IF NOT EXISTS ais_test_pgbench_tellers_bid_bid ON pgbench_tellers (bid, bid);
CREATE INDEX IF NOT EXISTS ais_test_pgbench_tellers_bid_tid ON pgbench_tellers (bid, tid);
CREATE INDEX IF NOT EXISTS ais_test_pgbench_tellers_tid ON pgbench_tellers (tid);
