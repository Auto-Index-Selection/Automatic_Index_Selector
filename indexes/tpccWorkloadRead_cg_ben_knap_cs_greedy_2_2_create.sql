create index if not exists order_line_ol_amount_idx on order_line(ol_amount);
create index if not exists order_line_ol_i_id_ol_w_id_ol_delivery_d_idx on order_line(ol_i_id,ol_w_id,ol_delivery_d);
