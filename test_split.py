def split_sql_statements(sql: str) -> list[str]:
    statements = []
    current_statement = []
    in_single_quote = False
    in_double_quote = False
    
    for i, char in enumerate(sql):
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
        elif char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
        
        if char == ';' and not in_single_quote and not in_double_quote:
            statements.append("".join(current_statement).strip())
            current_statement = []
        else:
            current_statement.append(char)
            
    if current_statement:
        stmt = "".join(current_statement).strip()
        if stmt:
            statements.append(stmt)
            
    return statements

sql = """-- type: write
INSERT INTO orders (o_id, o_d_id, o_w_id, o_c_id, o_entry_d, o_carrier_id, o_ol_cnt, o_all_local) VALUES (3001, 3, 2, 2571, now(), NULL, 8, 1); INSERT INTO order_line (ol_o_id, ol_d_id, ol_w_id, ol_number, ol_i_id, ol_supply_w_id, ol_delivery_d, ol_quantity, ol_amount, ol_dist_info) VALUES (3001, 3, 2, 1, 55334, 2, now(), 5, 596.81, 'dist_info_str; with semi'); """

for s in split_sql_statements(sql):
    print("STMT:", s)
