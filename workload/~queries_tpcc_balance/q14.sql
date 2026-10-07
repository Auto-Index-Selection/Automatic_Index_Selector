-- type: write
DELETE FROM order_line WHERE ol_w_id=4 AND ol_delivery_d < now() - interval '180 days';
