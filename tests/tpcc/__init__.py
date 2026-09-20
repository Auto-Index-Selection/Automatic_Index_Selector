"""
tests/tpcc
----------
Real TPC-C benchmark suite for the Automatic Index Selector.

Runs against `tpcc_standard_db` (9 tables, ~830 MB, 10 warehouses). Unlike
tests/pgbench — which is TPC-B and observes a separate window per strategy —
this suite executes the TPC-C workload *inline* during a single shared
observation window, then evaluates every algorithm combination against that
identical observation.
"""
