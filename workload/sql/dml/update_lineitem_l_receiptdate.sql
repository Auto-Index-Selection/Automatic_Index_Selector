UPDATE lineitem SET l_receiptdate = l_receiptdate + interval '1 day' WHERE l_orderkey = %s;
