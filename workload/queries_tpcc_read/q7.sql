-- type: read
SELECT o.o_id, o.o_entry_d, c.c_first, c.c_last FROM orders o JOIN customer c ON o.o_c_id=c.c_id AND o.o_w_id=c.c_w_id AND o.o_d_id=c.c_d_id WHERE o.o_w_id=1 AND o.o_d_id=3;
