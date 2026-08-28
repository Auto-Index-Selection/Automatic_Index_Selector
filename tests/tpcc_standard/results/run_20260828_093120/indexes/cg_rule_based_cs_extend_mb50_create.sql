CREATE INDEX IF NOT EXISTS ais_tpcc_std_customer_c_last_c_d_id ON customer (c_last, c_d_id);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_orders_o_c_id_o_d_id ON orders (o_c_id, o_d_id);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_stock_s_i_id ON stock (s_i_id);
