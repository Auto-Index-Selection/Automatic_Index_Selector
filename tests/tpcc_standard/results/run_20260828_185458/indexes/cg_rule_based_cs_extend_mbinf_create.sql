CREATE INDEX IF NOT EXISTS ais_tpcc_std_customer_c_id_c_d_id_c_w_id ON customer (c_id, c_d_id, c_w_id);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_customer_c_last_c_d_id_c_w_id ON customer (c_last, c_d_id, c_w_id);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_district_d_next_o_id_d_w_id ON district (d_next_o_id, d_w_id);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_item_i_id ON item (i_id);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_new_orders_no_d_id_no_w_id ON new_orders (no_d_id, no_w_id);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_order_line_ol_d_id_ol_w_id_ol_o_id ON order_line (ol_d_id, ol_w_id, ol_o_id);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_order_line_ol_o_id ON order_line (ol_o_id);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_orders_o_c_id_o_d_id_o_w_id ON orders (o_c_id, o_d_id, o_w_id);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_stock_s_i_id_s_quantity ON stock (s_i_id, s_quantity);
