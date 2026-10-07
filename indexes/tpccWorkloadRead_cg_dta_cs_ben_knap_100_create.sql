create index on item(i_id,i_name);
create index on item(i_id,i_price);
create index on item(i_price);
create index on orders(o_w_id);
create index on stock(s_quantity);
create index on stock(s_w_id,s_i_id,s_quantity);
create index on stock(s_w_id,s_quantity);
