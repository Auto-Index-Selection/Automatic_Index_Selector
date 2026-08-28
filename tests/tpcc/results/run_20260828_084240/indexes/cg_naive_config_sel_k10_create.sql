CREATE INDEX IF NOT EXISTS ais_test_pgbench_accounts_aid_bid ON pgbench_accounts (aid, bid);
CREATE INDEX IF NOT EXISTS ais_test_pgbench_accounts_bid ON pgbench_accounts (bid);
CREATE INDEX IF NOT EXISTS ais_test_pgbench_tellers_bid ON pgbench_tellers (bid);
CREATE INDEX IF NOT EXISTS ais_test_pgbench_tellers_tid ON pgbench_tellers (tid);
