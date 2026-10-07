-- type: read
SELECT ol_i_id, COUNT(*) AS times_ordered, SUM(ol_amount) AS revenue, AVG(ol_quantity) AS avg_qty FROM order_line GROUP BY ol_i_id ORDER BY revenue DESC;
