-- type: write
UPDATE customer SET c_balance = c_balance - 375.04, c_ytd_payment = c_ytd_payment + 375.04 WHERE c_w_id=5 AND c_d_id=3 AND c_id=1011;
