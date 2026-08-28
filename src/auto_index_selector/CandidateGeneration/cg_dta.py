from typing import *
import sqlglot as sg
from ordered_set import OrderedSet
from collections import OrderedDict


def _get_alias_map(parsed_query) -> Dict[str, str]:
    """Map alias -> actual_table_name and table_name -> table_name."""
    alias_map = {}
    for table_exp in parsed_query.find_all(sg.exp.Table):
        t_name = table_exp.name
        if not t_name:
            continue
        alias = table_exp.alias
        if alias:
            alias_map[alias] = t_name
        alias_map[t_name] = t_name
    return alias_map


def normalizeColumn(column_node_or_name, schema: Dict, alias_map: Optional[Dict[str, str]] = None) -> str:
    '''
    Input:
        column_node_or_name : sg.exp.Column or string
        schema : Dict
        alias_map : Dict[alias, table_name]
    Output:
        result : table.[column]
    '''
    if isinstance(column_node_or_name, sg.exp.Column):
        col_name = column_node_or_name.name
        tbl_ref = column_node_or_name.table
    else:
        col_name = str(column_node_or_name)
        tbl_ref = None

    # 1. If explicit table/alias is present on the column
    if tbl_ref and alias_map:
        real_tbl = alias_map.get(tbl_ref, tbl_ref)
        if real_tbl in schema and col_name in schema[real_tbl]:
            return f'{real_tbl}.[{col_name}]'

    # 2. If table alias map exists from current query, check tables present in the query
    if alias_map:
        for real_tbl in alias_map.values():
            if real_tbl in schema and col_name in schema[real_tbl]:
                return f'{real_tbl}.[{col_name}]'

    # 3. Fallback: search all schema tables
    for table, attrs in schema.items():
        if col_name in attrs.keys():
            return f'{table}.[{col_name}]'

    return ""


def getJoinCols(q: str, schema: Dict) -> OrderedSet:
    result = OrderedSet()
    parsed_query = sg.parse_one(q)
    alias_map = _get_alias_map(parsed_query)

    for joins in parsed_query.find_all(sg.exp.Join):
        on_clause = joins.args.get("on")
        if on_clause:
            for column in on_clause.find_all(sg.exp.Column):
                normalized_col = normalizeColumn(column, schema, alias_map)
                if normalized_col:
                    result.add(normalized_col)
    return result


def getEqCols(q: str, schema: Dict) -> OrderedSet:
    result = OrderedSet()
    parsed_query = sg.parse_one(q)
    alias_map = _get_alias_map(parsed_query)

    for where in parsed_query.find_all(sg.exp.Where):
        if where:
            for eq in where.find_all(sg.exp.EQ):
                for column in eq.find_all(sg.exp.Column):
                    normalized_col = normalizeColumn(column, schema, alias_map)
                    if normalized_col:
                        result.add(normalized_col)
    return result


def getRangeCols(q: str, schema: Dict) -> OrderedSet:
    result = OrderedSet()
    parsed_query = sg.parse_one(q)
    alias_map = _get_alias_map(parsed_query)
    range_operators = (sg.exp.GT, sg.exp.GTE, sg.exp.LT, sg.exp.LTE)
    for where in parsed_query.find_all(sg.exp.Where):
        if where:
            for range_op in where.find_all(range_operators):
                for column in range_op.find_all(sg.exp.Column):
                    normalized_col = normalizeColumn(column, schema, alias_map)
                    if normalized_col:
                        result.add(normalized_col)
    return result


def getOCols(q: str, schema: Dict) -> OrderedSet:
    result = OrderedSet()
    parsed_query = sg.parse_one(q)
    alias_map = _get_alias_map(parsed_query)
    group_order = (sg.exp.Group, sg.exp.Order)
    for clause in parsed_query.find_all(group_order):
        for column in clause.find_all(sg.exp.Column):
            normalized_col = normalizeColumn(column, schema, alias_map)
            if normalized_col:
                result.add(normalized_col)
    return result


def setToDict(s: OrderedSet) -> Dict:
    '''
    Input
        s : set with elements of form 'table.[column(s)]'
    Output
        result : dict with elements '{table : [column(s)]}'
    '''
    result = dict()
    for element in s:
        table = element.split('.')[0]
        columns_str = element.split('.')[1][1:-1]  # remove [ and ]
        columns = columns_str.split(',')
        if table not in result.keys():
            result[table] = []
        result[table].append(columns)
    return result


# --------------------------------------------------------------------------- #
# DTA-style candidate construction (Section 4, "Candidate Selection")
#
# Everything below replaces the old CIKM'20-style applyRule1..5 functions.
# DTA builds candidates one query at a time (not by pooling predicates
# across the whole workload first), so each helper below takes a single
# query's J/EQ/RANGE/O sets. generateCandidateIndexes() loops over the
# workload and unions the per-query results, matching the paper's
# "Candidate Selection is performed on one query at a time" (Section 4).
# --------------------------------------------------------------------------- #

Selectivity = Dict[str, float]  # 'table.[col]' -> selectivity in (0, 1], smaller = more selective


def _stripBrackets(label: str) -> Tuple[str, str]:
    '''table.[col] -> (table, col)'''
    table, bracketed = label.split('.', 1)
    return table, bracketed[1:-1]


def _groupByTable(cols: OrderedSet) -> "OrderedDict[str, List[str]]":
    grouped: "OrderedDict[str, List[str]]" = OrderedDict()
    for c in cols:
        table, col = _stripBrackets(c)
        grouped.setdefault(table, []).append(col)
    return grouped


def _orderBySelectivity(labels: List[str], selectivity: Optional[Selectivity]) -> List[str]:
    '''
    Order column labels ('table.[col]') by estimated selectivity, most
    selective (smallest fraction) first -- mirrors DTA ordering selection
    columns "by estimated selectivity of the predicate on that column"
    (Section 4). Falls back to original (query) order when no selectivity
    estimates are supplied.
    '''
    if not selectivity:
        return list(labels)
    return sorted(labels, key=lambda c: selectivity.get(c, 0.5))


def _makeIndexLabel(table: str, columns: List[str]) -> str:
    seen: OrderedSet = OrderedSet()
    ordered_cols = []
    for c in columns:
        if c not in seen:
            seen.add(c)
            ordered_cols.append(c)
    return f'{table}.[' + ','.join(ordered_cols) + ']'


def buildCompositeSelectionCandidates(EQ: OrderedSet, RANGE: OrderedSet,
                                       selectivity: Optional[Selectivity] = None) -> OrderedSet:
    '''
    Composite selection index per table: equality columns ordered by
    selectivity, leading, followed by at most one trailing range column
    (a range predicate can only usefully occupy the last key position).
    '''
    result: OrderedSet = OrderedSet()
    eq_by_table = _groupByTable(EQ)
    range_by_table = _groupByTable(RANGE)

    tables = OrderedSet(eq_by_table.keys()) | OrderedSet(range_by_table.keys())
    for table in tables:
        eq_cols = eq_by_table.get(table, [])
        range_cols = range_by_table.get(table, [])

        eq_labels = [f'{table}.[{c}]' for c in eq_cols]
        ordered_eq = [_stripBrackets(l)[1] for l in _orderBySelectivity(eq_labels, selectivity)]

        if len(ordered_eq) > 1:
            result.add(_makeIndexLabel(table, ordered_eq))
        if ordered_eq and range_cols:
            result.add(_makeIndexLabel(table, ordered_eq + range_cols[:1]))

    return result


def buildJoinLeadingCandidates(J: OrderedSet, EQ: OrderedSet, RANGE: OrderedSet,
                                selectivity: Optional[Selectivity] = None) -> OrderedSet:
    '''
    DTA: "indexes on join columns from two or more tables... with R.A as
    the leading column and indexes on S with S.B as the leading column"
    (Section 4). Each join column leads; that table's selectivity-ordered
    equality columns and (at most one) trailing range column follow.
    '''
    result: OrderedSet = OrderedSet()
    eq_by_table = _groupByTable(EQ)
    range_by_table = _groupByTable(RANGE)

    for j in J:
        table, join_col = _stripBrackets(j)

        eq_labels = [f'{table}.[{c}]' for c in eq_by_table.get(table, []) if c != join_col]
        ordered_eq = [_stripBrackets(l)[1] for l in _orderBySelectivity(eq_labels, selectivity)]
        trailing_range = [c for c in range_by_table.get(table, []) if c != join_col][:1]

        result.add(_makeIndexLabel(table, [join_col]))
        if ordered_eq or trailing_range:
            result.add(_makeIndexLabel(table, [join_col] + ordered_eq + trailing_range))

    return result


def buildGroupOrderCandidate(O: OrderedSet) -> OrderedSet:
    '''
    Composite GROUP BY / ORDER BY index, columns kept in query-clause
    order ("For order by columns, DTA uses the ordering specified in the
    query", Section 4). Only produced when every column belongs to the
    same table.
    '''
    result: OrderedSet = OrderedSet()
    if not O:
        return result

    tables = OrderedSet(_stripBrackets(c)[0] for c in O)
    if len(tables) != 1:
        return result

    table = next(iter(tables))
    cols = [_stripBrackets(c)[1] for c in O]
    if len(cols) > 1:
        result.add(_makeIndexLabel(table, cols))
    return result


def generateCandidateIndexes(W: List, schema: Dict, selectivity: Optional[Selectivity] = None) -> Dict:
    '''
    Input :
        W -> workload as a List of SQL strings
        schema -> dict
        selectivity -> optional {'table.[col]': selectivity_in_(0,1]} used
            to order equality columns within composite keys (most selective
            leading). Omit to fall back to query order.
    Return :
        candidateIndexes -> Candidate Indexes as dict {table: [candidate_columns, ...]}

    DTA-style candidate generation (Section 4, "Candidate Selection"):
    candidate selection is performed one query at a time, then unioned
    across the workload. For each query this produces:
        - single-column indexes on every selection (equality/range) and
          join column
        - a composite selection index (selectivity-ordered equality
          columns + one trailing range column)
        - a composite join-leading index per join column
        - a composite GROUP BY / ORDER BY index (query-clause order)
    '''
    print("DTA")
    cis: OrderedSet = OrderedSet()

    for query in W:
        J = getJoinCols(query, schema)
        EQ = getEqCols(query, schema)
        RANGE = getRangeCols(query, schema)
        O = getOCols(query, schema)

        # single-column candidates (getJoinCols/getEqCols/getRangeCols
        # already normalize to single-column 'table.[col]' labels)
        cis |= J
        cis |= EQ
        cis |= RANGE

        # composite candidates, built from this query's own predicates only
        cis |= buildCompositeSelectionCandidates(EQ, RANGE, selectivity)
        cis |= buildJoinLeadingCandidates(J, EQ, RANGE, selectivity)
        cis |= buildGroupOrderCandidate(O)

    return setToDict(cis)


