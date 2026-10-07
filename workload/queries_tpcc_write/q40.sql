-- type: write
INSERT INTO order_line (ol_o_id, ol_d_id, ol_w_id, ol_number, ol_i_id, ol_supply_w_id, ol_delivery_d, ol_quantity, ol_amount, ol_dist_info) SELECT 110217 + gs, (gs % 10) + 1, 1, ln, ((gs * 13 + ln) % 100000) + 1, 1, now(), 5, ROUND((random() * 1000)::numeric, 2), 'dist_info_str' FROM generate_series(1, 5000) AS gs, generate_series(1, 10) AS ln;
