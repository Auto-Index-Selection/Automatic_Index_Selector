create index on customer(c_w_id);
create index on order_line(ol_delivery_d);
create index on order_line(ol_w_id,ol_delivery_d);
create index on stock(s_i_id,s_quantity);
create index on stock(s_quantity);
create index on stock(s_w_id,s_quantity);
create index on warehouse(w_id);
