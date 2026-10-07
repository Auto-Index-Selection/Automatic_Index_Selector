create index if not exists customer_c_balance_idx on customer(c_balance);
create index if not exists customer_c_d_id_c_w_id_idx on customer(c_d_id,c_w_id);
create index if not exists customer_c_w_id_idx on customer(c_w_id);
create index if not exists customer_c_w_id_c_balance_idx on customer(c_w_id,c_balance);
create index if not exists order_line_ol_amount_idx on order_line(ol_amount);
create index if not exists order_line_ol_w_id_idx on order_line(ol_w_id);
