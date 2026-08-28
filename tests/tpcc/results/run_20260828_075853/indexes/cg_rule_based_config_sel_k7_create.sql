CREATE INDEX IF NOT EXISTS ais_test_pgbench_accounts_aid ON pgbench_accounts (aid);
CREATE INDEX IF NOT EXISTS ais_test_pgbench_accounts_bid_aid ON pgbench_accounts (bid, aid);
CREATE INDEX IF NOT EXISTS ais_test_pgbench_tellers_bid ON pgbench_tellers (bid);
CREATE INDEX IF NOT EXISTS ais_test_pgbench_tellers_tid ON pgbench_tellers (tid);
