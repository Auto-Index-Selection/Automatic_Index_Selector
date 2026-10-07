-- type: read
SELECT COUNT(DISTINCT s.s_i_id) FROM stock s JOIN order_line ol ON s.s_i_id=ol.ol_i_id AND s.s_w_id=ol.ol_w_id WHERE s.s_w_id=6 AND s.s_quantity < 50;
