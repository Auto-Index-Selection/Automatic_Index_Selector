UPDATE orders SET o_orderdate = o_orderdate + interval '1 day' WHERE o_orderkey = %s;
