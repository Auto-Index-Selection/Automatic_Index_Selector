create index if not exists customer_c_d_id_c_id_c_w_id_idx on customer(c_d_id,c_id,c_w_id);
create index if not exists item_i_id_i_price_idx on item(i_id,i_price);
create index if not exists item_i_price_idx on item(i_price);
create index if not exists orders_o_w_id_idx on orders(o_w_id);
create index if not exists stock_s_i_id_s_quantity_idx on stock(s_i_id,s_quantity);
create index if not exists stock_s_quantity_idx on stock(s_quantity);
create index if not exists stock_s_w_id_s_quantity_idx on stock(s_w_id,s_quantity);
