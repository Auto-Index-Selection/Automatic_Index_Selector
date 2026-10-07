-- type: read
SELECT ol_o_id, ol_i_id, ol_amount FROM order_line WHERE ol_w_id=3 AND ol_delivery_d > now() - interval '1 days';
