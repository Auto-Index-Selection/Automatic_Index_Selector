-- type: write
UPDATE stock SET s_quantity = s_quantity - 5, s_ytd = s_ytd + 5 WHERE s_w_id=1 AND s_i_id=97197;
