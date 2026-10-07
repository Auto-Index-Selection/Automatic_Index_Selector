create index if not exists customer_c_w_id_idx on customer(c_w_id);
create index if not exists stock_s_quantity_idx on stock(s_quantity);
create index if not exists stock_s_w_id_idx on stock(s_w_id);
create index if not exists stock_s_w_id_s_quantity_idx on stock(s_w_id,s_quantity);
create index if not exists warehouse_w_id_idx on warehouse(w_id);
