-- type: read
SELECT ol.ol_i_id, i.i_price, ol.ol_amount FROM order_line ol JOIN item i ON ol.ol_i_id = i.i_id WHERE ol.ol_amount > i.i_price * 8 ORDER BY ol.ol_amount DESC;
