-- type: read
SELECT c.c_w_id, c.c_d_id, COUNT(*) AS order_cnt, SUM(ol.ol_amount) AS total_spent FROM customer c JOIN orders o ON o.o_c_id=c.c_id AND o.o_w_id=c.c_w_id AND o.o_d_id=c.c_d_id JOIN order_line ol ON ol.ol_o_id=o.o_id AND ol.ol_d_id=o.o_d_id AND ol.ol_w_id=o.o_w_id GROUP BY c.c_w_id, c.c_d_id ORDER BY total_spent DESC;
