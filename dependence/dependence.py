﻿"""Impact of Dependencies.

The main class inspect the impact that dependencies can have on a quantity
of interest of the output of a model.
"""

import operator
import json
import warnings
import itertools
import os

import numpy as np
import pandas as pd
import openturns as ot
import h5py
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.cm as cmx
from mpl_toolkits.mplot3d import Axes3D
from scipy.stats import rv_continuous, norm
import pyDOE
from scipy.optimize import minimize
import nlopt

from pyquantregForest import QuantileForest

from .vinecopula import VineCopula, check_matrix
from .conversion import Conversion, get_tau_interval
from .correlation import create_random_kendall_tau

OPERATORS = {">": operator.gt, ">=": operator.ge,
             "<": operator.lt, "<=": operator.le}


class ImpactOfDependence(object):
    """
    Quantify the impact of dependencies.

    This class study the impact of eventual dependencies of random variables on
    a quantity of interest of the output distribution. Different forms of
    dependencies, using the notion of copulas, are availaible to study the
    relation between the output and the dependencies.

    Parameters
    ----------
    model_func : callable
        The evaluation model such as :math:`Y = g(\mathbf X)`.

    margins : list of :class:`~openturns.Distribution`
        The probabilistic distribution :math:`f_{X_i}` for :math:`i \in 1 \dots d` of the margins.

    copula_name : string, optional (default="NormalCopula")
        The copula name $C$ to describes the dependence structure.
        The copula name must be supported and in the list of available copulas.

    Attributes
    ----------
    rand_vars : :class:`~openturns.ComposedDistribution`
        The random variable :math:`\mathbf X` describing the marginals and
        the copula.
    """
    _load_data = False

    def __init__(self, model_func, margins, families, vine_structure=None, copula_type='vine'):
        self.model_func = model_func
        self.margins = margins
        self.families = families
        self.vine_structure = vine_structure
        self.copula_type = copula_type
        self._forest_built = False
        self._lhs_grid_criterion = 'centermaximin'
        self._grid_folder = './experiment_designs'
        self._dep_measure = None

    @classmethod
    def from_data(cls, data_sample, params, out_ID=0, with_input_sample=True):
        """Load from data.

        This method initialise the class using built data from previous simulations.

        Parameters
        ----------
        data_sample : :class:`~numpy.ndarray`
            The sample of built data.
        dim : int
            The input dimension.
        out_ID : int, optional (default=0)
            The output ID to take care of when multiple output are available.
        """
        # Creates the class object from ghost parameters.
        def tmp(): return None
        dim = params['Input Dimension']
        families = np.asarray(params['Families'])
        structure = np.asarray(params['Structure'])
        margins = []
        for i in range(dim):
            d_marg = params['Marginal_%d' % (i)]
            marginal = getattr(ot, d_marg['Family'])(*d_marg['Parameters'])
            margins.append(marginal)
            
        obj = cls(tmp, margins, families, structure)
        all_params = data_sample[:, :obj._corr_dim]
        params = pd.DataFrame(all_params).drop_duplicates().values
        n_sample = all_params.shape[0]
        n_params = params.shape[0]
        n = n_sample / n_params
        
        data_sample_ordered = np.zeros(data_sample.shape)
        for i, par in enumerate(params):
            id_p = (all_params == par).all(axis=1)
            data_sample_ordered[i*n:(i+1)*n, :] = data_sample[id_p, :]
        
        #obj.all_params_ = data_sample_ordered[:, :obj._corr_dim]
        obj._params = params
        obj._n_param = n_params
        obj._n_sample = n_sample
        obj._n_input_sample = n
        
        if with_input_sample:
            obj._input_sample = data_sample_ordered[:, obj._corr_dim:obj._corr_dim + dim]
        obj._all_output_sample = data_sample_ordered[:, obj._corr_dim + dim:]
        obj._output_dim = obj._all_output_sample.shape[1]
        obj._output_ID = out_ID
        obj._load_data = True
        return obj

    @classmethod
    def from_structured_data(cls, loaded_data="full_structured_data.csv",
                             info_params='info_params.json', with_input_sample=True):
        """
        Load from structured with labels.

        loaded_data: a string, a DataFrame, a list of strings.
        """
        if isinstance(loaded_data, str):
            data = pd.read_csv(loaded_data)
        elif isinstance(loaded_data, pd.DataFrame):
            data = loaded_data
        elif isinstance(loaded_data, list):
            labels = ""
            data = None  # Init the data
            # For each data sample
            for i, load_dat in enumerate(loaded_data):
                # If it's a string
                if isinstance(load_dat, str):
                    # Load it in a DataFrame
                    dat_i = pd.read_csv(load_dat)
                # If it's a DataFrame
                elif isinstance(load_dat, pd.DataFrame):
                    dat_i = load_dat
                else:
                    raise TypeError("Uncorrect type for data")
                if i == 0:  # For the first file
                    data = dat_i  # Data
                    labels = data.columns.values  # Labels
                else:  # For the other files
                    # Check if the data have the sample features
                    assert (data.columns.values == labels).all(), \
                        "Different data files"
                    # Append all
                    data = data.append(dat_i)
        else:
            raise TypeError("Uncorrect type for loaded_data")

        with open(info_params, 'r') as param_f:
            params = json.load(param_f)

        return cls.from_data(data.values, params, with_input_sample=with_input_sample)
        
    @classmethod
    def from_hdf(cls, loaded_data="./output_result.hdf5", id_exp='all', out_ID=0, with_input_sample=True):
        """
        """
        # Ghost function
        def tmp(): return None
        
        # Load of the hdf5 file
        with h5py.File(loaded_data, 'r') as hdf_store:
            # The file may contain multiple experiments. The user can load one 
            # or multiple if they have similiar configurations.
            if id_exp == 'all':
                # We load and concatenate every groups of experiments
                list_index = hdf_store.keys()
                list_index.remove('dependence_params')
            else:
                list_index = [str(id_exp)]
            
            params = hdf_store['dependence_params'].value
            run_type = hdf_store.attrs['Run Type']
            n_params = hdf_store.attrs['K']
            families = hdf_store.attrs['Copula Families']
            structure = hdf_store.attrs['Copula Structure']
            copula_type = hdf_store.attrs['Copula Type']
            input_dim = hdf_store.attrs['Input Dimension']
            input_names = hdf_store.attrs['Input Names']

            margins = []
            for i in range(input_dim):
                marg_f = 'Marginal_%d Family' % (i)
                marg_p = 'Marginal_%d Parameters' % (i)
                marginal = getattr(ot, hdf_store.attrs[marg_f])(*hdf_store.attrs[marg_p])
                margins.append(marginal)
                
            if run_type == 'Classic':
                dep_measure = hdf_store.attrs['Dependence Measure']
                grid_type = hdf_store.attrs['Grid Type']
                if 'Grid Filename' in hdf_store.attrs.keys():
                    grid_filename = hdf_store.attrs['Grid Filename']
                else:
                    grid_filename = None
                if grid_type == 'lhs':
                    lhs_grid_criterion = hdf_store.attrs['LHS Criterion']
                else:
                    lhs_grid_criterion = None
                
            output_dim = hdf_store.attrs['Output Dimension']
            output_names = hdf_store.attrs['Output Names']
            
            list_input_sample = []
            list_output_sample = []
            list_n = []
            n = 0
            for k, index in enumerate(list_index):
                grp = hdf_store[index] # Group of the experiment
                
                data_in = grp['input_sample']
                data_out = grp['output_sample']

                list_input_sample.append(data_in.value)
                list_output_sample.append(data_out.value)
                list_n.append(grp.attrs['n'])

        # The sample are reordered
        n = sum(list_n)
        n_sample = n*n_params
        input_sample = np.zeros((n_sample, input_dim))
        output_sample = np.zeros((n_sample, output_dim))
        
        a = 0
        for i, ni in enumerate(list_n):
            for k in range(n_params):
                start = a + k*n
                end = start + ni
                input_sample[start:end, :] = list_input_sample[i][k*ni:(k+1)*ni, :]
                output_sample[start:end, :] = list_output_sample[i][k*ni:(k+1)*ni, :]
            a += ni
                    
        obj = cls(tmp, margins, families, structure)
                
        obj._params = params
        obj._n_param = n_params
        obj._n_sample = n_sample
        obj._n_input_sample = n
        
        if with_input_sample:
            obj._input_sample = input_sample
        obj._all_output_sample = output_sample
        obj._output_dim = output_dim
        obj._copula_type = copula_type
        obj._input_names = input_names
        obj._output_names = output_names
        obj._run_type = run_type
        
        obj._output_ID = out_ID
        obj._load_data = True
        
        if run_type == 'Classic':
            obj._dep_measure = dep_measure
            obj._grid = grid_type
            obj._grid_filename = grid_filename
            obj._lhs_grid_criterion = lhs_grid_criterion
        
        return obj

    def run(self, n_dep_param, n_input_sample, grid='rand',
            dep_measure="KendallTau", seed=None, use_grid=None, save_grid=None):
        """Run the problem. It creates and evaluates the sample from different
        dependence parameter values.

        Because the sampling can be difficult for some copula parameters with
        infinite range of definition. The use of dependence measure is
        a good approach to have a normalised measure. Moreover, it can 
        easily compared with other copulas.

        Parameters
        ----------
        n_dep_param : int
            The number of dependence parameters.

        n_input_sample : int
            The number of observations in the sampling of :math:`\mathbf{X}`.

        fixed_grid : bool, optional (default=False)
            The sampling of :math:`\mathbf{X}` is fixed or random.

        dep_measure : string, optional (default="KendallTau")
            The dependence measure used in the problem to explore the dependence 
            structures. Available dependence measures: 
            - "PearsonRho": The Pearson Rho parameter. Also called linear correlation parameter.
            - "KendallTau": The Tau Kendall parameter.

        output_ID : int, optional (default=0)
            The index of the output if the output is multidimensional.

        seed : int or None, optional (default=None)
            If int, ``seed`` is the seed used by the random number generator;
            If None, ``seed`` is the seed is random.

        Attributes
        ----------
        input_sample_ : :class:`~numpy.ndarray`
            The input sample

        output_sample_ : :class:`~numpy.ndarray`
            The output sample from the model.

        all_params_ : :class:`~numpy.ndarray`
            The dependence parameters associated to each output observation.
        """
        if seed is not None:  # Initialises the seed
            np.random.seed(seed)
            ot.RandomGenerator.SetSeed(seed)

        # Creates the sample of dependence parameters
        self._build_corr_sample(n_dep_param, grid, dep_measure, use_grid, save_grid)

        # Creates the sample of input parameters
        self._build_input_sample(n_input_sample)

        # Evaluates the input sample
        self._all_output_sample = self.model_func(self._input_sample)

        # Get output dimension
        self._output_info()
        
        self._run_type = 'Classic'

    def minmax_run(self, n_input_sample, seed=None, eps=1.E-8, store_input_sample=True):
        """
        """
        if seed is not None:  # Initialises the seed
            np.random.seed(seed)
            ot.RandomGenerator.SetSeed(seed)
            
        p = self._n_corr_vars
        self._n_param = 3**p
        self._params = np.zeros((self._n_param, self._corr_dim), dtype=float)

        tmp = tuple(itertools.product([-1. + eps, 1. - eps, 0.], repeat=p))
        self._params[:, self._corr_vars] = np.asarray(tmp, dtype=float)

        # Creates the sample of input parameters
        self._build_input_sample(n_input_sample)

        # Evaluates the input sample
        self._all_output_sample = self.model_func(self._input_sample)
        if not store_input_sample:
            del self._input_sample

        # Get output dimension
        self._output_info()
        
        self._run_type = 'Perfect Dependence'

    def run_independence(self, n_input_sample, seed=None):
        """
        """
        if seed is not None:  # Initialises the seed
            np.random.seed(seed)
            ot.RandomGenerator.SetSeed(seed)
            
        self._n_param = 1
        self._params = np.zeros((1, self._corr_dim), dtype=float)

        # Creates the sample of input parameters
        self._build_input_sample(n_input_sample)

        # Evaluates the input sample
        self._all_output_sample = self.model_func(self._input_sample)

        # Get output dimension
        self._output_info()
        
        self._run_type = 'Independence'

    def run_custom_param(self, param, n_input_sample, seed=None):
        """
        """
        if seed is not None:  # Initialises the seed
            np.random.seed(seed)
            ot.RandomGenerator.SetSeed(seed)

        self._n_param = 1
        self._params = param

        # Creates the sample of input parameters
        self._build_input_sample(n_input_sample)

        # Evaluates the input sample
        self._all_output_sample = self.model_func(self._input_sample)

        # Get output dimension
        self._output_info()
        
        self._run_type = 'Custom'
        
    def func_quant(self, param, alpha, n_input_sample):
        """
        """
        self._n_param = 1
        self._params = np.zeros((1, self._corr_dim), dtype=float)
        self._params[self._corr_vars] = param
        
        # Creates the sample of input parameters
        self._build_input_sample(n_input_sample)
        
        # Evaluates the input sample
        self._all_output_sample = self.model_func(self._input_sample)
        
        # Get output dimension
        self._output_info()
        
        self._run_type = 'Minimising'
        
        return np.percentile(self.output_sample_, alpha * 100.)
        
    def minimise_quantile(self, alpha, n_input_sample, eps=1.E-5):
        """
        """
        theta_0 = [.2]
#        return minimize(self.func_quant, theta_0, args=(alpha, n_input_sample),
#                        method='SLSQP',
#                        bounds=((-1. + eps, 1. - eps), ), tol=1.E-2,
#options={'eps': 1e-2, 'disp': True})        
        def func(param, grad):
            if grad.size > 0:  
                print grad
            return self.func_quant(param, alpha, n_input_sample)
            
        algorithm = nlopt.GN_DIRECT        
        opt = nlopt.opt(algorithm, self._n_corr_vars)
        opt.set_lower_bounds([-0.9])
        opt.set_upper_bounds([0.9])  
#        opt.set_xtol_rel(1e-3)
        opt.maxeval=10

        opt.set_min_objective(func)
        
        xopt = opt.optimize(theta_0)
        
        return xopt


    def _build_corr_sample(self, n_param, grid, dep_measure, use_grid, save_grid):
        """Creates the sample of dependence parameters.

        Parameters
        ----------
        n_param : int
            The number of dependence parameters.

        fixed_grid : bool
            The sampling of :math:`\mathbf X` is fixed or random.

        dep_measure : string
            The dependence measure used in the problem to explore the dependence 
            structures. Available dependence measures: 
            - "PearsonRho": The Pearson Rho parameter. Also called linear correlation parameter.
            - "KendallTau": The Tau Kendall parameter.
        """
        grid_filename = None
        p = self._n_corr_vars
        if grid == 'lhs':
            gridname = '%s_crit_%s' % (grid, self._lhs_grid_criterion)
        else:
            gridname = grid
        # If the design is loaded
        if use_grid is not None:
            # TODO: let the code load the latest design which have the same configs           
            if isinstance(use_grid, str):
                filename = use_grid
                assert gridname in use_grid, \
                    "Not the same configurations"
                name = os.path.basename(filename)
            elif isinstance(use_grid, (int, bool)):
                k = int(use_grid)
                name = '%s_p_%d_n_%d_%s_%d.csv' % (gridname, p, n_param, dep_measure, k)
                filename = os.path.join(self._grid_folder, name)
            else:
                raise AttributeError('Unknow use_grid')
                
            assert os.path.exists(filename), \
                'Grid file %s does not exists' % name
            print 'loading file %s' % name
            sample = np.loadtxt(filename).reshape(n_param, -1)
            assert n_param == sample.shape[0], \
                'Wrong grid size'
            assert p == sample.shape[1], \
                'Wrong dimension'
                
            meas_param = np.zeros((n_param, self._corr_dim))
            for k, i in enumerate(self._corr_vars):
                tau_min, tau_max = get_tau_interval(self._family_list[i])
                meas_param[:, i] = sample[:, k]

            grid_filename = filename
        else:
            sample = np.zeros((n_param, p))
            if dep_measure == "KendallTau":
                if grid == 'fixed':    
                    # Number of points per dimension
                    n_p = int((n_param) ** (1./p))
                    v = []
                    for i in self._corr_vars:
                        tau_min, tau_max = get_tau_interval(self._family_list[i])
                        v.append(np.linspace(tau_min, tau_max, n_p+1, endpoint=False)[1:])
    
                    tmp = np.vstack(np.meshgrid(*v)).reshape(p,-1).T
    
                    # The final total number is not always the initial one.
                    n_param = n_p ** p
                    meas_param = np.zeros((n_param, self._corr_dim))
                    for k, i in enumerate(self._corr_vars):
                        meas_param[:, i] = tmp[:, k]
                        tau_min, tau_max = get_tau_interval(self._family_list[i])
                        sample[:, k] = (meas_param[:, i] - tau_min) / (tau_max - tau_min)
                    
                elif grid == 'rand':  # Random grid
                    if self._copula_type == "vine":
                        meas_param = np.zeros((n_param, self._corr_dim))
                        for k, i in enumerate(self._corr_vars):
                            tau_min, tau_max = get_tau_interval(self._family_list[i])
                            meas_param[:, i] = np.random.uniform(tau_min, tau_max, n_param)
                            sample[:, k] = (meas_param[:, i] - tau_min) / (tau_max - tau_min)
                    elif self._copula_type == "normal":
                        meas_param = create_random_kendall_tau(self._families, 
                                                           n_param)
                        for k, i in enumerate(self._corr_vars):
                            tau_min, tau_max = get_tau_interval(self._family_list[i])
                            sample[:, k] = (meas_param[:, i] - tau_min) / (tau_max - tau_min)
                    else:
                        raise AttributeError('Unknow copula type:', self._copula_type)
                        
                elif grid == 'lhs':
                    meas_param = np.zeros((n_param, self._corr_dim))
                    sample = pyDOE.lhs(p, samples=n_param, 
                                       criterion=self._lhs_grid_criterion)
                    for k, i in enumerate(self._corr_vars):
                        tau_min, tau_max = get_tau_interval(self._family_list[i])
                        meas_param[:, i] = sample[:, k]*(tau_max - tau_min) + tau_min
                else:
                    raise AttributeError('%s is unknow for DOE type' % grid)
                    
            elif dep_measure == "PearsonRho":
                NotImplementedError("Work in progress.")
            elif dep_measure == "SpearmanRho":
                raise NotImplementedError("Not yet implemented")
            else:
                raise AttributeError("Unkown dependence parameter")

        if save_grid is not None and use_grid is None:
            # The grid is saved
            if save_grid is True: # No filename provided
                dirname = self._grid_folder
                if not os.path.exists(dirname):
                    os.mkdir(dirname)
                    
                k = 0
                do_save = True
                name = '%s_p_%d_n_%d_%s_%d.csv' % (gridname, p, n_param, dep_measure, k)
                filename = os.path.join(dirname, name)
                # If this file already exists
                while os.path.exists(filename):
                    existing_sample = np.loadtxt(filename).reshape(n_param, -1)
                    if np.allclose(np.sort(existing_sample, axis=0), np.sort(sample, axis=0)):
                        do_save = False
                        print 'The DOE already exist in %s' % (name)
                        break
                    k += 1
                    name = '%s_p_%d_n_%d_%s_%d.csv' % (gridname, p, n_param, dep_measure, k)
                    filename = os.path.join(dirname, name)
                
            grid_filename = filename
            # It is saved
            if do_save:
                np.savetxt(filename, sample)
            
        self._grid_filename = grid_filename
        self._dep_measure = dep_measure
        self._grid = grid
        self._n_param = n_param
        self._params = np.zeros((n_param, self._corr_dim))

        # Convert the dependence measure to copula parameters
        for i in self._corr_vars:
            self._params[:, i] = self._copula[i].to_copula_parameter(meas_param[:, i], dep_measure)

    def _build_input_sample(self, n):
        """Creates the observations for differents dependence parameters.

        Parameters
        ----------
        n : int
            The number of observations in the sampling of :math:`\mathbf X`.
        """
        n_sample = n * self._n_param
        self._n_sample = n_sample
        self._n_input_sample = n
        res_list = map(lambda param: self._get_sample(param, n), self._params)
        self._input_sample = np.concatenate(res_list)

    def _get_sample(self, param, n_obs, param2=None):
        """Creates the sample from the Vine Copula.

        Parameters
        ----------
        param : :class:`~numpy.ndarray`
            The copula parameters.
        n_obs : int
            The number of observations.
        param2 : :class:`~numpy.ndarray`, optional (default=None)
            The 2nd copula parameters. Usefull for certain copula families like Student.
        """
        dim = self._input_dim
        structure = self._vine_structure
        matrix_param = to_matrix(param, dim)

        if self._copula_type == 'vine':
            # TODO: One param is used. Do it for two parameters copulas.
            vine_copula = VineCopula(structure, self._families, matrix_param)

            # Sample from the copula
            # The reshape is in case there is only one sample (for RF tests)
            cop_sample = vine_copula.get_sample(n_obs).reshape(n_obs, self._input_dim)
        elif self._copula_type == 'normal':
            # Create the correlation matrix
            cor_matrix = matrix_param + matrix_param.T + np.identity(dim)
            cop = ot.NormalCopula(ot.CorrelationMatrix(cor_matrix))
            cop_sample = np.asarray(cop.getSample(n_obs), dtype=float)
        else:
            raise AttributeError('Unknown type of copula.')

        # Applied to the inverse transformation to get the sample of the joint distribution
        joint_sample = np.zeros((n_obs, dim), dtype=float)
        for i, inv_CDF in enumerate(self._margins_inv_CDF):
            joint_sample[:, i] = np.asarray(inv_CDF(cop_sample[:, i])).ravel()

        return joint_sample

    def _output_info(self):
        # If the output dimension is one
        if self._all_output_sample.shape[0] == self._all_output_sample.size:
            self._output_dim = 1
        else:
            self._output_dim = self._all_output_sample.shape[1]

    def build_forest(self, quant_forest=QuantileForest()):
        """Build a Quantile Random Forest to estimate conditional quantiles.
        """
        # We take only used params (i.e. the zero cols are taken off)
        # Actually, it should not mind to RF, but it's better for clarity.
        used_params = self.all_params_[:, self._corr_vars]
        quant_forest.fit(used_params, self.output_sample_)
        self._quant_forest = quant_forest
        self._forest_built = True

    def compute_quantity(self, quantity_func, options, boostrap=False):
        """Compute the output quantity of interest.
        quantity_func: can be many things
            - a callable function that compute the quantity of interest
            given the output sample,
            - "quantile" to compute the quantiles,
            - "probability" to compute the probability,
            - "variance" to compute the output variance,
            - "mean" to compute the output mean,
            - "super-quantile" to compute the super quantile.
        """
        if isinstance(quantity_func, str):
            if quantity_func == "quantile":
                self.compute_quantiles(*options)
                self._quantity = self._quantiles
                if boostrap:
                    raise Exception("Not implemented yet")
                else:
                    self._quantity_interval = None
                    self._quantity_up_bound = None
                    self._quantity_low_bound = None
            if quantity_func == "probability":
                self.compute_probability(*options)
                self._quantity = self._probability
                self._quantity_interval = self._probability_interval
                self._quantity_up_bound = self._probability + self._probability_interval
                self._quantity_low_bound = self._probability - \
                    self._probability_interval
        elif callable(quantity_func):
            out_sample = self.reshaped_output_sample_
            result = quantity_func(out_sample, *options)
            if isinstance(result, tuple):
                self._quantity = result[0]
                self._quantity_interval = result[1]
                self._quantity_up_bound = result[0] + result[1] / 2.
                self._quantity_low_bound = result[0] - result[1] / 2.
            else:
                self._quantity = result[0]
                self._quantity_interval = None
                self._quantity_up_bound = None
                self._quantity_low_bound = None
        else:
            raise TypeError("Unknow input variable quantity_func")

    def compute_probability(self, threshold, estimation_method='empirical',
                            confidence_level=0.95, operator='>', bootstrap=False,
                            output_ID=0):
        """Computes conditional probabilities for each parameters.
        
        Compute the probability of the current sample for each dependence
        parameter.

        Parameters
        ----------
        threshold : float, int
            The threshold :math:`s` of the output probability :math:`\mathbb P[Y \geq s]`
        estimation_method : string, optional (default='empirical')
            The probability estimation method. Available methods: 
            - 'empirical': Count the number of sample greater of lower to the threshold.
            - 'randomforest': Not yet implemented.
        confidence_level : float, optional (default=0.95)
            The confidence level probability.
        operator : string, optional (default='>')
            The operator of the probability :math:`\mathbb P[Y \geq s]`.
        """
        assert isinstance(threshold, (float, int)), \
            TypeError("Threshold should be a number.")
        assert isinstance(confidence_level, float), \
            TypeError("Confidence Level should be a float")
        assert 0. < confidence_level < 1., \
            ValueError("Confidence level should be a probability")
        assert isinstance(estimation_method, str), \
            TypeError("Method name should be a string")

        self._output_ID = output_ID
        configs = {'Quantity Name': 'Probability',
                  'Threshold': threshold,
                  'Confidence Level': confidence_level,
                  'Estimation Method': estimation_method,
                  'Operator': operator
                 }

        op_func = OPERATORS[operator]

        if estimation_method == "empirical":
            out_sample = self.reshaped_output_sample_
            probability = (op_func(out_sample, threshold).astype(float)).mean(axis=1)
            tmp = np.sqrt(probability * (1. - probability) /
                          self._n_input_sample)
            # Quantile of a Gaussian distribution
            q_normal = norm.ppf((1 + confidence_level) / 2.)
            interval = q_normal * tmp  # Confidence interval
            cond_params = self._params[:, self._corr_vars]
        elif estimation_method == 'randomforest':
            raise NotImplementedError('Not Yet Done...')
        else:
            raise AttributeError("Method does not exist")

        return DependenceResult(configs, self, probability, interval, cond_params)

    def compute_quantiles(self, alpha, estimation_method='empirical',
                          confidence_level=0.95, grid_size=None, bootstrap=False,
                          output_ID=0):
        """Computes conditional quantiles.

        Compute the alpha-quantiles of the current sample for each dependence
        parameter.

        Parameters
        ----------
        alpha : float
            Probability of the quantile.
        estimation_method : string, optional (default='empirical')
            The quantile estimation method. Available methods: 
            - 'empirical': The percentile of the output sample.
            - 'randomforest': Not yet implemented.
        confidence_level : float, optional (default=0.95)
            The confidence level probability.
        """
        assert isinstance(estimation_method, str), \
            TypeError("Method name should be a string")
        if isinstance(alpha, (list, np.ndarray)):
            for alphai in alpha:
                assert 0. < alphai < 1., \
                    ValueError("alpha should be a probability")
            alpha = np.asarray(alpha)
        elif isinstance(alpha, float):
            assert 0. < alpha < 1., \
                ValueError("alpha should be a probability")
        else:
            TypeError("Method name should be a float")
            
        self._output_ID = output_ID
        configs = {'Quantity Name': 'Quantile',
                  'Quantile Probability': alpha,
                  'Confidence Level': confidence_level,
                  'Estimation Method': estimation_method
                 }

        interval = None

        cond_params = self._params[:, self._corr_vars]
        if estimation_method == 'empirical':
            out_sample = self.reshaped_output_sample_
            if not bootstrap:
                quantiles = np.percentile(out_sample, alpha * 100., axis=1).reshape(1, -1)
            else:
                quantiles, interval = bootstrap_quantile(out_sample, alpha, 20, 0.01)
                
            # TODO: think about using the check function instead of the percentile.

        elif estimation_method == "randomforest":
            if not self._forest_built:
                warnings.warn(""""RandomForest has not been manually built. 
                Please use the build_forest method before computing the 
                quantiles.\nThe forest is build with default options for now.""",
                DeprecationWarning)
                self.build_forest()

            if grid_size:
                # Create the grid of params
                grid = np.zeros((self._n_corr_vars, grid_size))
                for i, k in enumerate(self._corr_vars):
                    # The bounds are taken from data, no need to go further.
                    p_min = self._copula[k].to_Kendall(self._params[:, k].min())
                    p_max = self._copula[k].to_Kendall(self._params[:, k].max())
                    # The space is filled for Kendall Tau params
                    tmp = np.linspace(p_min, p_max, grid_size+1, endpoint=False)[1:]
                    # Then, it is converted to copula parameters
                    grid[i, :] = self._copula[k].to_copula_parameter(tmp, 'KendallTau')

                # This is all the points from the grid.
                cond_params = np.vstack(np.meshgrid(*grid)).reshape(self._n_corr_vars,-1).T

            # Compute the quantiles
            quantiles = self._quant_forest.compute_quantile(cond_params, alpha)
        else:
            raise AttributeError(
                "Unknow estimation method: %s" % estimation_method)

        return DependenceResult(configs, self, quantiles, interval, cond_params)
        
    def save_data_hdf(self, input_names=[], output_names=[],
                  path=".", file_name="output_result.hdf5"):
        """
        """
        # List of input variable names
        if input_names:
            assert len(input_names) == self._input_dim, \
                AttributeError("Dimension problem for input_names")
        else:
            for i in range(self._input_dim):
                input_names.append("x_%d" % (i + 1))
                
        # List of output variable names
        if output_names:
            assert len(output_names) == self._output_dim, \
                AttributeError("Dimension problem for output_names")
        else:
            for i in range(self._output_dim):
                output_names.append("y_%d" % (i + 1))
                
        margin_dict = {}
        # List of marginal names
        for i, marginal in enumerate(self._margins):
                name = marginal.getName()
                params = list(marginal.getParameter())
                margin_dict['Marginal_%d Family' % (i)] = name
                margin_dict['Marginal_%d Parameters' % (i)] = params
               
        filename_exists = True
        k = 0
        while filename_exists:
            try:
                with h5py.File(os.path.join(path, file_name), 'a') as hdf_store:
                    # General attributes
                    # Check the attributes of the file, if it already exists
                    if hdf_store.attrs.keys():                        
                        np.testing.assert_allclose(hdf_store['dependence_params'].value, self._params)
                        assert hdf_store.attrs['Input Dimension'] == self._input_dim
                        assert hdf_store.attrs['Output Dimension'] == self._output_dim
                        assert hdf_store.attrs['Run Type'] == self._run_type
                        np.testing.assert_array_equal(hdf_store.attrs['Copula Families'], self._families)
                        np.testing.assert_array_equal(hdf_store.attrs['Copula Structure'], self._vine_structure)
                        assert hdf_store.attrs['Copula Type'] == self._copula_type
                        np.testing.assert_array_equal(hdf_store.attrs['Input Names'], input_names)
                        np.testing.assert_array_equal(hdf_store.attrs['Output Names'], output_names)
                        for i in range(self._input_dim):
                            assert hdf_store.attrs['Marginal_%d Family' % (i)] == margin_dict['Marginal_%d Family' % (i)]
                            np.testing.assert_array_equal(hdf_store.attrs['Marginal_%d Parameters' % (i)], margin_dict['Marginal_%d Parameters' % (i)])
                            
                        if self._run_type == 'Classic':
                            assert hdf_store.attrs['Dependence Measure'] == self._dep_measure
                            assert hdf_store.attrs['Grid Type'] == self._grid
                    else:
                        # We save the attributes in this empty new file                        
                        hdf_store.create_dataset('dependence_params', data=self._params)
                        hdf_store.attrs['K'] = self._n_param
                        hdf_store.attrs['Input Dimension'] = self._input_dim
                        hdf_store.attrs['Output Dimension'] = self._output_dim
                        hdf_store.attrs['Run Type'] = self._run_type
                        hdf_store.attrs['Copula Families'] = self._families
                        hdf_store.attrs['Copula Structure'] = self._vine_structure
                        hdf_store.attrs['Copula Type'] = self._copula_type
                        hdf_store.attrs['Input Names'] = input_names
                        hdf_store.attrs['Output Names'] = output_names
                        for i in range(self._input_dim):
                            hdf_store.attrs['Marginal_%d Family' % (i)] = margin_dict['Marginal_%d Family' % (i)]
                            hdf_store.attrs['Marginal_%d Parameters' % (i)] = margin_dict['Marginal_%d Parameters' % (i)]
                        
                        if self._run_type == 'Classic':
                            hdf_store.attrs['Dependence Measure'] = self._dep_measure
                            hdf_store.attrs['Grid Type'] = self._grid
                            if self._grid_filename:
                                hdf_store.attrs['Grid Filename'] = os.path.basename(self._grid_filename)
                            if self._grid == 'lhs':
                                hdf_store.attrs['LHS Criterion'] = self._lhs_grid_criterion      
                    
                    # Check the number of experiments
                    grp_number = 0
                    list_groups = hdf_store.keys()
                    list_groups.remove('dependence_params')
                    list_groups = [int(g) for g in list_groups]
                    list_groups.sort()
                    if list_groups:
                        grp_number = list_groups[-1] + 1
        
                    grp = hdf_store.create_group(str(grp_number))
                    grp.attrs['n'] = self._n_input_sample
                    grp.create_dataset('input_sample', data=self._input_sample)
                    grp.create_dataset('output_sample', data=self._all_output_sample.reshape((self._n_sample, self._output_dim)))
                    filename_exists = False
            except AssertionError:
                print 'File %s already has different configurations' % (file_name)
                file_name = '%s_%d.hdf5' % (file_name[:-5], k)
                k += 1

        print 'Data saved in %s' % (file_name)
        
        return file_name
            
    def save_data(self, input_names=[], output_names=[],
                  path=".", data_fname="full_structured_data",
                  ftype=".csv", param_fname='info_params'):
        """
        """
        output_dim = self._output_dim

        # List of correlated variable names
        labels = []
        for i in range(self._input_dim):
            for j in range(i):
                labels.append("r_%d%d" % (i + 1, j + 1))

        # List of input variable names
        if input_names:
            assert len(input_names) == self._input_dim, \
                AttributeError("Dimension problem for input_names")
            labels.extend(input_names)
        else:
            for i in range(self._input_dim):
                labels.append("x_%d" % (i + 1))

        # List of output variable names
        if output_names:
            assert len(output_names) == output_dim,\
                AttributeError("Dimension problem for output_names")
            labels.extend(output_names)
        else:
            for i in range(output_dim):
                labels.append("y_%d" % (i + 1))

        path_fname = path + '/' + data_fname + ftype
        out = np.c_[self.all_params_, self._input_sample,
                    self._all_output_sample]
        out_df = pd.DataFrame(out, columns=labels)
        out_df.to_csv(path_fname, index=False)

        # Save the parameters
        dict_output = {}
        dict_output['Input Dimension'] = self._input_dim
        dict_output['Parameter Number'] = self._n_param
        dict_output['Sample Size'] = self._n_input_sample
        dict_output['Grid'] = self._grid
        if self._grid_filename:
            dict_output['Grid Filename'] = os.path.basename(self._grid_filename)
        if self._grid == 'lhs':
            dict_output['LHS Criterion'] = self._lhs_grid_criterion
        

        dict_copula = {'Families': self._families.tolist(),
                       'Structure': self._vine_structure.tolist()}
                       
        dict_output.update(dict_copula)

        # TODO: Find a way to get the name of the variable for
        # Scipy frozen rv_continous instances
        if isinstance(self._margins[0], ot.DistributionImplementation):
            dict_margins = {}
            for i, marginal in enumerate(self._margins):
                name = marginal.getName()
                params = list(marginal.getParameter())
                dict_margins['Marginal_%d' % (i)] = {'Family': name,
                                                     'Parameters': params}
    
            dict_output.update(dict_margins)

        path_fname = path + '/' + param_fname + '.json'
        with open(path_fname, 'w') as outfile:
            json.dump(dict_output, outfile, indent=4)

    def draw_matrix_plot(self, corr_id=None, copula_space=False, figsize=(10, 10),
                         savefig=False):
        """
        """
        if corr_id is None:
            id_corr = np.ones(self._n_sample, dtype=bool)
        else:
            id_corr = np.where((self.all_params_ == self._params[corr_id]).all(axis=1))[0]

        data = self._input_sample[id_corr]
        
        if copula_space:
            x = np.zeros(data.shape)
            for i, marginal in enumerate(self._margins):
                for j, ui in enumerate(data[:, i]):
                    x[j, i] = marginal.computeCDF(ui)
        else:
            x = data

        fig, axes = plt.subplots(self._input_dim, self._input_dim, figsize=figsize, sharex='col')

        for i in range(self._input_dim):
            for j in range(self._input_dim):
                ax = axes[i, j]
                xi = x[:, i]
                xj = x[:, j]
                if i != j:
                    ax.plot(xj, xi, '.')
                if i == j:
                    ax.hist(xi, bins=30, normed=True)
                    
                if copula_space:
                    ax.set_xticks([])
                    ax.set_yticks([])

        fig.tight_layout()
        
        if savefig:
            if isinstance(savefig, str):
                fname = savefig + '/'
            else:
                fname = "./"
            fname += "matrix_plot"
            fig.savefig(fname + ".png", dpi=200)

        return fig

    def draw_design_space(self, corr_id=None, figsize=(10, 6),
                          savefig=False, color_map="jet", output_name=None,
                          input_names=None, return_fig=False, color_lims=None):
        """
        """
        assert self._input_dim in [2, 3], "Cannot draw quantiles for dim > 3"

        fig = plt.figure(figsize=figsize)  # Create the fig object

        if corr_id is None:
            id_corr = np.ones(self._n_sample, dtype=bool)
        else:
            id_corr = np.where((self.all_params_ == self._params[corr_id]).all(axis=1))[0]

        if input_names:
            param_name = input_names
        else:
            param_name = ["$x_{%d}$" % (i + 1) for i in range(self._input_dim)]

        if output_name:
            output_label = output_name
        else:
            output_label = "Output value"

        x = self._input_sample[id_corr]
        y = self.output_sample_[id_corr]
        color_scale = y
        cm = plt.get_cmap(color_map)
        if color_lims is None:
            c_min, c_max = min(color_scale), max(color_scale)
        else:
            c_min, c_max = color_lims[0], color_lims[1]
        cNorm = matplotlib.colors.Normalize(vmin=c_min, vmax=c_max)
        scalarMap = cmx.ScalarMappable(norm=cNorm, cmap=cm)

        if self._input_dim == 2:  # If input dimension is 2
            ax = fig.add_subplot(111)  # Creat the ax object
            x1 = x[:, 0]
            x2 = x[:, 1]
            ax.scatter(x1, x2, c=scalarMap.to_rgba(color_scale))
            scalarMap.set_array(color_scale)
            cbar = fig.colorbar(scalarMap)
            cbar.set_label(output_label)
            ax.set_xlabel(param_name[0], fontsize=14)
            ax.set_ylabel(param_name[1], fontsize=14)

        if self._input_dim == 3:  # If input dimension is 3
            x1 = x[:, 0]
            x2 = x[:, 1]
            x3 = x[:, 2]

            ax = fig.add_subplot(111, projection='3d')
            ax.scatter(x1, x2, x3, c=scalarMap.to_rgba(color_scale), s=40)
            scalarMap.set_array(color_scale)
            cbar = fig.colorbar(scalarMap)
            cbar.set_label(output_label)

            ax.set_xlabel(param_name[0], fontsize=14)
            ax.set_ylabel(param_name[1], fontsize=14)
            ax.set_zlabel(param_name[2], fontsize=14)

        title = "Design Space with $n = %d$ observations" % len(id_corr)
        if corr_id is not None:
            title += "\n$\\rho = "
            p = self._params[corr_id]
            if self._corr_dim == 1:
                title += "%.1f$" % (p)
            elif self._corr_dim == 3:
                p = self._params[corr_id]
                title += "[%.1f, %.1f, %.1f]$" % (p[0], p[1], p[2])
        ax.set_title(title, fontsize=18)
        ax.axis("tight")
        fig.tight_layout()

        if savefig:
            if isinstance(savefig, str):
                fname = savefig + '/'
            else:
                fname = "./"
            fname += "sample_space"
            fig.savefig(fname + ".pdf")
            fig.savefig(fname + ".png")

        if return_fig:
            return fig
        else:
            return None

# =============================================================================
# Setters
# =============================================================================
    @property
    def model_func(self):
        """The model function. Must be a callable.
        """
        return self._model_func

    @model_func.setter
    def model_func(self, func):
        if callable(func):
            self._model_func = func
        else:
            raise TypeError("The model function must be callable.")

    @property
    def margins(self):
        """The PDF margins. List of :class:`~openturns.Distribution` objects.
        """
        return self._margins

    @margins.setter
    def margins(self, list_margins):
        assert isinstance(list_margins, list), \
            TypeError("It should be a list of margins distribution objects.")

        self._margins_inv_CDF = []
        for marginal in list_margins:
            if isinstance(marginal, ot.DistributionImplementation):
                self._margins_inv_CDF.append(marginal.computeQuantile)
            elif isinstance(marginal, rv_continuous):
                self._margins_inv_CDF.append(marginal.ppf)
            else:
                TypeError("Must be scipy or OpenTURNS distribution objects.")

        self._margins = list_margins
        self._input_dim = len(list_margins)
        self._corr_dim = self._input_dim * (self._input_dim - 1) / 2

    @property
    def families(self):
        """The copula families.
        """
        return self._families

    @families.setter
    def families(self, matrix):
        matrix = matrix.astype(int)
        check_matrix(matrix)
        self._families = matrix
        self._family_list = []
        self._n_corr_vars = 0
        self._corr_vars = []
        k = 0
        list_vars = []
        for i in range(self._input_dim):
            for j in range(i):
                self._family_list.append(matrix[i, j])
                if matrix[i, j] > 0:
                    self._corr_vars.append(k)
                    self._n_corr_vars += 1
                    list_vars.append([i, j])
                k += 1

        self._copula = [Conversion(family) for family in self._family_list]
        self._corr_vars_ids = list_vars

    @property
    def copula_type(self):
        return self._copula_type

    @copula_type.setter
    def copula_type(self, value):
        assert isinstance(value, str), \
            TypeError('Type must be a string. Type given:', type(value))
            
        if value == "normal":
            families = self._families
            # Warn if the user added a wrong type of family
            if (families[self._families != 0] != 1).any():
                warnings.warn('Some families were not normal and you want an elliptic copula.')
            
            # Set all to families to normal
            families[self._families != 0] = 1
            self.families = families
        self._copula_type = value

    @property
    def vine_structure(self):
        return self._vine_structure

    @vine_structure.setter
    def vine_structure(self, structure):
        if structure is None:
            # TODO: The structure is standard, think about changing it.
            dim = self._input_dim
            structure = np.zeros((dim, dim), dtype=int)
            for i in range(dim):
                structure[i, 0:i+1, ] = i + 1
        else:
            check_matrix(structure)
        self._vine_structure = structure

    @property
    def output_sample_(self):
        if self._output_dim == 1:
            return self._all_output_sample
        else:
            return self._all_output_sample[:, self._output_ID]

    @output_sample_.setter
    def output_sample_(self, value):
        raise EnvironmentError("You cannot set this variable")

    @property
    def input_sample_(self):
        return self._input_sample

    @input_sample_.setter
    def input_sample_(self, value):
        raise EnvironmentError("You cannot set this variable")

    # TODO: there is a compromise between speed and memory efficiency...
    @property
    def all_params_(self):
        params = np.zeros((self._n_sample, self._corr_dim), dtype=float)
        n = self._n_input_sample
        for i, param in enumerate(self._params):
            params[n*i:n*(i+1), :] = param
        return params

    @property
    def reshaped_output_sample_(self):
        return self.output_sample_.reshape((self._n_param, self._n_input_sample))

    @reshaped_output_sample_.setter
    def reshaped_output_sample_(self, value):
        raise EnvironmentError("You cannot set this variable")
        
                
    def get_params(self, dep_meas='Kendall Tau', only_corr_vars=True):
        if dep_meas == 'Kendall Tau':
            params = np.zeros((self._n_param, self._corr_dim))
            for k in self._corr_vars:
                params[:, k] = self._copula[k].to_Kendall(self._params[:, k])
        
        if only_corr_vars:
            return params[:, self._corr_vars]
        else:
            return params


class DependenceResult(object):

    def __init__(self, configs, dependence_object, quantity,
                 confidence_interval=None, cond_params=None):
        """
        """
        #TODO: think of a way to delete the dependence object.
        #TODO: Use a ** object to pass the arguments
        self.configs = configs
        self.quantity = quantity
        self.confidence_interval = confidence_interval
        self.dependence = dependence_object
        self.cond_params = cond_params

    @property
    def cond_params(self):
        return self._cond_params

    @cond_params.setter
    def cond_params(self, value):
        self._cond_params = value

    @property
    def dependence(self):
        return self._dependence

    @dependence.setter
    def dependence(self, obj):
        # TODO: there is a bug sometimes. I don't know why it makes a difference 
        # between ImpactOfDependence and dependence.dependence.ImpactOfDependence
        #assert isinstance(obj, ImpactOfDependence), \
        #    TypeError('Variable must be an ImpactOfDependence object. Got instead:', type(obj))
        self._dependence = obj

    @property
    def configs(self):
        return self._configs

    @configs.setter
    def configs(self, value):
        assert isinstance(value, dict), \
            TypeError("It should be a dictionnary")

        self._quantity_name = value['Quantity Name']
        self._confidence_level = value['Confidence Level']
        self._estimation_method = value['Estimation Method']

        if self._quantity_name == 'Probability':
            self._threshold = value['Threshold']
            self._operator = value['Operator']
        elif self._quantity_name == 'Quantile':
            self._alpha = value['Quantile Probability']

        self._configs = value

    def __str__(self):
        to_print = '%s: %s\n' % (self._quantity_name, self.quantity)
        if self.confidence_interval is not None:
            to_print += '%s confidence interval at %d %%: %s' % \
                (self._quantity_name, self._confidence_level *
                 100, self.confidence_interval)
        return to_print

    def draw_bounds(self, indep_quant=None, figsize=(10, 6)):
        """
        """
        min_quantile = self.quantity.min(axis=1)
        max_quantile = self.quantity.max(axis=1)

        fig, ax = plt.subplots()
        if indep_quant is not None:
            ax.plot(indep_quant._alpha, indep_quant.quantity, 'b')
        ax.plot(self._alpha, min_quantile, '--b')
        ax.plot(self._alpha, max_quantile, '--b')

        ax.set_ylabel(self._quantity_name)
        ax.set_xlabel('$\\alpha$')
        ax.axis('tight')
        fig.tight_layout()

    def draw(self, id_alpha=0, dep_meas="KendallTau", figsize=(10, 6), 
             color_map="jet", max_eps=1.E-1,
             savefig=False, figpath='.'):
        """Draw the quantity with the dependence measure or copula
        parameter
        """
        obj = self._dependence
        copula_params = self._cond_params
        n_param, n_corr_vars = copula_params.shape

        assert n_corr_vars in [1, 2, 3],\
            EnvironmentError("Cannot draw the quantity for dim > 3")

        if dep_meas == "KendallTau":
            params = np.zeros((n_param, n_corr_vars))
            for i, k in enumerate(obj._corr_vars):
                params[:, i] = obj._copula[k].to_Kendall(copula_params[:, i])
            param_name = "\\tau"
        elif dep_meas == "CopulaParam":
            params = copula_params
            param_name = "\\rho"
        elif dep_meas == "PearsonRho":
            raise NotImplementedError("Still not done")
            params = None
            param_name = "\\rho^{Pearson}"
        else:
            raise AttributeError("Undefined param")

        
        # Output quantities of interest
        quantity = self.quantity[id_alpha, :].reshape(n_param, -1)
        if self.confidence_interval is not None:
            interval = self.confidence_interval[id_alpha, :]
        else:
            interval = None
        quantity_name = self._quantity_name

        # Find the "almost" independent configuration
        if n_corr_vars == 1:
            id_indep = (np.abs(params)).argmin()
        else:
            id_indep = np.abs(params).sum(axis=1).argmin()

        # Independent parameter and quantile
        indep_param = params[id_indep]
        indep_quant = quantity[id_indep]

        # If we have confidence interval
        if interval is not None:
            low_bound = quantity - interval
            up_bound = quantity + interval
            indep_quant_l_bound = low_bound[id_indep]
            indep_quant_u_bound = up_bound[id_indep]

        # If it's greater than the tolerence, no need to show it
        if np.sum(indep_param) > max_eps:
            print_indep = False
        else:
            print_indep = True

        fig = plt.figure(figsize=figsize)  # Create the fig object

        if n_corr_vars == 1:  # One used correlation parameter
            ax = fig.add_subplot(111)  # Create the axis object

            # Ids of the sorted parameters for the plot
            id_sorted_params = np.argsort(params, axis=0).ravel()

            # Plot of the quantile conditionally to the correlation parameter
            ax.plot(params[id_sorted_params], quantity[id_sorted_params, :],
                    'ob', label=quantity_name, linewidth=2)

            # Plot the confidence bounds
            if interval is not None:
                ax.plot(params[id_sorted_params], up_bound[id_sorted_params],
                        '--b', label=quantity_name + " confidence",
                        linewidth=2)
                ax.plot(params[id_sorted_params], low_bound[id_sorted_params],
                        '--b', linewidth=2)

            # Print a line to distinguish the difference with the independence
            # case
            if print_indep:
                p_min, p_max = params.min(), params.max()
                ax.plot([p_min, p_max], [indep_quant] * 2, "r-",
                        label="Independence")
                if interval is not None:
                    ax.plot([p_min, p_max], [indep_quant_l_bound] * 2, "r--")
                    ax.plot([p_min, p_max], [indep_quant_u_bound] * 2, "r--")

            i, j = obj._corr_vars_ids[0][0], obj._corr_vars_ids[0][1]
            ax.set_xlabel("$%s_{%d%d}$" % (param_name, i, j), fontsize=14)
            ax.set_ylabel(quantity_name)
            ax.legend(loc="best")

        elif n_corr_vars == 2:  # For 2 correlation parameters
            view = "3d"
            if view == "3d":
                # Dependence parameters values
                r1, r2 = params[:, 0], params[:, 1]

                # 3d ax
                ax = fig.add_subplot(111, projection='3d')
                # Draw the point with the colors
                ax.scatter(r1, r2, quantity, s=40)

                # Plot the confidence bounds
                if interval is not None:
                    ax.plot_trisurf(r1, r2, low_bound,
                                    color="red", alpha=0.05, linewidth=1)
                    ax.plot_trisurf(r1, r2, up_bound, color="red",
                                    alpha=0.05, linewidth=1)
                    #ax.plot(r1, r2, up_bound, 'r.')
                    #ax.plot(r1, r2, low_bound, 'r.')

                # Print a line to distinguish the difference with the
                # independence case
                if print_indep:
                    p1_min, p1_max = r1.min(), r1.max()
                    p2_min, p2_max = r2.min(), r2.max()
                    p1_ = np.linspace(p1_min, p1_max, 3)
                    p2_ = np.linspace(p2_min, p2_max, 3)
                    p1, p2 = np.meshgrid(p1_, p2_)
                    q = np.zeros(p1.shape) + indep_quant
                    ax.plot_wireframe(p1, p2, q, color="red")

                    if interval is not None:
                        q_l = np.zeros(p1.shape) + indep_quant_l_bound
                        q_u = np.zeros(p1.shape) + indep_quant_u_bound
                        ax.plot_wireframe(p1, p2, q_l, color="red")
                        ax.plot_wireframe(p1, p2, q_u, color="red")
                    #    ax.plot([p_min, p_max], [indep_quant_l_bound] * 2,
                    #    "r--")
                    #    ax.plot([p_min, p_max], [indep_quant_u_bound] * 2,
                    #    "r--")
                # Labels
                i, j = obj._corr_vars_ids[0][0], obj._corr_vars_ids[0][1]
                ax.set_xlabel("$%s_{%d%d}$" % (param_name, i, j), fontsize=14)
                i, j = obj._corr_vars_ids[1][0], obj._corr_vars_ids[1][1]
                ax.set_ylabel("$%s_{%d%d}$" % (param_name, i, j), fontsize=14)

                ax.set_zlabel(quantity_name)

        elif n_corr_vars == 3:  # For 2 correlation parameters
            color_scale = quantity
            cm = plt.get_cmap(color_map)
            c_min, c_max = min(color_scale), max(color_scale)
            cNorm = matplotlib.colors.Normalize(vmin=c_min, vmax=c_max)
            scalarMap = cmx.ScalarMappable(norm=cNorm, cmap=cm)

            x, y, z = params[:, 0], params[:, 1], params[:, 2]

            ax = fig.add_subplot(111, projection='3d')
            ax.scatter(x, y, z, c=scalarMap.to_rgba(color_scale), s=40)
            scalarMap.set_array(color_scale)
            cbar = fig.colorbar(scalarMap)
            if print_indep:
                pos = cbar.ax.get_position()
                cbar.ax.set_aspect('auto')
                ax2 = cbar.ax.twinx()
                ax2.set_ylim([c_min, c_max])
                width = 0.05
                pos.x0 = pos.x1 - width
                ax2.set_position(pos)
                cbar.ax.set_position(pos)
                n_label = 5
                labels_val = np.linspace(c_min, c_max, n_label).tolist()
                labels = [str(round(labels_val[i], 2)) for i in range(n_label)]
                labels_val.append(indep_quant)
                labels.append("Indep=%.2f" % indep_quant)
                ax2.set_yticks([indep_quant])
                ax2.set_yticklabels(["Indep"])

            ax.set_xlabel("$%s_{12}$" % (param_name), fontsize=14)
            ax.set_ylabel("$%s_{13}$" % (param_name), fontsize=14)
            ax.set_zlabel("$%s_{23}$" % (param_name), fontsize=14)
        
        # Other figure stuffs
        title = r"%s - $n = %d$" % (quantity_name, obj._n_input_sample)
        ax.set_title(title, fontsize=18)
        ax.axis("tight")
        fig.tight_layout()
    
        # Saving the figure
        if savefig:
            fname = figpath + '/'
            if isinstance(savefig, str):
                fname += savefig
            else:
                fname += "fig" + quantity_name
            fig.savefig(fname + ".pdf")
            fig.savefig(fname + ".png")
            
        return ax


def bootstrap_quantile(data, alpha, n_boot, alpha_boot):
    """Returns bootstrap estimate of 100.0*(1-alpha) CI for statistic."""
    n = len(data)
    idx = np.random.randint(0, n, (n_boot, n))
    samples = data[idx]
    stat = np.sort(np.percentile(samples, alpha * 100., axis=1))

    return stat.mean(axis=0), np.c_[stat[int((alpha_boot*0.5) * n_boot)],
            stat[int((1. - alpha_boot*0.5) * n_boot)]]
    

def to_matrix(param, dim):
    """
    """

    matrix = np.zeros((dim, dim))
    k = 0
    for i in range(dim):
        for j in range(i):
            matrix[i, j] = param[k]
            k += 1

    return matrix
