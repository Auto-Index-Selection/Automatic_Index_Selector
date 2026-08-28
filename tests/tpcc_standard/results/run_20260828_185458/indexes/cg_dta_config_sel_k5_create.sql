CREATE INDEX IF NOT EXISTS ais_tpcc_std_customer_c_w_id_c_id_c_d_id ON customer (c_w_id, c_id, c_d_id);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_customer_c_w_id_c_last_c_d_id ON customer (c_w_id, c_last, c_d_id);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_order_line_ol_o_id_ol_w_id_ol_d_id ON order_line (ol_o_id, ol_w_id, ol_d_id);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_orders_o_c_id_o_w_id_o_d_id ON orders (o_c_id, o_w_id, o_d_id);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_stock_s_i_id_s_quantity ON stock (s_i_id, s_quantity);
