"""
Vector Autoregression (VAR) processes

References
----------
Lutkepohl (2005) New Introduction to Multiple Time Series Analysis
"""

from __future__ import division

from collections import defaultdict
from cStringIO import StringIO

import numpy as np
import numpy.linalg as npl
from numpy.linalg import cholesky as chol, solve
import scipy.stats as stats
import scipy.linalg as L

from scikits.statsmodels.tools.decorators import cache_readonly
from scikits.statsmodels.tools.tools import chain_dot
from scikits.statsmodels.tsa.tsatools import vec, unvec

import scikits.statsmodels.tsa.var.irf as irf_module
reload(irf_module)

from scikits.statsmodels.tsa.var.irf import IRAnalysis
from scikits.statsmodels.tsa.var.output import VARSummary

import scikits.statsmodels.tsa.tsatools as tsa
import scikits.statsmodels.tsa.var.output as output
import scikits.statsmodels.tsa.var.plotting as plotting
import scikits.statsmodels.tsa.var.util as util

mat = np.array

import pandas.util.testing as test
st = test.set_trace

#-------------------------------------------------------------------------------
# VAR process routines

def ma_rep(coefs, maxn=10):
    r"""
    MA(\infty) representation of VAR(p) process

    Parameters
    ----------
    coefs : ndarray (p x k x k)
    maxn : int
        Number of MA matrices to compute

    Notes
    -----
    VAR(p) process as

    .. math:: y_t = A_1 y_{t-1} + \ldots + A_p y_{t-p} + u_t

    can be equivalently represented as

    .. math:: y_t = \mu + \sum_{i=0}^\infty \Phi_i u_{t-i}

    e.g. can recursively compute the \Phi_i matrices with \Phi_0 = I_k

    Returns
    -------
    """
    p, k, k = coefs.shape
    phis = np.zeros((maxn+1, k, k))
    phis[0] = np.eye(k)

    # recursively compute Phi matrices
    for i in xrange(1, maxn + 1):
        for j in xrange(1, i+1):
            if j > p:
                break

            phis[i] += np.dot(phis[i-j], coefs[j-1])

    return phis

def is_stable(coefs, verbose=False):
    """
    Determine stability of VAR(p) system by examining the eigenvalues of the
    VAR(1) representation

    Parameters
    ----------
    coefs : ndarray (p x k x k)

    Returns
    -------
    is_stable : bool
    """
    A_var1 = util.comp_matrix(coefs)
    eigs = np.linalg.eigvals(A_var1)

    if verbose:
        print 'Eigenvalues of VAR(1) rep'
        for val in np.abs(eigs):
            print val

    return (np.abs(eigs) <= 1).all()

def var_acf(coefs, sig_u, nlags=None):
    """
    Compute autocovariance function ACF_y(h) up to nlags of stable VAR(p)
    process

    Parameters
    ----------
    coefs : ndarray (p x k x k)
        Coefficient matrices A_i
    sig_u : ndarray (k x k)
        Covariance of white noise process u_t
    nlags : int, optional
        Defaults to order p of system

    Notes
    -----
    Ref: Lutkepohl p.28-29

    Returns
    -------
    acf : ndarray, (p, k, k)
    """
    p, k, _ = coefs.shape
    if nlags is None:
        nlags = p

    # p x k x k, ACF for lags 0, ..., p-1
    result = np.zeros((nlags + 1, k, k))
    result[:p] = _var_acf(coefs, sig_u)

    # yule-walker equations
    for h in xrange(p, nlags + 1):
        # compute ACF for lag=h
        # G(h) = A_1 G(h-1) + ... + A_p G(h-p)

        for j in xrange(p):
            result[h] += np.dot(coefs[j], result[h-j-1])

    return result

def _var_acf(coefs, sig_u):
    """
    Compute autocovariance function ACF_y(h) for h=1,...,p

    Notes
    -----
    Lutkepohl (2005) p.29
    """
    p, k, k2 = coefs.shape
    assert(k == k2)

    A = util.comp_matrix(coefs)
    # construct VAR(1) noise covariance
    SigU = np.zeros((k*p, k*p))
    SigU[:k,:k] = sig_u

    # vec(ACF) = (I_(kp)^2 - kron(A, A))^-1 vec(Sigma_U)
    vecACF = L.solve(np.eye((k*p)**2) - np.kron(A, A), vec(SigU))

    acf = unvec(vecACF)
    acf = acf[:k].T.reshape((p, k, k))

    return acf

def forecast(y, coefs, intercept, steps):
    """
    Produce linear MSE forecast

    Parameters
    ----------


    Notes
    -----
    Lutkepohl p. 37
    """
    p = len(coefs)
    k = len(coefs[0])
    # initial value
    forcs = np.zeros((steps, k)) + intercept

    # h=0 forecast should be latest observation
    # forcs[0] = y[-1]

    # make indices easier to think about
    for h in xrange(1, steps + 1):
        # y_t(h) = intercept + sum_1^p A_i y_t_(h-i)
        f = forcs[h - 1]
        for i in xrange(1, p + 1):
            # slightly hackish
            if h - i <= 0:
                # e.g. when h=1, h-1 = 0, which is y[-1]
                prior_y = y[h - i - 1]
            else:
                # e.g. when h=2, h-1=1, which is forcs[0]
                prior_y = forcs[h - i - 1]

            # i=1 is coefs[0]
            f = f + np.dot(coefs[i - 1], prior_y)
        forcs[h - 1] = f

    return forcs

def forecast_cov(ma_coefs, sig_u, steps):
    """
    Compute forecast error variance matrices
    """
    k = len(sig_u)
    forc_covs = np.zeros((steps, k, k))

    prior = np.zeros((k, k))
    for h in xrange(steps):
        # Sigma(h) = Sigma(h-1) + Phi Sig_u Phi'
        phi = ma_coefs[h]
        var = chain_dot(phi, sig_u, phi.T)
        forc_covs[h] = prior = prior + var

    return forc_covs

def var_loglike(resid, omega, nobs):
    r"""
    Returns the value of the VAR(p) log-likelihood.

    Parameters
    ----------
    resid : ndarray (T x K)
    omega : ndarray
        Sigma hat matrix.  Each element i,j is the average product of the
        OLS residual for variable i and the OLS residual for variable j or
        np.dot(resid.T,resid)/nobs.  There should be no correction for the
        degrees of freedom.
    nobs : int

    Returns
    -------
    loglike : float
        The value of the loglikelihood function for a VAR(p) model

    Notes
    -----
    The loglikelihood function for the VAR(p) is

    .. math::

        -\left(\frac{T}{2}\right)
        \left(\ln\left|\Omega\right|-K\ln\left(2\pi\right)-K\right)
    """
    logdet = util.get_logdet(np.asarray(omega))
    neqs = len(omega)
    part1 = - (nobs * neqs / 2) * np.log(2 * np.pi)
    part2 = - (nobs / 2) * (logdet + neqs)
    return part1 + part2

#-------------------------------------------------------------------------------
# VARProcess class: for known or unknown VAR process

class VAR(object):
    r"""
    Fit VAR(p) process and do lag order selection

    .. math:: y_t = A_1 y_{t-1} + \ldots + A_p y_{t-p} + u_t

    Notes
    -----
    **References**
    Lutkepohl (2005) New Introduction to Multiple Time Series Analysis

    Returns
    -------
    .fit() method returns VARResults object
    """

    def __init__(self, endog, names=None, dates=None):
        self.y, self.names = util.interpret_data(endog, names)
        self.nobs, self.neqs = self.y.shape

        if dates is not None:
            assert(self.nobs == len(dates))
        self.dates = dates

    def fit(self, maxlags=None, method='ols', ic=None, trend='c',
            verbose=False):
        """
        Fit the VAR model

        Parameters
        ----------
        maxlags : int
            Maximum number of lags to check for order selection, defaults to
            12 * (nobs/100.)**(1./4), see select_order function
        method : {'ols'}
            Estimation method to use
        ic : {'aic', 'fpe', 'hqic', 'bic', None}
            Information criterion to use for VAR order selection.
            aic : Akaike
            fpe : Final prediction error
            hqic : Hannan-Quinn
            bic : Bayesian a.k.a. Schwarz
        verbose : bool, default False
            Print order selection output to the screen
        trend, str {"c", "ct", "ctt", "nc"}
            "c" - add constant
            "ct" - constant and trend
            "ctt" - constant, linear and quadratic trend
            "nc" - co constant, no trend
            Note that these are prepended to the columns of the dataset.

        Notes
        -----
        Lutkepohl pp. 146-153

        Returns
        -------
        est : VARResults
        """
        lags = maxlags

        if ic is not None:
            selections = self.select_order(maxlags=maxlags, verbose=verbose)
            if ic not in selections:
                raise Exception("%s not recognized, must be among %s"
                                % (ic, sorted(selections)))
            lags = selections[ic]
            if verbose:
                print 'Using %d based on %s criterion' %  (lags, ic)

        return self._estimate_var(lags, trend=trend)

    def _estimate_var(self, lags, trend='c'):
        trendorder = util.get_trendorder(trend)

        z = util.get_var_endog(self.y, lags, trend=trend)
        y_sample = self.y[lags:]

        # Lutkepohl p75, about 5x faster than stated formula
        params = np.linalg.lstsq(z, y_sample)[0]
        resid = y_sample - np.dot(z, params)

        # Unbiased estimate of covariance matrix $\Sigma_u$ of the white noise
        # process $u$
        # equivalent definition
        # .. math:: \frac{1}{T - Kp - 1} Y^\prime (I_T - Z (Z^\prime Z)^{-1}
        # Z^\prime) Y
        # Ref: Lutkepohl p.75
        # df_resid right now is T - Kp - 1, which is a suggested correction

        avobs = len(y_sample)

        df_resid = avobs - (self.neqs * lags + trendorder)

        sse = np.dot(resid.T, resid)
        omega = sse / df_resid

        return VARResults(self.y, z, params, omega, lags, names=self.names,
                          dates=self.dates, model=self)

    def select_order(self, maxlags=None, verbose=True):
        """

        Parameters
        ----------

        Returns
        -------
        """
        if maxlags is None:
            maxlags = int(round(12*(self.nobs/100.)**(1/4.)))

        ics = defaultdict(list)
        for p in range(maxlags + 1):
            result = self._estimate_var(p)
            for k, v in result.info_criteria.iteritems():
                ics[k].append(v)

        selected_orders = dict((k, mat(v).argmin())
                               for k, v in ics.iteritems())

        if verbose:
            output.print_ic_table(ics, selected_orders)

        return selected_orders

class VARProcess(object):
    """
    Class represents a known VAR(p) process

    Parameters
    ----------
    coefs : ndarray (p x k x k)
    intercept : ndarray (length k)
    sigma_u : ndarray (k x k)
    names : sequence (length k)

    Returns
    -------
    **Attributes**:

    """
    def __init__(self, coefs, intercept, sigma_u, names=None):
        self.p = len(coefs)
        self.neqs = coefs.shape[1]
        self.coefs = coefs
        self.intercept = intercept
        self.sigma_u = sigma_u
        self.names = names

    def get_eq_index(self, name):
        return util.get_index(self.names, name)

    def __str__(self):
        output = ('VAR(%d) process for %d-dimensional response y_t'
                  % (self.p, self.neqs))
        output += '\nstable: %s' % self.is_stable()
        output += '\nmean: %s' % self.mean()

        return output

    def is_stable(self, verbose=False):
        """Determine stability based on model coefficients

        Parameters
        ----------
        verbose : bool
            Print eigenvalues of VAR(1) rep matrix

        Notes
        -----

        """
        return is_stable(self.coefs, verbose=verbose)

    def plotsim(self, steps=1000):
        Y = util.varsim(self.coefs, self.intercept, self.sigma_u, steps=steps)
        plotting.plot_mts(Y)

    def mean(self):
        r"""Mean of stable process

        Lutkepohl eq. 2.1.23

        .. math:: \mu = (I - A_1 - \dots - A_p)^{-1} \alpha
        """
        return solve(self._char_mat, self.intercept)

    def ma_rep(self, maxn=10):
        """Compute MA(\infty) coefficient matrices (also are impulse response
        matrices))

        Parameters
        ----------
        maxn : int
            Number of coefficient matrices to compute

        Returns
        -------
        coefs : ndarray (maxn x k x k)
        """
        return ma_rep(self.coefs, maxn=maxn)

    def orth_ma_rep(self, maxn=10, P=None):
        """Compute Orthogonalized MA coefficient matrices using P matrix such
        that \Sigma_u = PP'. P defaults to the Cholesky decomposition of
        \Sigma_u

        Parameters
        ----------
        maxn : int
            Number of coefficient matrices to compute
        P : ndarray (k x k), optional
            Matrix such that Sigma_u = PP', defaults to Cholesky descomp

        Returns
        -------
        coefs : ndarray (maxn x k x k)
        """
        if P is None:
            P = self._chol_sigma_u

        ma_mats = self.ma_rep(maxn=maxn)
        return mat([np.dot(coefs, P) for coefs in ma_mats])

    def long_run_effects(self):
        return L.inv(self._char_mat)

    @cache_readonly
    def _chol_sigma_u(self):
        return chol(self.sigma_u)

    @cache_readonly
    def _char_mat(self):
        return np.eye(self.neqs) - self.coefs.sum(0)

    def acf(self, nlags=None):
        """
        Compute

        """
        return var_acf(self.coefs, self.sigma_u, nlags=nlags)

    def acorr(self, nlags=None):
        return util.acf_to_acorr(var_acf(self.coefs, self.sigma_u,
                                         nlags=nlags))

    def plot_acorr(self, nlags=10, linewidth=8):
        plotting.plot_acorr(self.acorr(nlags=nlags), linewidth=linewidth)

    def forecast(self, y, steps):
        """Produce linear minimum MSE forecasts for desired number of steps
        ahead, using prior values y

        Parameters
        ----------
        y : ndarray (p x k)
        steps : int

        Returns
        -------

        Notes
        -----
        Lutkepohl pp 37-38
        """
        return forecast(y, self.coefs, self.intercept, steps)

    def mse(self, steps):
        """Compute forecast error covariance matrices

        Parameters
        ----------


        Returns
        -------
        covs : (steps, k, k)
        """
        return forecast_cov(self.ma_rep(steps), self.sigma_u, steps)

    forecast_cov = mse

    def _forecast_vars(self, steps):
        covs = self.forecast_cov(steps)

        # Take diagonal for each cov
        inds = np.arange(self.neqs)
        return covs[:, inds, inds]

    def forecast_interval(self, y, steps, alpha=0.05):
        """Construct forecast interval estimates assuming the y are Gaussian

        Parameters
        ----------

        Notes
        -----
        Lutkepohl pp. 39-40

        Returns
        -------
        (lower, mid, upper) : (ndarray, ndarray, ndarray)
        """
        assert(0 < alpha < 1)
        q = util.norm_signif_level(alpha)

        point_forecast = self.forecast(y, steps)
        sigma = np.sqrt(self._forecast_vars(steps))

        forc_lower = point_forecast - q * sigma
        forc_upper = point_forecast + q * sigma

        return point_forecast, forc_lower, forc_upper

#-------------------------------------------------------------------------------
# VARResults class


class VARResults(VARProcess):
    """Estimate VAR(p) process with fixed number of lags

    Returns
    -------
    **Attributes**
    k : int
        Number of variables (equations)
    p : int
        Order of VAR process
    T : Number of model observations (len(data) - p)
    y : ndarray (K x T)
        Observed data
    names : ndarray (K)
        variables names

    df_model : int
    df_resid : int

    coefs : ndarray (p x K x K)
        Estimated A_i matrices, A_i = coefs[i-1]
    intercept : ndarray (K)
    params : ndarray (Kp + 1) x K
        A_i matrices and intercept in stacked form [int A_1 ... A_p]

    sigma_u : ndarray (K x K)
        Estimate of white noise process variance Var[u_t]
    """
    _model_type = 'VAR'

    def __init__(self, y, ys_lagged, params, sigma_u, lag_order,
                 model=None, trendorder=1, names=None, dates=None):

        self.model = model
        self.y = y
        self.ys_lagged = ys_lagged
        self.dates = dates

        self.totobs, neqs = self.y.shape
        self.nobs = self.totobs  - lag_order
        self.trendorder = trendorder

        self.coef_names = util.make_lag_names(names, lag_order, trendorder)
        self.params = params

        # Initialize VARProcess parent class
        # construct coefficient matrices
        # Each matrix needs to be transposed
        reshaped = self.params[1:].reshape((lag_order, neqs, neqs))

        # Need to transpose each coefficient matrix
        intercept = self.params[0]
        coefs = reshaped.swapaxes(1, 2).copy()

        VARProcess.__init__(self, coefs, intercept, sigma_u,
                            names=names)

    def plot(self):
        plotting.plot_mts(self.y, names=self.names, index=self.dates)

    @property
    def df_model(self):
        """
        Number of estimated parameters, including the intercept / trends
        """
        return self.neqs * self.p + self.trendorder

    @property
    def df_resid(self):
        return self.nobs - self.df_model

    @cache_readonly
    def resid(self):
        return self.y[self.p:] - np.dot(self.ys_lagged, self.params)

    @cache_readonly
    def sigma_u_mle(self):
        return self.sigma_u * self.df_resid / self.nobs

    @cache_readonly
    def cov_params(self):
        # Covariance of vec(B), where B is the matrix
        # [intercept, A_1, ..., A_p] (K x (Kp + 1))
        # Adjusted to be an unbiased estimator
        # Ref: Lutkepohl p.74-75
        z = self.ys_lagged
        return np.kron(L.inv(np.dot(z.T, z)), self.sigma_u)

    def cov_ybar(self):
        r"""Asymptotically consistent estimate of covariance of the sample mean

        .. math::

            \sqrt(T) (\bar{y} - \mu) \rightarrow {\cal N}(0, \Sigma_{\bar{y}})\\

            \Sigma_{\bar{y}} = B \Sigma_u B^\prime, \text{where } B = (I_K - A_1
            - \cdots - A_p)^{-1}

        Notes
        -----
        L. Proposition 3.3
        """

        Ainv = L.inv(np.eye(self.neqs) - self.coefs.sum(0))
        return chain_dot(Ainv, self.sigma_u, Ainv.T)

#------------------------------------------------------------
# Estimation-related things

    @cache_readonly
    def _zz(self):
        # Z'Z
        return np.dot(self.ys_lagged.T, self.ys_lagged)

    @property
    def _cov_alpha(self):
        """
        Estimated covariance matrix of model coefficients ex intercept
        """
        # drop intercept
        return self.cov_params[self.neqs:, self.neqs:]

    @cache_readonly
    def _cov_sigma(self):
        """
        Estimated covariance matrix of vech(sigma_u)
        """
        D_K = tsa.duplication_matrix(self.neqs)
        D_Kinv = npl.pinv(D_K)

        sigxsig = np.kron(self.sigma_u, self.sigma_u)
        return 2 * chain_dot(D_Kinv, sigxsig, D_Kinv.T)

    @cache_readonly
    def loglike(self):
        return var_loglike(self.resid, self.sigma_u_mle, self.nobs)

    @cache_readonly
    def stderr(self):
        """Standard errors of coefficients, reshaped to match in size
        """
        stderr = np.sqrt(np.diag(self.cov_params))
        return stderr.reshape((self.df_model, self.neqs), order='C')

    bse = stderr  # statsmodels interface?

    def t(self):
        """Compute t-statistics. Use Student-t(T - Kp - 1) = t(df_resid) to test
        significance.
        """
        return self.params / self.stderr

    @cache_readonly
    def pvalues(self):
        return stats.t.sf(np.abs(self.t()), self.df_resid)*2

    def plot_forecast(self, steps, alpha=0.05):
        """
        Plot forecast
        """
        mid, lower, upper = self.forecast_interval(self.y[-self.p:], steps,
                                                   alpha=alpha)
        plotting.plot_var_forc(self.y, mid, lower, upper, names=self.names)

    # Forecast error covariance functions

    def forecast_cov(self, steps=1):
        r"""Compute forecast covariance matrices for desired number of steps

        Parameters
        ----------
        steps : int

        Notes
        -----
        .. math:: \Sigma_{\hat y}(h) = \Sigma_y(h) + \Omega(h) / T

        Ref: Lutkepohl pp. 96-97

        Returns
        -------
        covs : ndarray (steps x k x k)
        """
        mse = self.mse(steps)
        omegas = self._omega_forc_cov(steps)
        return mse + omegas / self.nobs

    def _omega_forc_cov(self, steps):
        # Approximate MSE matrix \Omega(h) as defined in Lut p97
        G = self._zz
        Ginv = L.inv(G)

        # memoize powers of B for speedup
        # TODO: see if can memoize better
        B = self._bmat_forc_cov()
        _B = {}
        def bpow(i):
            if i not in _B:
                _B[i] = np.linalg.matrix_power(B, i)

            return _B[i]

        phis = self.ma_rep(steps)
        sig_u = self.sigma_u

        omegas = np.zeros((steps, self.neqs, self.neqs))
        for h in range(1, steps + 1):
            if h == 1:
                omegas[h-1] = self.df_model * self.sigma_u
                continue

            om = omegas[h-1]
            for i in range(h):
                for j in range(h):
                    Bi = bpow(h - 1 - i)
                    Bj = bpow(h - 1 - j)
                    mult = np.trace(chain_dot(Bi.T, Ginv, Bj, G))
                    om += mult * chain_dot(phis[i], sig_u, phis[j].T)
            omegas[h-1] = om

        return omegas

    def _bmat_forc_cov(self):
        # B as defined on p. 96 of Lut
        upper = np.zeros((1, self.df_model))
        upper[0,0] = 1

        lower_dim = self.neqs * (self.p - 1)
        I = np.eye(lower_dim)
        lower = np.column_stack((np.zeros((lower_dim, 1)), I,
                                 np.zeros((lower_dim, self.neqs))))

        return np.vstack((upper, self.params.T, lower))

    def summary(self):
        return VARSummary(self)

    def irf(self, periods=10, var_decomp=None):
        """Analyze impulse responses to shocks in system

        Parameters
        ----------
        periods : int
        var_decomp : ndarray (k x k), lower triangular
            Must satisfy Omega = P P', where P is the passed matrix. Defaults to
            Cholesky decomposition of Omega

        Returns
        -------

        """
        return IRAnalysis(self, P=var_decomp, periods=periods)

    def fevd(self, periods=10, var_decomp=None):
        """
        Compute forecast error variance decomposition ("fevd")

        Returns
        -------
        fevd : FEVD instance
        """
        return FEVD(self, P=var_decomp, periods=periods)

#-------------------------------------------------------------------------------
# VAR Diagnostics: Granger-causality, whiteness of residuals, normality, etc.

    def test_causality(self, equation, variables, kind='f', signif=0.05,
                       verbose=True):
        """Compute test statistic for null hypothesis of Granger-noncausality,
        general function to test joint Granger-causality of multiple variables

        Parameters
        ----------
        equation : string or int
            Equation to test for causality
        variables : sequence (of strings or ints)
            List, tuple, etc. of variables to test for Granger-causality
        kind : {'f', 'wald'}
            Perform F-test or Wald (chi-sq) test
        signif : float, default 5%
            Significance level for computing critical values for test,
            defaulting to standard 0.95 level

        Notes
        -----
        Null hypothesis is that there is no Granger-causality for the indicated
        variables

        Returns
        -------
        results : dict
        """
        if isinstance(variables, (basestring, int, np.integer)):
            variables = [variables]

        k, p = self.neqs, self.p

        # number of restrictions
        N = len(variables) * self.p

        # Make restriction matrix
        C = np.zeros((N, k ** 2 * self.p + k), dtype=float)

        eq_index = self.get_eq_index(equation)
        vinds = mat([self.get_eq_index(v) for v in variables])

        # remember, vec is column order!
        offsets = np.concatenate([k + k ** 2 * j + k * vinds + eq_index
                                  for j in range(p)])
        C[np.arange(N), offsets] = 1

        # Lutkepohl 3.6.5
        Cb = np.dot(C, vec(self.params.T))
        middle = L.inv(chain_dot(C, self.cov_params, C.T))

        # wald statistic
        lam_wald = statistic = chain_dot(Cb, middle, Cb)

        if kind.lower() == 'wald':
            df = N
            dist = stats.chi2(df)
        elif kind.lower() == 'f':
            statistic = lam_wald / N
            df = (N, k * self.df_resid)
            dist = stats.f(*df)
        else:
            raise Exception('kind %s not recognized' % kind)

        pvalue = dist.sf(statistic)
        crit_value = dist.ppf(1 - signif)

        conclusion = 'fail to reject' if statistic < crit_value else 'reject'
        results = {
            'statistic' : statistic,
            'crit_value' : crit_value,
            'pvalue' : pvalue,
            'df' : df,
            'conclusion' : conclusion,
            'signif' :  signif
        }

        if verbose:
            summ = output.causality_summary(results, variables, equation, kind)

            print summ

        return results

    def test_whiteness(self):
        """
        Test white noise assumption
        """

        pass

    def test_normality(self):
        """
        Test assumption of Gaussian-distributed errors
        """
        pass

    @cache_readonly
    def detomega(self):
        r"""
        Return determinant of white noise covariance with degrees of freedom
        correction:

        .. math::

            \hat \Omega = \frac{T}{T - Kp - 1} \hat \Omega_{\mathrm{MLE}}
        """
        return L.det(self.sigma_u)

    @cache_readonly
    def info_criteria(self):
        # information criteria for order selection
        nobs = self.nobs
        neqs = self.neqs
        lag_order = self.p
        free_params = lag_order * neqs ** 2 + neqs * self.trendorder

        ld = util.get_logdet(self.sigma_u_mle)

        # See Lutkepohl pp. 146-150

        aic = ld + (2. / nobs) * free_params
        bic = ld + (np.log(nobs) / nobs) * free_params
        hqic = ld + (2. * np.log(np.log(nobs)) / nobs) * free_params
        fpe = ((nobs + self.df_model) / self.df_resid) ** neqs * np.exp(ld)

        return {
            'aic' : aic,
            'bic' : bic,
            'hqic' : hqic,
            'fpe' : fpe
            }

    @property
    def aic(self):
        # Akaike information criterion
        return self.info_criteria['aic']

    @property
    def fpe(self):
        """
        Lutkepohl p. 147, see info_criteria
        """
        # Final Prediction Error (FPE)
        return self.info_criteria['fpe']

    @property
    def hqic(self):
        # Hannan-Quinn criterion
        return self.info_criteria['hqic']

    @property
    def bic(self):
        # Bayesian a.k.a. Schwarz info criterion
        return self.info_criteria['bic']

class FEVD(object):
    """
    Compute and plot Forecast error variance decomposition and asymptotic
    standard errors
    """
    def __init__(self, model, P=None, periods=None):
        self.periods = periods

        self.model = model
        self.neqs = model.neqs
        self.names = model.names

        self.irfobj = model.irf(var_decomp=P, periods=periods)
        self.orth_irfs = self.irfobj.orth_irfs

        # cumulative impulse responses
        irfs = (self.orth_irfs[:periods] ** 2).cumsum(axis=0)

        rng = range(self.neqs)
        mse = self.model.mse(periods)[:, rng, rng]

        # lag x equation x component
        fevd = np.empty_like(irfs)

        for i in range(periods):
            fevd[i] = (irfs[i].T / mse[i]).T

        # switch to equation x lag x component
        self.decomp = fevd.swapaxes(0, 1)

    def summary(self):
        buf = StringIO()

        rng = range(self.periods)
        for i in range(self.neqs):
            ppm = output.pprint_matrix(self.decomp[i], rng, self.names)

            print >> buf, 'FEVD for %s' % self.names[i]
            print >> buf, ppm

        print buf.getvalue()

    def cov(self):
        """
        Compute asymptotic standard errors

        Returns
        -------
        """
        pass

    def plot(self, periods=None, figsize=(10,10), **plot_kwds):
        """
        Plot graphical display of FEVD

        Parameters
        ----------
        periods : int, default None
            Defaults to number originally specified. Can be at most that number
        """
        import matplotlib.pyplot as plt

        k = self.neqs
        periods = periods or self.periods

        fig, axes = plt.subplots(nrows=k, figsize=(10,10))

        fig.suptitle('Forecast error variance decomposition (FEVD)')

        colors = [str(c) for c in np.arange(k, dtype=float) / k]
        ticks = np.arange(periods)

        limits = self.decomp.cumsum(2)

        for i in range(k):
            ax = axes[i]

            this_limits = limits[i].T

            handles = []

            for j in range(k):
                lower = this_limits[j - 1] if j > 0 else 0
                upper = this_limits[j]
                handle = ax.bar(ticks, upper - lower, bottom=lower,
                                color=colors[j], label=self.names[j],
                                **plot_kwds)

                handles.append(handle)

            ax.set_title(self.names[i])

        # just use the last axis to get handles for plotting
        handles, labels = ax.get_legend_handles_labels()
        fig.legend(handles, labels, loc='upper right')
        plotting.adjust_subplots(right=0.85)

#-------------------------------------------------------------------------------

if __name__ == '__main__':
    import scikits.statsmodels.api as sm
    from scikits.statsmodels.tsa.var.util import parse_lutkepohl_data

    np.set_printoptions(linewidth=140, precision=5)

    sdata, dates = parse_lutkepohl_data('data/%s.dat' % 'e1')

    names = sdata.dtype.names
    data = util.struct_to_ndarray(sdata)
    adj_data = np.diff(np.log(data), axis=0)
    # est = VAR(adj_data, p=2, dates=dates[1:], names=names)
    model = VAR(adj_data[:-16], dates=dates[1:-16], names=names)
    est = model.fit(maxlags=2)
    irf = est.irf()

    y = est.y[-2:]
    """
    # irf.plot_irf()

    # i = 2; j = 1
    # cv = irf.cum_effect_cov(orth=True)
    # print np.sqrt(cv[:, j * 3 + i, j * 3 + i]) / 1e-2

    # data = np.genfromtxt('Canada.csv', delimiter=',', names=True)
    # data = data.view((float, 4))
    """

    # mdata = sm.datasets.macrodata.load().data[['realgdp','realcons','realinv']]
    # names = mdata.dtype.names
    # data = mdata.view((float,3))
    # data = np.diff(np.log(data), axis=0)

    # model = VAR(data, names=names)
    # est = model.fit(maxlags=2)
    # irf = est.irf()