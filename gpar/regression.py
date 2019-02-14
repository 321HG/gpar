# -*- coding: utf-8 -*-

from __future__ import absolute_import, division, print_function

import logging

import numpy as np
import torch
from lab.torch import B
from stheno.torch import Graph, GP, EQ, Linear
from varz import Vars, minimise_l_bfgs_b

from .model import GPAR

__all__ = ['GPARRegressor']
log = logging.getLogger(__name__)


def model_generator(vs,
                    m,
                    p,
                    scale,
                    linear,
                    linear_slope,
                    nonlinear,
                    nonlinear_scale,
                    noise):
    def model():
        # Start out with a constant kernel.
        kernel = vs.bnd(name=(p, 'constant'), group=p, init=noise)

        # Add nonlinear kernel over inputs.
        scales = vs.bnd(name=(p, 'I/NL/scales'), group=p,
                        init=scale * B.ones(m))
        variance = vs.bnd(name=(p, 'I/NL/var'), group=p, init=1.)
        inds = list(range(m))  # TODO: only do this if p > 1
        kernel += variance * EQ().stretch(scales).select(inds)

        # Add linear kernel if asked for.
        if linear:
            slopes = vs.bnd(name=(p, 'IO/L/slopes'), group=p,
                            init=linear_slope * B.ones(m + p - 1))
            kernel += Linear().stretch(1 / slopes)

        # Add nonlinear kernel over outputs if asked for.
        if nonlinear and p > 1:
            scales = vs.bnd(name=(p, 'O/NL/scales'), group=p,
                            init=nonlinear_scale * B.ones(p - 1))
            variance = vs.bnd(name=(p, 'O/NL/var'), group=p, init=1.)
            inds = list(range(m, m + p - 1))
            kernel += variance * EQ().stretch(scales).select(inds)

        # Return model and noise.
        return GP(kernel=kernel, graph=Graph()), \
               vs.bnd(name=(p, 'noise'), group=p, init=noise)

    return model


def construct_gpar(reg, vs, m, p):
    # Check if inducing points are used.
    if reg.x_ind is not None:
        x_ind = vs.get(name='inducing_points', init=reg.x_ind)
    else:
        x_ind = None

    # Construct GPAR model layer by layer.
    gpar = GPAR(replace=reg.replace, impute=reg.impute, x_ind=x_ind)
    for i in range(1, p + 1):
        gpar = gpar.add_layer(model_generator(vs, m, i, **reg.model_config))

    # Return GPAR model.
    return gpar


class GPARRegressor(object):
    """GPAR regressor.
    """

    def __init__(self,
                 replace=True,
                 impute=True,
                 scale=1.0,
                 linear=True,
                 linear_slope=0.1,
                 nonlinear=True,
                 nonlinear_scale=0.1,
                 noise=0.1,
                 greedy=False,
                 x_ind=None):
        # Model configuration.
        self.replace = replace
        self.impute = impute
        self.sparse = x_ind is not None
        self.x_ind = None if x_ind is None else _uprank(x_ind)
        self.greedy = greedy
        self.model_config = {
            'scale': scale,
            'linear': linear,
            'linear_slope': linear_slope,
            'nonlinear': nonlinear,
            'nonlinear_scale': nonlinear_scale,
            'noise': noise
        }

        # Model fitting.
        self.vs = Vars(dtype=torch.float64)
        self.is_fit = False
        self.x = None  # Inputs of training data
        self.y = None  # Outputs of training data
        self.n = None  # Number of data points
        self.m = None  # Number of input features
        self.p = None  # Number of outputs

    def fit(self, x, y, progressive=False):
        # Store data.
        self.x, self.y = _uprank(x), _uprank(y)
        self.n, self.m = self.x.shape
        self.p = self.y.shape[1]

        # Determine extra variables to optimise at every step.
        if self.sparse:
            names = ['inducing_points']
        else:
            names = []

        # Optimise layers by layer or all layers simultaneously.
        if progressive:
            # Check whether to only optimise the last layer.
            only_last = not (self.replace or self.impute or self.sparse)

            # Fit layer by layer.
            for i in range(1, self.p + 1):
                def objective(vs):
                    gpar = construct_gpar(self, vs, self.m, i)
                    return -gpar.logpdf(self.x, self.y,
                                        only_last_layer=only_last)

                minimise_l_bfgs_b(objective,
                                  self.vs,
                                  names=names,
                                  groups=[i],
                                  trace=True)
        else:
            # Fit all layers simultaneously.
            def objective(vs):
                gpar = construct_gpar(self, vs, self.m, self.p)
                return -gpar.logpdf(self.x, self.y, only_last_layer=False)

            minimise_l_bfgs_b(objective,
                              self.vs,
                              names=names,
                              groups=list(range(1, self.p + 1)),
                              trace=True)

        # Store that the model is fit.
        self.is_fit = True

    def sample_prior(self, x, p):
        x = _uprank(x)
        m = x.shape[1]  # Number of input features
        gpar = construct_gpar(self, self.vs, m, p)
        return gpar.sample(x).detach().numpy()

    def sample_posterior(self, x, num_samples=100, latent=True):
        # Check that model is fit.
        if not self.is_fit:
            raise RuntimeError('Must fit model before sampling form the '
                               'posterior.')

        # Construct posterior GPAR.
        gpar = construct_gpar(self, self.vs, self.m, self.p) | (self.x, self.y)

        # Sample from the posterior.
        return [gpar.sample(_uprank(x), latent=latent).detach().numpy()
                for _ in range(num_samples)]

    def predict(self, x, num_samples=100, latent=True, credible_bounds=False):
        # Sample from posterior.
        samples = self.sample_posterior(x,
                                        num_samples=num_samples,
                                        latent=latent)

        # Compute mean.
        mean = np.mean(samples, axis=0)

        if credible_bounds:
            # Also return lower and upper credible bounds if asked for.
            lowers = np.percentile(samples, 2.5, axis=0)
            uppers = np.percentile(samples, 100 - 2.5, axis=0)
            return mean, lowers, uppers
        else:
            return mean


def _uprank(x):
    if np.ndim(x) > 2:
        raise ValueError('Invalid rank {}.'.format(np.ndims(x)))
    while 0 <= np.ndim(x) < 2:
        x = np.expand_dims(x, 1)
    return B.array(x)
