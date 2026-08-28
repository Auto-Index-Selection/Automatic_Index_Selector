-- TC3: Payment Transaction — Customer Point Lookup by ID
SELECT c_first, c_middle, c_last, c_street_1, c_city, c_state, c_zip, c_phone, c_credit, c_credit_lim, c_discount, c_balance 
FROM customer 
WHERE c_w_id = 1 AND c_d_id = 3 AND c_id = 1500;
