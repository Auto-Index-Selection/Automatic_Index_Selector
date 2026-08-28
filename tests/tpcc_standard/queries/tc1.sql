-- TC1: New-Order Transaction — Item Catalog & Warehouse Stock Lookup
SELECT i.i_price, i.i_name, i.i_data, s.s_quantity, s.s_data 
FROM item i 
JOIN stock s ON i.i_id = s.s_i_id 
WHERE i.i_id = 45000 AND s.s_w_id = 1;
