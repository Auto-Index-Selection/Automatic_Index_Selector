-- TC9: Delivery Transaction — Customer Balance Lookup for Delivery
SELECT c_id, c_balance, c_delivery_cnt 
FROM customer 
WHERE c_w_id = 1 AND c_d_id = 4 AND c_id = 300;
