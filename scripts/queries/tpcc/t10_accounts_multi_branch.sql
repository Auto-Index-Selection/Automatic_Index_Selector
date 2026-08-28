SELECT aid, bid, abalance FROM pgbench_accounts
WHERE bid IN (1, 2, 3, 4, 5)
ORDER BY bid, abalance DESC
LIMIT 500;
