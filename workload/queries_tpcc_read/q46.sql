-- type: read
SELECT i.i_id, i.i_name, COUNT(*) AS times_ordered, SUM(ol.ol_amount) AS revenue FROM item i JOIN order_line ol ON ol.ol_i_id = i.i_id GROUP BY i.i_id, i.i_name ORDER BY revenue DESC LIMIT 100;
