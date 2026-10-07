-- type: write
INSERT INTO orders (o_id, o_d_id, o_w_id, o_c_id, o_entry_d, o_carrier_id, o_ol_cnt, o_all_local) SELECT 178893 + gs, (gs % 10) + 1, 9, ((gs * 37) % 3000) + 1, now(), NULL, 10, 1 FROM generate_series(1, 5000) AS gs;
