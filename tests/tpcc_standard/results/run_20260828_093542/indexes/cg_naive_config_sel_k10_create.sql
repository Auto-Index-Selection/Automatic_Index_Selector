CREATE INDEX IF NOT EXISTS ais_tpcc_std_customer_c_w_id_c_last ON customer (c_w_id, c_last);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_item_i_id ON item (i_id);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_orders_o_w_id_o_c_id ON orders (o_w_id, o_c_id);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_stock_s_i_id_s_w_id ON stock (s_i_id, s_w_id);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_stock_s_w_id ON stock (s_w_id);
