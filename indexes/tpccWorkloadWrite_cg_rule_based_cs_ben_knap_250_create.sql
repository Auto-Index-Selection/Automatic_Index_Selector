create index if not exists customer_c_w_id_idx on customer(c_w_id);
create index if not exists order_line_ol_delivery_d_idx on order_line(ol_delivery_d);
create index if not exists order_line_ol_w_id_ol_delivery_d_idx on order_line(ol_w_id,ol_delivery_d);
create index if not exists stock_s_i_id_s_quantity_idx on stock(s_i_id,s_quantity);
create index if not exists stock_s_quantity_idx on stock(s_quantity);
create index if not exists stock_s_w_id_s_quantity_idx on stock(s_w_id,s_quantity);
