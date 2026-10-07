-- type: write
UPDATE order_line SET ol_amount = ROUND((ol_amount * 1.05)::numeric, 2) WHERE ol_w_id=6;
