UPDATE lineitem
SET l_quantity       = l_quantity + -1.3,
    l_discount       = l_discount + -0.029,
    l_extendedprice  = l_extendedprice + -46.6
WHERE l_orderkey = 1433504 AND l_linenumber = 6;
