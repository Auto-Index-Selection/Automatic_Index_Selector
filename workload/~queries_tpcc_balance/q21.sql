-- type: write
INSERT INTO orders (o_id, o_d_id, o_w_id, o_c_id, o_entry_d, o_carrier_id, o_ol_cnt, o_all_local) SELECT 146530 + gs, (gs % 10) + 1, 4, ((gs * 37) % 3000) + 1, now(), NULL, 10, 1 FROM generate_series(1, 2000) AS gs;
