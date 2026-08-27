UPDATE orders
SET o_totalprice    = o_totalprice + %s,
    o_orderstatus   = %s,
    o_clerk         = %s
WHERE o_orderkey = %s;
