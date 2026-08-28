-- TC5: Order-Status Transaction — Customer Latest Order Header by ID
SELECT o_id, o_entry_d, o_carrier_id 
FROM orders 
WHERE o_w_id = 1 AND o_d_id = 2 AND o_c_id = 850 
ORDER BY o_id DESC 
LIMIT 1;
