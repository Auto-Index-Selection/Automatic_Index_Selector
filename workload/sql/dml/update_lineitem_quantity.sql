UPDATE lineitem
SET l_quantity = l_quantity + %s
WHERE l_orderkey = %s AND l_linenumber = %s;
