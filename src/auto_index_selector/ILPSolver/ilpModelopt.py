import gurobipy as g
import random
import gc

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
        range(1, numCandidateIndexes + 1),
        vtype=g.GRB.BINARY,
        name='y'
    )

    # x_ik : whether query 'i' uses configuration 'k'
    # SPARSE: only create a variable when benefit[i,k] is actually meaningful
    # (i.e. configuration k is relevant to query i). This is the fix —
    # previously x was dense over the full (query x config) cross-product,
    # which blew up to 54M+ variables even though most had benefit 0.
    x = {}
    valid_configs = {}  # i -> list of k for which x[i,k] exists

    for i in range(1, numQueries + numUpdates + 1):
        for k in range(1, numConfigurations + 1):
            b = benefit.get((i, k), 0)
            if b != 0:
                x[i, k] = model.addVar(vtype=g.GRB.BINARY, name=f'x[{i},{k}]')
                valid_configs.setdefault(i, []).append(k)

    model.update()

    # debug
    if debug:
        print(y)
        print(f"Created {len(x)} x variables (out of {numQueries + numUpdates} * {numConfigurations} possible)")

    ###############
    # Constraints #
    ###############
    # one_config_{i} = query i uses at most 1 configuration
    for i in range(1, numQueries + numUpdates + 1):
        ks = valid_configs.get(i, [])
        if ks:
            model.addConstr(
                g.quicksum(x[i, k] for k in ks) <= 1,
                name=f"one_config_{i}"
            )

    # cfg_{i}_{k}_{j} = for each x_i_k every y_j must be built
    for i in range(1, numQueries + numUpdates + 1):
        for k in valid_configs.get(i, []):
            for j in configs[k]:
                model.addConstr(
                    x[i, k] <= y[j],
                    name=f"cfg_{i}_{k}_{j}"
                )

    # storage_{j} storage constraint
    model.addConstr(
        g.quicksum(y[j] * indexSize[j] for j in range(1, numCandidateIndexes + 1)) <= storageBudget,
        name='storage_constraint'
    )

    # debug
    if debug:
        model.update()
        for c in model.getConstrs():
            row = model.getRow(c)
            print(f"\nConstraint: {c.ConstrName}")
            expr = ""
            for idx in range(row.size()):
                coeff = row.getCoeff(idx)
                var = row.getVar(idx)
                expr += f"{coeff}*{var.VarName} + "
            expr = expr[:-3]
            sense = "<=" if c.Sense == '<' else (">=" if c.Sense == '>' else "=")
            print(f"{expr} {sense} {c.RHS}")
            print(row)
            print()

    ######################
    # Objective Function #
    ######################
    obj = (
        g.quicksum(
            benefit[i, k] * x[i, k]
            for i in range(1, numQueries + numUpdates + 1)
            for k in valid_configs.get(i, [])
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


    del benefit
    del configs
    del indexSize
    del f

    gc.collect()

    model.Params.NodefileStart = 1.0

    model.optimize()

    ##################
    # Print Solution #
    ##################
    if model.Status == g.GRB.OPTIMAL:
        print("\nSelected indexes:")
        for j in range(1, numCandidateIndexes + 1):
            if y[j].X == 1:
                print(f"y[{j}] = 1")

        print("\nSelected configurations:")
        for i in range(1, numQueries + numUpdates + 1):
            for k in valid_configs.get(i, []):
                if x[i, k].X == 1:
                    print(f"x[{i},{k}] = 1")

        print(f"\nOptimal Z = {model.ObjVal}")
