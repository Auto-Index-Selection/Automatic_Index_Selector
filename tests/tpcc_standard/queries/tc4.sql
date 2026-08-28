-- TC4: Payment Transaction — Customer Lookup by Last Name
SELECT c_id, c_first, c_middle, c_last, c_balance 
FROM customer 
WHERE c_w_id = 1 AND c_d_id = 3 AND c_last = 'BARBARBAR' 
ORDER BY c_first;
