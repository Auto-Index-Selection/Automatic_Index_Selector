-- type: read
SELECT s.s_w_id, s.s_i_id, s.s_quantity, COUNT(ol.ol_o_id) AS demand FROM stock s JOIN order_line ol ON ol.ol_i_id = s.s_i_id AND ol.ol_supply_w_id = s.s_w_id GROUP BY s.s_w_id, s.s_i_id, s.s_quantity HAVING COUNT(ol.ol_o_id) > 5 ORDER BY demand DESC;
