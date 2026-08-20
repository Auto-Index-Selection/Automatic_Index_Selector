import gurobipy as g
import random
import statistics


numQueries = 100
numUpdates = 50


def solveILP(configs, indexSize, cost_initial, cost_final, cost_update):


    storageBudget = 3000
    # benifits of queries and updates
    benefit = { (i,k): cost_initial[i]-cost_final[i, k] for i in range(1, numQueries+numUpdates+1) for k in range(1, numConfigurations+1)}
    # negative benefits of a particular index over multiple update statements
    f = {}
    for j in range(1, numCandidateIndexes + 1):
        f[j] = sum(
            cost_update[l,j]
            for l in range(1, numUpdates + 1)
        )

    ##############
    # Init model #
    ##############
    model = g.Model()

    model.Params.OutputFlag=0
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

    model.optimize()
    return model.Runtime

numCandidateIndexesVar = [10, 20, 30, 40, 50]
numConfigurationsVar = [100, 150, 200, 250, 300]

print("NumCandidateIndexes, NumConfigurations, Time(ms)")
for numCandidateIndexes in numCandidateIndexesVar:
    for numConfigurations in numConfigurationsVar:
        times = []
        for seed in range(5):
            random.seed(seed)
            configs = {
                i: random.sample(range(1, numCandidateIndexes+1), random.randint(1, numCandidateIndexes))
                for i in range(1, numConfigurations+1)
            }

            indexSize = {
                i :  random.randint(100, 500) for i in range(1, numCandidateIndexes+1)
            }

            
            cost_initial = { i: random.randint(1, 4) for i in range(1, numQueries+numUpdates+1)}
            cost_final = { (i, k): random.randint(2, 5) for i in range(1, numQueries+numUpdates+1) for k in range(1, numConfigurations+1)}

            cost_update = { (l, j): 0 for l in range(1, numUpdates+1) for j in range(1, numCandidateIndexes+1)}
            times.append( solveILP(configs, indexSize, cost_initial, cost_final, cost_update))
        mean_t = statistics.mean(times)
        std_t = statistics.stdev(times)
        print(f"{numCandidateIndexes}, {numConfigurations}, {mean_t:.3f}, {std_t: .3f}")