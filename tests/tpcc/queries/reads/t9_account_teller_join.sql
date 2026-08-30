SELECT a.aid, a.abalance, t.tid, t.tbalance
FROM pgbench_accounts a
JOIN pgbench_tellers t ON a.bid = t.bid
WHERE t.bid = 5
ORDER BY a.aid
LIMIT 100;
