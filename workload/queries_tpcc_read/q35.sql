-- type: read
SELECT o.o_w_id, o.o_d_id, COUNT(*) AS orders, SUM(ol.ol_amount) AS revenue, AVG(o.o_ol_cnt) AS avg_lines FROM orders o JOIN order_line ol ON ol.ol_o_id=o.o_id AND ol.ol_d_id=o.o_d_id AND ol.ol_w_id=o.o_w_id GROUP BY o.o_w_id, o.o_d_id ORDER BY revenue DESC;
