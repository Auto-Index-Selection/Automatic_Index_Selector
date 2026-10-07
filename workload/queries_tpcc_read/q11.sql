-- type: read
SELECT ol_i_id, SUM(ol_amount) AS revenue FROM order_line WHERE ol_w_id=2 GROUP BY ol_i_id ORDER BY revenue DESC LIMIT 10;
