##########
# DEXTER #
##########

DEXTER = True

from itertools import combinations
import subprocess
import regex as re

def generateCandidateIndexes(conn, max_width=2):
    """
        Returns : dict {table : indexes}
    """

    candidates = {}

    with conn.cursor() as cur:

        # fetch all user tables
        cur.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_type = 'BASE TABLE'
            ORDER BY table_name;
        """)

        tables = [row[0] for row in cur.fetchall()]

        for table in tables:

            cur.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = %s
                ORDER BY ordinal_position;
            """, (table,))

            columns = [row[0] for row in cur.fetchall()]

            indexes = []

            for size in range(
                1,
                min(max_width, len(columns)) + 1
            ):
                indexes.extend(
                    list(combinations(columns, size))
                )

            candidates[table] = indexes
    # print(candidates)
    return candidates

def helperGetIndexes(db):
    result = subprocess.run(f"dexter -d {db} --pg-stat-statements", capture_output=True, shell=True)
    # print(result.stdout)
    output = str(result.stdout)
    pattern = r'Index found:\spublic\.([\w]*\s\([\w,\s]*\))'
    results = re.findall(pattern, output)
    # print(results)
    return results

def helperGetIndexes2(indexes):
    data = dict()
    for string in indexes:
        table = ", ".join(map(str, re.findall(r'([\w]*)\s\([\w*\,\s]*\w*\)', string)))
        # print(type(table))
        cols = re.findall(r'[\w]*\s\(([\w*\,\s]*\w*)\)', string)
        # print(type(cols))
        cols = ", ".join(map(str, cols)).split(', ')
        # print(type(cols))
        # print(cols)

        for col in cols:
            if table in data.keys():
                data[table].append(col)
            else:
                data[table] = [col]
            # print('2')
        # if table in data.keys():
        #     data[table].append(cols)
        # else:
        #     data[table] = [cols]
    # print(data)
    result = {
        table: [(col,) for col in dict.fromkeys(cols)]
        for table, cols in data.items()
    }

   
    return result


def getIndexes(conn, max_width=2):
    indexes = helperGetIndexes('tpch')
    indexes += helperGetIndexes('tpch10')
    return helperGetIndexes2(indexes)
    

def runDexter(conn, Workload):
    if DEXTER:
        return getIndexes(conn, 2)
    else:
        return generateCandidateIndexes(conn, 2)