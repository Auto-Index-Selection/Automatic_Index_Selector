UPDATE orders
SET o_totalprice = o_totalprice + %s
WHERE o_orderkey = %s;
