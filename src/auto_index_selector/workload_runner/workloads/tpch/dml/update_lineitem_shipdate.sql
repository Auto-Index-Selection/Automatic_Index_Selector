UPDATE lineitem
SET l_shipdate = l_shipdate + (%s * INTERVAL '1 day')
WHERE l_orderkey = %s AND l_linenumber = %s;
