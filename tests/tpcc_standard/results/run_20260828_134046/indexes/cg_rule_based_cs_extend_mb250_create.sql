CREATE INDEX IF NOT EXISTS ais_tpcc_std_customer_c_id_c_w_id ON customer (c_id, c_w_id);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_customer_c_w_id_c_last ON customer (c_w_id, c_last);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_item_i_id ON item (i_id);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_new_orders_no_w_id ON new_orders (no_w_id);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_order_line_ol_o_id ON order_line (ol_o_id);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_order_line_ol_w_id ON order_line (ol_w_id);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_orders_o_c_id_o_w_id ON orders (o_c_id, o_w_id);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_stock_s_i_id ON stock (s_i_id);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_stock_s_w_id ON stock (s_w_id);
