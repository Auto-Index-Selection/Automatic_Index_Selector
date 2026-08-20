from itertools import product
import sqlglot


def getTablesUsed(query):
    """
    Extract tables from a SQL query.
    """

    parsed = sqlglot.parse_one(query)

    tables = []

    for table in parsed.find_all(sqlglot.exp.Table):
        tables.append(table.name)

    return list(dict.fromkeys(tables))

def sortedConfig(config):
    return sorted(config)

def generateConfigurations(candidate_indexes, workload):
    """
    Parameters
    ----------
    candidate_indexes : dict

    workload : list[str]

    Returns
    -------
    list of configurations cross product
    """
    configurations = list()
    tables_seen = list()
    i = 0
    for query in workload:
        tables = sorted(getTablesUsed(query))
        if tables in tables_seen:
            continue
        tables_seen.append(tables)
        idxs = list()
        for table in tables:

            if table not in candidate_indexes.keys():
                continue
            idxs.append(candidate_indexes[table])
        
        # print(idxs)
        for config in product(*idxs):
            s_config = sortedConfig(config)
            if s_config not in configurations:
                configurations.append(s_config)
                i+=1
        # print(config)
            
            if i%1000 == 0:
                print(i, 'configs generated')


    return (configurations)

def generateConfigurations1(candidate_indexes, workload):

    ## getTablesUsed(query)
    pass

def numericConfig(configurations, indexSet):
    """
    Return : dict (ConfigNum : list(index))
    """
    configSet = dict()
    k = 1
    for config in configurations:
        configSet[k] = list()
        for index in config:
            temp = '['+','.join(index)+']'
            pos = indexSet[temp][0]
            configSet[k].append(pos)
        k += 1

    return configSet
