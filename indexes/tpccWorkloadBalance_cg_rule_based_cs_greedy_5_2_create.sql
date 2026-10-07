create index if not exists customer_c_w_id_c_balance_idx on customer(c_w_id,c_balance);
create index if not exists item_i_id_idx on item(i_id);
create index if not exists order_line_ol_amount_idx on order_line(ol_amount);
