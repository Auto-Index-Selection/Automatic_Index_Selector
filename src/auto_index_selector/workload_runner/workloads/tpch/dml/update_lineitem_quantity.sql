UPDATE lineitem
SET l_quantity = l_quantity + -4.29
WHERE l_orderkey = 1331646 AND l_linenumber = 2;
