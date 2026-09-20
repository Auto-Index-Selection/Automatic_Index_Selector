-- Order-Status: most recent order for one customer.
SELECT o_id, o_entry_d, o_carrier_id, o_ol_cnt
FROM orders
WHERE o_w_id = 5 AND o_d_id = 3 AND o_c_id = 1500
ORDER BY o_id DESC
LIMIT 1;
