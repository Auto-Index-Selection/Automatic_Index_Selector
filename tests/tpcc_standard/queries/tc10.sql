-- TC10: Stock-Level Transaction — Low-Stock Items Analytical Join
SELECT count(DISTINCT s.s_i_id) 
FROM district d 
JOIN order_line ol ON d.d_w_id = ol.ol_w_id AND d.d_id = ol.ol_d_id 
JOIN stock s ON ol.ol_i_id = s.s_i_id AND ol.ol_w_id = s.s_w_id 
WHERE d.d_w_id = 1 AND d.d_id = 1 AND ol.ol_o_id >= (d.d_next_o_id - 20) AND ol.ol_o_id < d.d_next_o_id AND s.s_quantity < 15;
