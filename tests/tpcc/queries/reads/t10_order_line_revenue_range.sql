-- Analytical: revenue over an order-id range, joined to orders.
SELECT o.o_d_id, SUM(ol.ol_amount) AS revenue, COUNT(*) AS lines
FROM order_line ol
JOIN orders o
  ON o.o_w_id = ol.ol_w_id AND o.o_d_id = ol.ol_d_id AND o.o_id = ol.ol_o_id
WHERE ol.ol_w_id = 5 AND ol.ol_o_id BETWEEN 2500 AND 2700
GROUP BY o.o_d_id
ORDER BY revenue DESC;
