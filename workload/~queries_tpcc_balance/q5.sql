-- type: write
UPDATE stock SET s_quantity = s_quantity - 10, s_ytd = s_ytd + 10 WHERE s_w_id=9 AND s_i_id=38428;
