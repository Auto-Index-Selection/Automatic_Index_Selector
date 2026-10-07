-- type: write
UPDATE item SET i_price = ROUND((i_price * 0.98)::numeric, 2);
