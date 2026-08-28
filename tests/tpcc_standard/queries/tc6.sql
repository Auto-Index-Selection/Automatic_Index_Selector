-- TC6: Order-Status Transaction — Customer Latest Order Header by Last Name
SELECT c.c_id, c.c_first, c.c_balance, o.o_id, o.o_entry_d 
FROM customer c 
JOIN orders o ON c.c_w_id = o.o_w_id AND c.c_d_id = o.o_d_id AND c.c_id = o.o_c_id 
WHERE c.c_w_id = 1 AND c.c_d_id = 2 AND c.c_last = 'ABLEBAR' 
ORDER BY o.o_id DESC 
LIMIT 1;
