CREATE INDEX IF NOT EXISTS ais_test_pgbench_accounts_bid ON pgbench_accounts (bid);
CREATE INDEX IF NOT EXISTS ais_test_pgbench_accounts_bid_aid ON pgbench_accounts (bid, aid);
CREATE INDEX IF NOT EXISTS ais_test_pgbench_tellers_bid ON pgbench_tellers (bid);
