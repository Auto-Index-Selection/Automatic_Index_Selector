CREATE INDEX IF NOT EXISTS ais_tpcc_std_customer_c_id_c_w_id_c_d_id ON customer (c_id, c_w_id, c_d_id);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_customer_c_w_id_c_last ON customer (c_w_id, c_last);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_stock_s_i_id_s_w_id_s_quantity ON stock (s_i_id, s_w_id, s_quantity);
