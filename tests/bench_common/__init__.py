"""
tests/bench_common
------------------
Schema-agnostic benchmarking harness shared by the suites under tests/.

  runner.py   measurement primitives (timing, index build/sweep, prewarm)
  harness.py  experiment scaffolding (baseline, build/measure/drop, persistence)
  plot.py     comparison charts

Each suite (tests/pgbench, tests/tpcc) supplies only what is schema-specific:
its queries, its traffic generator, and its experiment driver.
"""
