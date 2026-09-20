-- Order-Status / Payment step: customer by full primary key.
SELECT c_id, c_first, c_last, c_balance, c_credit
FROM customer
WHERE c_w_id = 5 AND c_d_id = 3 AND c_id = 1500;
