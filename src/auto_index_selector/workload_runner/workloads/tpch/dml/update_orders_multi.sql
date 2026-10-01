UPDATE orders
SET o_totalprice    = o_totalprice + 97.9,
    o_orderstatus   = 'P',
    o_clerk         = 'Clerk#000000705'
WHERE o_orderkey = 1168009;
