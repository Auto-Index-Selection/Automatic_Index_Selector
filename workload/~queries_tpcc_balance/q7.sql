-- type: write
UPDATE stock SET s_quantity = s_quantity - 3, s_ytd = s_ytd + 3 WHERE s_w_id=10 AND s_i_id=83228;
