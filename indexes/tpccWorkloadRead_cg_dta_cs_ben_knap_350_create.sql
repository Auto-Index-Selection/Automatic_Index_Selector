create index on order_line(ol_amount);
create index on order_line(ol_delivery_d);
create index on order_line(ol_i_id);
create index on stock(s_i_id,s_w_id,s_quantity);
create index on stock(s_quantity);
create index on stock(s_w_id,s_i_id,s_quantity);
create index on stock(s_w_id,s_quantity);
