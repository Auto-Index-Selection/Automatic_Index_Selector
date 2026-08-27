from typing import *
import sqlglot as sg
from ordered_set import OrderedSet
from collections import OrderedDict
def normalizeColumn(column: str, schema: Dict) -> str:
    '''
    Input 
        column : string
        schema : Dict
    Output 
        result : table.column
    '''
    result = ""
    for table,attrs in schema.items():
        if column in attrs.keys():
            result = f'{table}.[{column}]'
    # print(result)
    return result

def getJoinCols(q: str, schema: Dict) ->OrderedSet:
    result = OrderedSet()
    parsed_query = sg.parse_one(q)

    # extract joins
    for joins in parsed_query.find_all(sg.exp.Join):
        on_clause = joins.args.get("on")
        if on_clause:
            for column in on_clause.find_all(sg.exp.Column):
                normalized_col = normalizeColumn(column.name, schema)
                if normalized_col == '':
                    continue
                result.add(normalized_col)
    return result

def getEqCols(q: str, schema: Dict) ->OrderedSet:
    result = OrderedSet()
    parsed_query = sg.parse_one(q)
    # extract equality column in where (no having, join)

    for where in parsed_query.find_all(sg.exp.Where):
        if where:
            for eq in where.find_all(sg.exp.EQ):
                for column in eq.find_all(sg.exp.Column):
                    normalized_col = normalizeColumn(column.name, schema)
                    if normalized_col == '':
                        continue
                    result.add(normalized_col)
    return result

def getRangeCols(q: str, schema: Dict) ->OrderedSet:
    result = OrderedSet()
    parsed_query = sg.parse_one(q)
    range_operators = (sg.exp.GT, sg.exp.GTE, sg.exp.LT, sg.exp.LTE)
    for where in parsed_query.find_all(sg.exp.Where):
        if where:
            for range in where.find_all(range_operators):
                for column in range.find_all(sg.exp.Column):
                    normalized_col = normalizeColumn(column.name, schema)
                    if normalized_col == '':
                        continue
                    result.add(normalized_col)
    return result

def getOCols(q: str, schema: Dict) ->OrderedSet:
    result = OrderedSet()
    parsed_query = sg.parse_one(q)
    group_order = (sg.exp.Group, sg.exp.Order)
    for range in parsed_query.find_all(group_order):
        for column in range.find_all(sg.exp.Column):
               normalized_col = normalizeColumn(column.name, schema)
               if normalized_col == '':
                   continue
               result.add(normalized_col)
    return result

def _stripBrackets(label: str) -> Tuple[str, str]:
    '''table.[col] -> (table, col)'''
    table, bracketed = label.split('.', 1)
    return table, bracketed[1:-1]

def applyRule1(J, EQ, RANGE):
    result = OrderedSet()
    result |= (J | EQ | RANGE)
    return result

def applyRule2(O:OrderedSet) ->OrderedSet:
    result = OrderedSet()

    tables = OrderedSet()
    for ci in O:
        tables.add(ci.split('.')[0])
    if len(tables) != 1:
        return result
    table = next(iter(O)).split('.')[0]
    # print(table)
    columns = "["
    for ci in O:
        # print(ci)
        col = ci.split('.')[1]\
            [1:-1]
        columns += col + ','
    # print(columns)
    columns = columns[:-1] + ']'
    # print(columns)
    result.add(f'{table}.{columns}')
    # print(result)
    return result

def applyRule3(q, schema):
    result = OrderedSet()
    parsed_query = sg.parse_one(q)

    temp = OrderedDict()
    # extract joins
    for joins in parsed_query.find_all(sg.exp.Join):
        # print(joins)
        temp_dict = OrderedDict()
        on_clause = joins.args.get("on")
        # print(on_clause)
        if on_clause:
            for column in on_clause.find_all(sg.exp.Column):
                # print(f'\tname : {column.name}')
                normalized_col = normalizeColumn(column.name, schema)
                if normalized_col == '':
                    continue
                table = normalized_col.split('.')[0]
                # column = normalized_col.split('.')[1]
                if table not  in temp_dict.keys():
                    temp_dict[table] = column.name
                else:
                    temp_dict[table] += (',' + column.name)
                # print(temp_dict)

            for key, item in temp_dict.items():
                if key not in temp.keys():
                    temp[key] = OrderedSet()
                temp[key].add(item)

    # print(temp) 
    for table, idxs in temp.items():
        columns = f'{table}.['
        for idx in idxs:
            for col in idx.split(','):
                # print(col)
                columns += col + ','
        columns = columns[:-1] + ']'
            # print(columns)
        result.add(columns)
    # print("")
    # print(result)
    return result


def applyRule4(J: OrderedSet, EQ: OrderedSet, RANGE: OrderedSet) -> OrderedSet:
    result = OrderedSet()
 
    j: Dict[str, List[List[str]]] = OrderedDict()
    eq: Dict[str, List[str]] = OrderedDict()
    r: Dict[str, List[str]] = OrderedDict()
 
    for idx in J:
        table = idx.split('.')[0]
        columns = idx.split('.')[1][1:-1]
        j.setdefault(table, []).append(columns.split(','))
 
    for idx in EQ:
        table = idx.split('.')[0]
        columns = idx.split('.')[1][1:-1]
        eq.setdefault(table, []).append(columns)
 
    for idx in RANGE:
        table = idx.split('.')[0]
        columns = idx.split('.')[1][1:-1]
        r.setdefault(table, []).append(columns)
 
    all_tables = set(j.keys()) | set(eq.keys()) | set(r.keys())
 
    def make(table: str, cols: List[str]) -> str:
        return f'{table}.[' + ','.join(cols) + ']'
 
    for table in all_tables:
        j_list = j.get(table, [])
        eq_list = eq.get(table, [])
        r_list = r.get(table, [])
 
        # j + EQ + r
        for j_cols in j_list:
            for eq_col in eq_list:
                for r_col in r_list:
                    cols = list(j_cols)
                    if eq_col not in cols:
                        cols.append(eq_col)
                    if r_col not in cols:
                        cols.append(r_col)
                    if len(cols) > 1:
                        result.add(make(table, cols))
 
        # EQ + r
        for eq_col in eq_list:
            for r_col in r_list:
                if eq_col == r_col:
                    continue
                result.add(make(table, [eq_col, r_col]))
 
        # j + r
        for j_cols in j_list:
            for r_col in r_list:
                cols = list(j_cols)
                if r_col not in cols:
                    cols.append(r_col)
                if len(cols) > 1:
                    result.add(make(table, cols))
 
        # j + EQ
        for j_cols in j_list:
            for eq_col in eq_list:
                cols = list(j_cols)
                if eq_col not in cols:
                    cols.append(eq_col)
                if len(cols) > 1:
                    result.add(make(table, cols))
 
    return result
 
 
def applyRule5(cis: OrderedSet, USED: OrderedSet, max_attrs: int = 3) -> OrderedSet:
    result = OrderedSet()
 
    used_by_table: Dict[str, OrderedSet] = OrderedDict()
    for idx in USED:
        table = idx.split('.')[0]
        col = idx.split('.')[1][1:-1]
        used_by_table.setdefault(table, OrderedSet()).add(col)
 
    for ci in cis:
        table = ci.split('.')[0]
        columns_str = ci.split('.')[1][1:-1]
        columns = columns_str.split(',')
 
        if len(columns) >= max_attrs:
            continue
 
        remaining = used_by_table.get(table, OrderedSet())
        for attr in remaining:
            if attr in columns:
                continue
            new_columns = columns + [attr]
            if len(new_columns) > max_attrs:
                continue
            result.add(f'{table}.[' + ','.join(new_columns) + ']')
 
    return result

def setToDict(s:OrderedSet) -> Dict :
    '''
    Input
        s : set with elements of form 'table.[column(s)]'
    Output
        result : dict with elements '{table : [column(s)}'
    '''
    result = dict()
    for element in s:
        table = element.split('.')[0]
        columns_str = element.split('.')[1]\
            [1:-1] # remove [ and ]
        columns = columns_str.split(',')
        if table not in result.keys():
            result[table] = []
        result[table].append(columns)
    return result

def generateCandidateIndexes(conn, W: List, schema: Dict) -> Dict:
    '''
    Input : 
        W -> workload as a List
        schema -> dict
    Return :
        candidateIndexes -> Candidate Indexes as dict {table: candidates}
    '''
    J:OrderedSet = OrderedSet()
    EQ:OrderedSet = OrderedSet()
    RANGE:OrderedSet = OrderedSet()
    O:OrderedSet = OrderedSet()
    USED:OrderedSet = OrderedSet()

    cis = OrderedSet()

    for query in W:
        join_cols = getJoinCols(query, schema)
        J |= join_cols    
        # print(join_cols)

        eq_cols = getEqCols(query, schema)
        EQ |= eq_cols 

        range_cols = getRangeCols(query, schema)
        RANGE |= range_cols 

        o_cols = getOCols(query, schema)
        O |= o_cols 

        USED = J | EQ | RANGE | O
    
        # rule 1
        cis |= applyRule1(J, EQ, RANGE) #done

        # rule 2 
        cis |= applyRule2(O) #done

        # rule 3
        rule3_attr = applyRule3(query, schema) #done
        cis |= rule3_attr
        # rule 4
        cis |= applyRule4(J 
                        #    rule3_attr
                          , EQ, RANGE) 

        # rule 5
        # cis |= applyRule5(cis, USED)
    # print(cis)
    result = setToDict(cis)

    return result
