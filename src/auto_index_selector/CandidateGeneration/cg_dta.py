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
        result : table.[column]
    '''
    result = ""
    for table, attrs in schema.items():
        if column in attrs.keys():
            result = f'{table}.[{column}]'
    return result


def getJoinCols(q: str, schema: Dict) -> OrderedSet:
    result = OrderedSet()
    parsed_query = sg.parse_one(q)

    for joins in parsed_query.find_all(sg.exp.Join):
        on_clause = joins.args.get("on")
        if on_clause:
            for column in on_clause.find_all(sg.exp.Column):
                normalized_col = normalizeColumn(column.name, schema)
                if normalized_col == '':
                    continue
                result.add(normalized_col)
    return result


def getEqCols(q: str, schema: Dict) -> OrderedSet:
    result = OrderedSet()
    parsed_query = sg.parse_one(q)
    # extract equality columns in where (no having, join)

    for where in parsed_query.find_all(sg.exp.Where):
        if where:
            for eq in where.find_all(sg.exp.EQ):
                for column in eq.find_all(sg.exp.Column):
                    normalized_col = normalizeColumn(column.name, schema)
                    if normalized_col == '':
                        continue
                    result.add(normalized_col)
    return result


def getRangeCols(q: str, schema: Dict) -> OrderedSet:
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


def getOCols(q: str, schema: Dict) -> OrderedSet:
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


def generateCandidateIndexes(conn, W: List, schema: Dict, selectivity: Optional[Selectivity] = None) -> Dict:
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


