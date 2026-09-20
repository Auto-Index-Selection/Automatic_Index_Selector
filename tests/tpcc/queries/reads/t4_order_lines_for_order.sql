-- Order-Status: the lines of one order (composite prefix scan).
SELECT ol_number, ol_i_id, ol_quantity, ol_amount, ol_delivery_d
FROM order_line
WHERE ol_w_id = 5 AND ol_d_id = 3 AND ol_o_id = 2000
ORDER BY ol_number;
