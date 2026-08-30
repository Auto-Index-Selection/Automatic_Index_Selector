SELECT bid, COUNT(*) AS cnt, SUM(abalance) AS total
FROM pgbench_accounts
GROUP BY bid
ORDER BY bid;
