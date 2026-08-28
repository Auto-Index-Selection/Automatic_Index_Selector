CREATE INDEX IF NOT EXISTS ais_test_lineitem_l_orderkey ON lineitem (l_orderkey);
CREATE INDEX IF NOT EXISTS ais_test_lineitem_l_partkey ON lineitem (l_partkey);
CREATE INDEX IF NOT EXISTS ais_test_lineitem_l_suppkey ON lineitem (l_suppkey);
CREATE INDEX IF NOT EXISTS ais_test_part_p_partkey ON part (p_partkey);
CREATE INDEX IF NOT EXISTS ais_test_partsupp_ps_suppkey ON partsupp (ps_suppkey);
