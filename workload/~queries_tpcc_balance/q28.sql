-- type: write
UPDATE item SET i_price = ROUND((i_price * 1.01)::numeric, 2);
