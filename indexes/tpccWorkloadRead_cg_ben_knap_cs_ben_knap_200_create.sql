create index if not exists item_i_price_idx on item(i_price);
create index if not exists order_line_ol_amount_idx on order_line(ol_amount);
create index if not exists orders_o_w_id_idx on orders(o_w_id);
create index if not exists stock_s_i_id_s_quantity_idx on stock(s_i_id,s_quantity);
create index if not exists stock_s_i_id_s_w_id_s_quantity_idx on stock(s_i_id,s_w_id,s_quantity);
create index if not exists stock_s_quantity_idx on stock(s_quantity);
create index if not exists stock_s_w_id_s_quantity_idx on stock(s_w_id,s_quantity);
