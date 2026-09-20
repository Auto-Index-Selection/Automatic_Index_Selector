-- Analytical: per-district order counts for one warehouse.
SELECT o_d_id, COUNT(*) AS orders, AVG(o_ol_cnt) AS avg_lines
FROM orders
WHERE o_w_id = 5
GROUP BY o_d_id
ORDER BY o_d_id;
