-- TC7: Order-Status Transaction — Order Line Items Batch Fetch
SELECT ol_i_id, ol_supply_w_id, ol_quantity, ol_amount, ol_delivery_d 
FROM order_line 
WHERE ol_w_id = 1 AND ol_d_id = 2 AND ol_o_id = 2100;
