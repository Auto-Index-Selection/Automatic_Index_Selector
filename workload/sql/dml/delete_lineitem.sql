DELETE FROM lineitem
WHERE l_orderkey = %s AND l_linenumber = %s;
