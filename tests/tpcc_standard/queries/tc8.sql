-- TC8: Delivery Transaction — Find Oldest Pending Order in District
SELECT no_o_id 
FROM new_orders 
WHERE no_w_id = 1 AND no_d_id = 4 
ORDER BY no_o_id ASC 
LIMIT 1;
