-- Payment by name: the classic secondary-index case (c_w_id, c_d_id, c_last).
SELECT c_id, c_first, c_last, c_balance
FROM customer
WHERE c_w_id = 5 AND c_d_id = 3 AND c_last = 'BARBARBAR'
ORDER BY c_first;
