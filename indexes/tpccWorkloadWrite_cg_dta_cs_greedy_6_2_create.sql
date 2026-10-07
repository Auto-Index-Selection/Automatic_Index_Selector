create index if not exists customer_c_id_c_w_id_c_d_id_idx on customer(c_id,c_w_id,c_d_id);
create index if not exists customer_c_w_id_idx on customer(c_w_id);
create index if not exists stock_s_quantity_idx on stock(s_quantity);
create index if not exists stock_s_w_id_s_i_id_idx on stock(s_w_id,s_i_id);
create index if not exists warehouse_w_id_idx on warehouse(w_id);
