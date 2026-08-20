from pathlib import Path
from pyprojroot import here

tpcc_schema = {
    "warehouse": {
        "w_id": "INT",
        "w_name": "VARCHAR",
        "w_street_1": "VARCHAR",
        "w_street_2": "VARCHAR",
        "w_city": "VARCHAR",
        "w_state": "VARCHAR",
        "w_zip": "VARCHAR",
        "w_tax": "DOUBLE",
        "w_ytd": "DOUBLE"
    },
    "district": {
        "d_id": "INT",
        "d_w_id": "INT",
        "d_name": "VARCHAR",
        "d_street_1": "VARCHAR",
        "d_street_2": "VARCHAR",
        "d_city": "VARCHAR",
        "d_state": "VARCHAR",
        "d_zip": "VARCHAR",
        "d_tax": "DOUBLE",
        "d_ytd": "DOUBLE",
        "d_next_o_id": "INT"
    },
    "customer": {
        "c_id": "INT",
        "c_d_id": "INT",
        "c_w_id": "INT",
        "c_first": "VARCHAR",
        "c_middle": "VARCHAR",
        "c_last": "VARCHAR",
        "c_street_1": "VARCHAR",
        "c_street_2": "VARCHAR",
        "c_city": "VARCHAR",
        "c_state": "VARCHAR",
        "c_zip": "VARCHAR",
        "c_phone": "VARCHAR",
        "c_since": "DATE",
        "c_credit": "VARCHAR",
        "c_credit_lim": "DOUBLE",
        "c_discount": "DOUBLE",
        "c_balance": "DOUBLE",
        "c_ytd_payment": "DOUBLE",
        "c_payment_cnt": "INT",
        "c_delivery_cnt": "INT",
        "c_data": "VARCHAR"
    },
    "history": {
        "h_c_id": "INT",
        "h_c_d_id": "INT",
        "h_c_w_id": "INT",
        "h_d_id": "INT",
        "h_w_id": "INT",
        "h_date": "DATE",
        "h_amount": "DOUBLE",
        "h_data": "VARCHAR"
    },
    "new_order": {
        "no_o_id": "INT",
        "no_d_id": "INT",
        "no_w_id": "INT"
    },
    "orders": {
        "o_id": "INT",
        "o_d_id": "INT",
        "o_w_id": "INT",
        "o_c_id": "INT",
        "o_entry_d": "DATE",
        "o_carrier_id": "INT",
        "o_ol_cnt": "INT",
        "o_all_local": "INT"
    },
    "order_line": {
        "ol_o_id": "INT",
        "ol_d_id": "INT",
        "ol_w_id": "INT",
        "ol_number": "INT",
        "ol_i_id": "INT",
        "ol_supply_w_id": "INT",
        "ol_delivery_d": "DATE",
        "ol_quantity": "INT",
        "ol_amount": "DOUBLE",
        "ol_dist_info": "VARCHAR"
    },
    "item": {
        "i_id": "INT",
        "i_im_id": "INT",
        "i_name": "VARCHAR",
        "i_price": "DOUBLE",
        "i_data": "VARCHAR"
    },
    "stock": {
        "s_i_id": "INT",
        "s_w_id": "INT",
        "s_quantity": "INT",
        "s_dist_01": "VARCHAR",
        "s_dist_02": "VARCHAR",
        "s_dist_03": "VARCHAR",
        "s_dist_04": "VARCHAR",
        "s_dist_05": "VARCHAR",
        "s_dist_06": "VARCHAR",
        "s_dist_07": "VARCHAR",
        "s_dist_08": "VARCHAR",
        "s_dist_09": "VARCHAR",
        "s_dist_10": "VARCHAR",
        "s_ytd": "DOUBLE",
        "s_order_cnt": "INT",
        "s_remote_cnt": "INT",
        "s_data": "VARCHAR"
    }
}

def getWorkload():
    workloadPath = str(here() /"workload"  /  "queries_tpcc")
    # print(workloadPath)
    queries = []
    workloadPath = Path(workloadPath)
    for sql_file in sorted(workloadPath.glob("*.sql")):

        with open(sql_file, "r") as f:
            query = f.read().strip()

        if query:
            queries.append(query)

    return queries, 'tpcc', tpcc_schema