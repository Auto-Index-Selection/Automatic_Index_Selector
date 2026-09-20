UPDATE customer
SET c_acctbal     = c_acctbal + %s,
    c_phone       = %s,
    c_mktsegment  = %s
WHERE c_custkey = %s;
