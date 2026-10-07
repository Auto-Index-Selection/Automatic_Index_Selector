create index on order_line(ol_i_id,ol_supply_w_id);
create index on order_line(ol_i_id,ol_w_id);
create index on order_line(ol_w_id,ol_delivery_d);
create index on order_line(ol_w_id,ol_o_id,ol_d_id);
create index on orders(o_d_id,o_c_id,o_w_id,o_w_id,o_id,o_d_id);
create index on orders(o_w_id);
create index on stock(s_w_id,s_quantity);
