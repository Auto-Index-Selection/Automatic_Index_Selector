
dbset db pg
dbset bm TPROC-C
diset connection pg_host localhost
diset connection pg_port 5432
diset tpcc pg_superuser postgres
diset tpcc pg_superuserpass 123
diset tpcc pg_defaultdbase postgres
diset tpcc pg_dbase tpcc_standard_db
diset tpcc pg_user postgres
diset tpcc pg_pass 123
diset tpcc pg_count_ware 10
diset tpcc pg_num_vu 10
diset tpcc pg_storedprocs true
buildschema
exit
