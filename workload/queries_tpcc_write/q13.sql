-- type: write
UPDATE order_line SET ol_delivery_d = now() WHERE ol_w_id=5 AND ol_d_id=2 AND ol_o_id=865;
