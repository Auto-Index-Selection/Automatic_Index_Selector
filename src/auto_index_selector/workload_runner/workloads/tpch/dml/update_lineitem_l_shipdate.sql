UPDATE lineitem SET l_shipdate = l_shipdate + interval '1 day' WHERE l_orderkey = %s;
