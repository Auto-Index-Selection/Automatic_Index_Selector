UPDATE lineitem
SET l_shipdate = l_shipdate + (3 * INTERVAL '1 day')
WHERE l_orderkey = 513406 AND l_linenumber = 2;
