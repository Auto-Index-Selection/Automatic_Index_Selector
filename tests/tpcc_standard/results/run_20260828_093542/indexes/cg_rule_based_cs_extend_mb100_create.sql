CREATE INDEX IF NOT EXISTS ais_tpcc_std_customer_c_last ON customer (c_last);
CREATE INDEX IF NOT EXISTS ais_tpcc_std_orders_o_c_id_o_w_id_o_d_id ON orders (o_c_id, o_w_id, o_d_id);
