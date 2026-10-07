-- type: read
SELECT c.c_id, c.c_w_id, c.c_d_id, c.c_balance FROM customer c WHERE c.c_balance = (SELECT MIN(c2.c_balance) FROM customer c2 WHERE c2.c_w_id = c.c_w_id);
