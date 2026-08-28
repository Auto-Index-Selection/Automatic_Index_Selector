CREATE INDEX IF NOT EXISTS ais_test_lineitem_l_orderkey ON lineitem (l_orderkey);
CREATE INDEX IF NOT EXISTS ais_test_nation_n_nationkey ON nation (n_nationkey);
CREATE INDEX IF NOT EXISTS ais_test_orders_o_custkey ON orders (o_custkey);
CREATE INDEX IF NOT EXISTS ais_test_orders_o_orderkey ON orders (o_orderkey);
CREATE INDEX IF NOT EXISTS ais_test_partsupp_ps_suppkey ON partsupp (ps_suppkey);
