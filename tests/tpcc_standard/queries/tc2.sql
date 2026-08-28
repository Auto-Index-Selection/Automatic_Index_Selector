-- TC2: New-Order Transaction — Customer Discount & Warehouse/District Tax
SELECT c.c_discount, c.c_last, c.c_credit, w.w_tax, d.d_tax 
FROM customer c 
JOIN warehouse w ON c.c_w_id = w.w_id 
JOIN district d ON c.c_w_id = d.d_w_id AND c.c_d_id = d.d_id 
WHERE c.c_w_id = 1 AND c.c_d_id = 5 AND c.c_id = 1200;
