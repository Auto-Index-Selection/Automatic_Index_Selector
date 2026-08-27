UPDATE lineitem SET l_commitdate = l_commitdate + interval '1 day' WHERE l_orderkey = %s;
