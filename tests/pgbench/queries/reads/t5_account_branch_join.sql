SELECT a.aid, a.abalance, b.bbalance
FROM pgbench_accounts a
JOIN pgbench_branches b ON a.bid = b.bid
WHERE b.bid = 10
ORDER BY a.aid
LIMIT 50;
