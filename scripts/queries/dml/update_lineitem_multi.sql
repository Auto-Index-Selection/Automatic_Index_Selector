UPDATE lineitem
SET l_quantity       = l_quantity + %s,
    l_discount       = l_discount + %s,
    l_extendedprice  = l_extendedprice + %s
WHERE l_orderkey = %s AND l_linenumber = %s;
