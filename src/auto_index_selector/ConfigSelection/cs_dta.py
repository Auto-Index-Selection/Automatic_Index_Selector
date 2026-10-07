from .config_sel import greedyMK

def selectConfiguration(conn, W, candidate_dict, m=2, k=10, cost_cache=None,
                        write_penalties=None, query_weights=None, **kwargs):
    """
    DTA (Database Tuning Advisor) style configuration selection.
    Uses the Greedy(m,k) algorithm as originally described in AutoAdmin.
    """
    return greedyMK(conn, W, candidate_dict, m=m, k=k,
                    cost_cache=cost_cache,
                    write_penalties=write_penalties,
                    query_weights=query_weights)
