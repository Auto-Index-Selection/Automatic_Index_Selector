-- Stock-Level: order_line joined to stock over a 20-order window.
SELECT COUNT(DISTINCT s.s_i_id) AS low_stock
FROM order_line ol
JOIN stock s ON s.s_i_id = ol.ol_i_id AND s.s_w_id = ol.ol_w_id
WHERE ol.ol_w_id = 5 AND ol.ol_d_id = 3
  AND ol.ol_o_id BETWEEN 2980 AND 3000
  AND s.s_quantity < 15;
