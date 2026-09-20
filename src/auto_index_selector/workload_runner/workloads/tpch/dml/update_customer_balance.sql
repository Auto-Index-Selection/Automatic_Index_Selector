UPDATE customer
SET c_acctbal = c_acctbal + %s
WHERE c_custkey = %s;
