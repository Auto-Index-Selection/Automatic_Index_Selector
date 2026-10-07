create index if not exists customer_c_balance_c_w_id_idx on customer(c_balance,c_w_id);
create index if not exists item_i_id_i_name_idx on item(i_id,i_name);
create index if not exists order_line_ol_amount_idx on order_line(ol_amount);
create index if not exists order_line_ol_i_id_ol_amount_idx on order_line(ol_i_id,ol_amount);
create index if not exists stock_s_w_id_s_i_id_s_quantity_idx on stock(s_w_id,s_i_id,s_quantity);
