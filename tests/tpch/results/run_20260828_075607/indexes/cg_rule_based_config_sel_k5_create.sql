CREATE INDEX IF NOT EXISTS ais_test_lineitem_l_orderkey_l_quantity ON lineitem (l_orderkey, l_quantity);
CREATE INDEX IF NOT EXISTS ais_test_lineitem_l_partkey_l_quantity ON lineitem (l_partkey, l_quantity);
CREATE INDEX IF NOT EXISTS ais_test_lineitem_l_suppkey_l_partkey ON lineitem (l_suppkey, l_partkey);
CREATE INDEX IF NOT EXISTS ais_test_part_p_partkey ON part (p_partkey);
CREATE INDEX IF NOT EXISTS ais_test_partsupp_ps_partkey_ps_suppkey ON partsupp (ps_partkey, ps_suppkey);
