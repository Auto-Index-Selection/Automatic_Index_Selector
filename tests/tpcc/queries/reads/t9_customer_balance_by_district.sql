-- Analytical: outstanding balance per district across a warehouse.
SELECT c_d_id, COUNT(*) AS customers, SUM(c_balance) AS total_balance
FROM customer
WHERE c_w_id = 5
GROUP BY c_d_id
ORDER BY c_d_id;
