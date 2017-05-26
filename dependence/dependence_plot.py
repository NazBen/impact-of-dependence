import matplotlib.pyplot as plt
import numpy as np

def get_all_quantity(results, q_func=None):
    """
    """
    quantities = []
    for res_name in results:
        if q_func is not None:
            # We change the quantity function
            results[res_name].q_func = q_func
        min_quantity = results[res_name].min_quantity
        quantities.append(min_quantity)
    return quantities

def get_all_min_result(results, q_func=None):
    """
    """
    min_results = []
    for res_name in results:
        if q_func is not None:
            # We change the quantity function
            results[res_name].q_func = q_func
        min_result = results[res_name].min_result
        min_results.append(min_result)
    return min_results

def get_min_result(all_min_results, q_func=None):
    """
    """
    min_result = None
    min_quantity = np.inf
    for result in all_min_results:
        if q_func is not None:
            # We change the quantity function
            result.q_func = q_func
        if result.min_quantity < min_quantity:
            min_result = result.min_result
            min_quantity = result.min_quantity
            
    return min_result

def get_n_pairs(all_results):
    """
    """
    n_pairs = []
    for results in all_results:
        n_pair = results[results.keys()[0]].n_pairs
        for res_name in results:
            assert results[res_name].n_pairs== n_pair, "Not the same numer of pairs... Weird"
            
        n_pairs.append(n_pair)
        
    return n_pairs

def plot_iterative_results(all_results, indep_result=None, grid_result=None, q_func=None, figsize=(8, 4), quantity_name='Quantity', with_bootstrap=False):
    """
    """
    delay = 0.02
    
    # Figure
    fig, ax = plt.subplots(figsize=figsize)
    
    # Number of trees
    n_levels = len(all_results)
    
    # Colors of the levels and independence
    cmap = plt.get_cmap('jet')
    n_p = 0
    n_p += 1 if indep_result is not None else 0
    n_p += 1 if grid_result is not None else 0
    colors = [cmap(i) for i in np.linspace(0, 1, n_levels+n_p)]
    
    # Number of pairs at each iteration
    n_pairs = get_n_pairs(all_results)   
    
    if indep_result is not None:
        ax.plot([n_pairs[0], n_pairs[-1]], [indep_result.quantity]*2, '-o', 
                color=colors[0], label='independence')
        
        if False:
            indep_result.compute_bootstrap()
            boot = indep_result.bootstrap_sample
            
            up = np.percentile(boot, 99)
            down = np.percentile(boot, 1)
            ax.plot([n_pairs[0], n_pairs[-1]], [up]*2, '--', 
                color=colors[0])
            ax.plot([n_pairs[0], n_pairs[-1]], [down]*2, '--', 
                color=colors[0])
        
    
    if grid_result is not None:
        ax.plot([n_pairs[0], n_pairs[-1]], [grid_result.quantity]*2, '-o', 
                color=colors[1], label='grid-search with $K=1000$')
        if with_bootstrap:
            grid_result.compute_bootstrap()
            boot = grid_result.bootstrap_sample
            up = np.percentile(boot, 95)
            down = np.percentile(boot, 5)
            ax.plot([n_pairs[0], n_pairs[-1]], [up]*2, '--', 
                color=colors[1], linewidth=0.8)
            ax.plot([n_pairs[0], n_pairs[-1]], [down]*2, '--', 
                color=colors[1], linewidth=0.8)
        
    quantities = []
    min_results_level = []
    for result_by_level in all_results:
        quantities.append(get_all_quantity(result_by_level, q_func=q_func))
        min_results_level.append(get_min_result(result_by_level.values(), q_func=q_func))

    # Get the minimum of each level
    min_quantities = []
    for quant_lvl in quantities:
        min_quant = min(quant_lvl)
        min_quantities.append(min_quant)
        
        # Remove the minimum from the list of quantities
        quant_lvl.remove(min_quant)
    
    for lvl in range(n_levels):
        # The quantities of this level
        quant_lvl = np.asarray(quantities[lvl]) - delay
        # The number of results
        n_res = len(quant_lvl)
        ax.plot([n_pairs[lvl]]*n_res, quant_lvl, '.', color=colors[lvl+n_p])
        
    for lvl in range(n_levels):
        if n_pairs[lvl] == n_pairs[-1]:
            ax.plot(n_pairs[lvl], min_quantities[lvl] - delay, 'o', color=colors[lvl+n_p])
            if with_bootstrap:
                min_results_level[lvl].compute_bootstrap()
                boot = min_results_level[lvl].bootstrap_sample
                up = np.percentile(boot, 95) - delay
                down = np.percentile(boot, 5) - delay
                ax.plot(n_pairs[lvl], up, '.',
                    color=colors[lvl+n_p], linewidth=0.8)
                ax.plot(n_pairs[lvl], down, '.',
                    color=colors[lvl+n_p], linewidth=0.8)
        else:
            ax.plot([n_pairs[lvl], n_pairs[lvl+1]], [min_quantities[lvl] - delay]*2, 'o-', color=colors[lvl+n_p])
            if with_bootstrap:
                min_results_level[lvl].compute_bootstrap()
                boot = min_results_level[lvl].bootstrap_sample
                up = np.percentile(boot, 95) - delay
                down = np.percentile(boot, 5) - delay
                ax.plot([n_pairs[lvl], n_pairs[lvl+1]], [up]*2, '--', 
                    color=colors[lvl+n_p], linewidth=0.8)
                ax.plot([n_pairs[lvl], n_pairs[lvl+1]], [down]*2, '--', 
                    color=colors[lvl+n_p], linewidth=0.8)
    
    ax.axis('tight')
    ax.set_xlabel('Number of considered pairs')
    ax.set_ylabel(quantity_name)
    ax.set_xticks(range(1, n_levels+1))
    ax.legend(loc=0)
    fig.tight_layout()        
