import gurobipy as g
# import random

debug = False

####### TO DO ############
## Variable received ##
# numCandidateIndexes = 7
# numQueries = 10
# numConfigurations = 3
# numUpdates = 10
# configs = {
#     1: [1, 2, 4],
#     2: [3, 5],
#     3: [2, 6, 7]
# }
# indexSize = {
#     1: 1,
#     2: 2,
#     3: 3,
#     4: 4,
#     5: 5,
#     6: 6,
#     7: 7
# }
# storageBudget = 10
# random.seed(1)
# cost_initial = { i: random.randint(1, 4) for i in range(1, numQueries+numUpdates+1)}
# cost_final = { (i, k): random.randint(2, 5) for i in range(1, numQueries+numUpdates+1) for k in range(1, numConfigurations+1)}

# cost_update = { (l, j): 0 for l in range(1, numUpdates+1) for j in range(1, numCandidateIndexes+1)}




def solveILP(numQueries, numUpdates, numCandidateIndexes, numConfigurations, configs, indexSize, storageBudget, benefit, f):
    
    ##############
    # Init model #
    ##############
    model = g.Model()


    ######################
    #  Decision Variable #
    ######################
    # y_j : whether index is implemented or not

    y = model.addVars(
        range(1, numCandidateIndexes+1),
        vtype = g.GRB.BINARY, 
        name = f'y'
    )
        

    # x_ik : whether query 'i' uses configuration 'k'
    x = model.addVars(
        range(1,numQueries+numUpdates+1),
        range(1, numConfigurations+1),
        vtype = g.GRB.BINARY, 
        name = f'x'
    )
    # x = {}
    # for i in range(1, numQueries + numUpdates + 1):
    #     for k in range(1, numConfigurations + 1):
    #         if benefit.get((i, k), 0) > 0:   # or: configs[k] relevant to query i
    #             x[i, k] = model.addVar(vtype=g.GRB.BINARY, name=f'x[{i},{k}]')
    #
    # debug
    if debug:
        model.update()
        print(y)
        print(x)


    ###############
    # Constraints #
    ###############
    # one_config_{i} = query i uses atmost 1 configuration
    for i in range(1, numQueries + numUpdates + 1):                                 # all queries
        model.addConstr(
            g.quicksum(x[i, k] for k in range(1, numConfigurations + 1)) <= 1,      # each query, all config
            name=f"one_config_{i}"
        )

    # cfg_{i}_{j}_{k} = for each x_i_k every y_j must be build
    # print(configs)
    # print(numCandidateIndexes)
    for i in range(1, numQueries + numUpdates + 1):
        for k in range(1, numConfigurations + 1):
            for j in configs[k]:
                model.addConstr(
                    x[i, k] <= y[j],
                    name=f"cfg_{i}_{k}_{j}"
                )

    # storage_{j} storage constraing
    model.addConstr(
        g.quicksum(y[j]*indexSize[j] for j in range(1, numCandidateIndexes+1)) <= storageBudget,
        name=f'storage_constraint'
    )

    # debug
    if debug:
        model.update()

        for c in model.getConstrs():
            row = model.getRow(c)
            
            print(f"\nConstraint: {c.ConstrName}")

            expr = ""
            for i in range(row.size()):
                coeff = row.getCoeff(i)
                var = row.getVar(i)
                expr += f"{coeff}*{var.VarName} + "

            expr = expr[:-3]  # remove last " + "

            if c.Sense == '<':
                sense = "<="
            elif c.Sense == '>':
                sense = ">="
            else:
                sense = "="

            print(f"{expr} {sense} {c.RHS}")
            print(row)
            print()



    # model.Params.NodefileStart = 4.0

    ######################
    # Objective Function #
    ######################

    obj = (
        g.quicksum(
            benefit[i,k] * x[i, k]
            for i in range(1, numQueries + numUpdates + 1)
            for k in range(1, numConfigurations + 1)
        )
        -
        g.quicksum(
            f[j] * y[j]
            for j in range(1, numCandidateIndexes + 1)
        )
    )

    model.setObjective(obj, g.GRB.MAXIMIZE)

    # debug
    if debug:
        model.update()
        print(model.getObjective())
        print(benefit)
        print(f)

    model.optimize()

    ##################
    # Print Solution #
    ##################
    # for j in range(1, numCandidateIndexes+1):
    #     print(f"y{j} = {y[j]}")
    # for i in range(1, numQueries+numUpdates+1):
    #     for k in range(1, numConfigurations+1):
    #         print(f"x[{i},{k}] = {x[i,k]}")

    final_config = []
    if model.Status == g.GRB.OPTIMAL:
        print("\nSelected indexes:")
        for j in range(1, numCandidateIndexes + 1):
            if y[j].X == 1:
                print(f"y[{j}] = 1")
                final_config.append(j)

        print("\nSelected configurations:")
        for i in range(1, numQueries + numUpdates + 1):
            for k in range(1, numConfigurations + 1):
                if x[i, k].X == 1:
                    print(f"x[{i},{k}] = 1")
                    
        print(f"\nOptimal Z = {model.ObjVal}")

    return final_config
