# Copyright Contributors to the Pyro project.
# SPDX-License-Identifier: Apache-2.0

"""
Optimizer classes defined here are light wrappers over the corresponding optimizers
sourced from :mod:`jax.example_libraries.optimizers` with an interface that is better
suited for working with NumPyro inference algorithms.
"""

from collections import namedtuple
from collections.abc import Callable
from typing import Any, Literal, Optional, Protocol

import jax
from jax import jacfwd, lax, value_and_grad
from jax.example_libraries import optimizers
from jax.flatten_util import ravel_pytree
import jax.numpy as jnp
from jax.scipy.optimize import minimize
from jax.tree_util import register_pytree_node
from jax.typing import ArrayLike

__all__ = [
    "Adam",
    "Adagrad",
    "ClippedAdam",
    "Minimize",
    "Momentum",
    "RMSProp",
    "RMSPropMomentum",
    "SGD",
    "SM3",
]

_Params = Any
_OptState = Any
_IterOptState = tuple[ArrayLike, _OptState]


def _value_and_grad(f, x, forward_mode_differentiation: bool = False) -> tuple:  # noqa: ANN001
    if forward_mode_differentiation:

        def _wrapper(x):  # noqa: ANN001, ANN202
            out, aux = f(x)
            return out, (out, aux)

        grads, (out, aux) = jacfwd(_wrapper, has_aux=True)(x)
        return (out, aux), grads
    else:
        return value_and_grad(f, has_aux=True)(x)


class UpdateExtraArgsFn(Protocol):
    """An update function accepting additional keyword arguments."""

    def __call__(
        self,
        arr: ArrayLike,
        params: _Params,
        state: _OptState,
        **extra_args,
    ) -> _OptState:
        """
        Based on https://github.com/google-deepmind/optax/blob/2e66ce897e83b4901d37dcbb477a7432497848d6/optax/_src/base.py#L110-L147,
        this protocol expresses an update function that *may* take extra arguments.
        """


class _NumPyroOptim(object):
    def __init__(self, optim_fn: Callable, *args, **kwargs) -> None:
        self.init_fn: Callable[[_Params], _IterOptState]
        self.update_fn: UpdateExtraArgsFn
        self.get_params_fn: Callable[[_OptState], _Params]
        self.update_with_value: bool = kwargs.pop("update_with_value", False)
        self.init_fn, self.update_fn, self.get_params_fn = optim_fn(*args, **kwargs)

    def init(self, params: _Params) -> _IterOptState:
        """
        Initialize the optimizer with parameters designated to be optimized.

        :param params: a collection of numpy arrays.
        :return: initial optimizer state.
        """
        opt_state = self.init_fn(params)
        return jnp.array(0), opt_state

    def update(
        self, g: _Params, state: _IterOptState, value: Optional[ArrayLike] = None
    ) -> _IterOptState:
        """
        Gradient update for the optimizer.

        :param g: gradient information for parameters.
        :param state: current optimizer state.
        :return: new optimizer state after the update.
        """
        i, opt_state = state
        if self.update_with_value:
            assert value is not None
            opt_state = self.update_fn(i, g, opt_state, value=value)
        else:
            opt_state = self.update_fn(i, g, opt_state)
        return i + 1, opt_state

    def eval_and_update(
        self,
        fn: Callable[[Any], tuple],
        state: _IterOptState,
        forward_mode_differentiation: bool = False,
    ) -> tuple[tuple[Any, Any], _IterOptState]:
        """
        Performs an optimization step for the objective function `fn`.
        For most optimizers, the update is performed based on the gradient
        of the objective function w.r.t. the current state. However, for
        some optimizers such as :class:`Minimize`, the update is performed
        by reevaluating the function multiple times to get optimal
        parameters.

        :param fn: an objective function returning a pair where the first item
            is a scalar loss function to be differentiated and the second item
            is an auxiliary output.
        :param state: current optimizer state.
        :param forward_mode_differentiation: boolean flag indicating whether to use forward mode differentiation.
        :return: a pair of the output of objective function and the new optimizer state.
        """
        params: _Params = self.get_params(state)
        (out, aux), grads = _value_and_grad(
            fn, x=params, forward_mode_differentiation=forward_mode_differentiation
        )
        return (out, aux), self.update(grads, state, value=out)

    def eval_and_stable_update(
        self,
        fn: Callable[[Any], tuple],
        state: _IterOptState,
        forward_mode_differentiation: bool = False,
    ) -> tuple[tuple[Any, Any], _IterOptState]:
        """
        Like :meth:`eval_and_update` but when the value of the objective function
        or the gradients are not finite, we will not update the input `state`
        and will set the objective output to `nan`.

        :param fn: objective function.
        :param state: current optimizer state.
        :param forward_mode_differentiation: boolean flag indicating whether to use forward mode differentiation.
        :return: a pair of the output of objective function and the new optimizer state.
        """
        params: _Params = self.get_params(state)
        (out, aux), grads = _value_and_grad(
            fn, x=params, forward_mode_differentiation=forward_mode_differentiation
        )
        out, state = lax.cond(
            jnp.isfinite(out) & jnp.isfinite(ravel_pytree(grads)[0]).all(),
            lambda _: (out, self.update(grads, state, value=out)),
            lambda _: (jnp.nan, state),
            None,
        )
        return (out, aux), state

    def get_params(self, state: _IterOptState) -> _Params:
        """
        Get current parameter values.

        :param state: current optimizer state.
        :return: collection with current value for parameters.
        """
        _, opt_state = state
        return self.get_params_fn(opt_state)


def _add_doc(fn) -> Callable[[Any], Any]:  # noqa: ANN001
    def _wrapped(cls):  # noqa: ANN001, ANN202
        cls.__doc__ = "Wrapper class for the JAX optimizer: :func:`~jax.example_libraries.optimizers.{}`".format(
            fn.__name__
        )
        return cls

    return _wrapped


@_add_doc(optimizers.adam)
class Adam(_NumPyroOptim):
    def __init__(self, *args, **kwargs) -> None:
        super(Adam, self).__init__(optimizers.adam, *args, **kwargs)


class ClippedAdam(_NumPyroOptim):
    """
    :class:`~numpyro.optim.Adam` optimizer with gradient clipping.

    :param float clip_norm: All gradient values will be clipped between
        `[-clip_norm, clip_norm]`.

    **Reference:**

    `A Method for Stochastic Optimization`, Diederik P. Kingma, Jimmy Ba
    https://arxiv.org/abs/1412.6980
    """

    def __init__(self, *args, clip_norm: float = 10.0, **kwargs) -> None:
        self.clip_norm = clip_norm
        super(ClippedAdam, self).__init__(optimizers.adam, *args, **kwargs)

    def update(
        self, g: _Params, state: _IterOptState, value: Optional[ArrayLike] = None
    ) -> _IterOptState:
        i, opt_state = state
        # clip norm
        g = jax.tree.map(lambda g_: jnp.clip(g_, -self.clip_norm, self.clip_norm), g)
        opt_state = self.update_fn(i, g, opt_state)
        return i + 1, opt_state


@_add_doc(optimizers.adagrad)
class Adagrad(_NumPyroOptim):
    def __init__(self, *args, **kwargs) -> None:
        super(Adagrad, self).__init__(optimizers.adagrad, *args, **kwargs)


@_add_doc(optimizers.momentum)
class Momentum(_NumPyroOptim):
    def __init__(self, *args, **kwargs) -> None:
        super(Momentum, self).__init__(optimizers.momentum, *args, **kwargs)


@_add_doc(optimizers.rmsprop)
class RMSProp(_NumPyroOptim):
    def __init__(self, *args, **kwargs) -> None:
        super(RMSProp, self).__init__(optimizers.rmsprop, *args, **kwargs)


@_add_doc(optimizers.rmsprop_momentum)
class RMSPropMomentum(_NumPyroOptim):
    def __init__(self, *args, **kwargs) -> None:
        super(RMSPropMomentum, self).__init__(
            optimizers.rmsprop_momentum, *args, **kwargs
        )


@_add_doc(optimizers.sgd)
class SGD(_NumPyroOptim):
    def __init__(self, *args, **kwargs) -> None:
        super(SGD, self).__init__(optimizers.sgd, *args, **kwargs)


@_add_doc(optimizers.sm3)
class SM3(_NumPyroOptim):
    def __init__(self, *args, **kwargs) -> None:
        super(SM3, self).__init__(optimizers.sm3, *args, **kwargs)


# TODO: currently, jax.scipy.optimize.minimize only supports 1D input,
# so we need to add the following mechanism to transform params to flat_params
# and pass `unravel_fn` around.
# When arbitrary pytree is supported in JAX, we can just simply use
# identity functions for `init_fn` and `get_params`.
class _MinimizeState(namedtuple("_MinimizeState", ["flat_params", "unravel_fn"])):
    flat_params: ArrayLike
    unravel_fn: Callable[[ArrayLike], _Params]


register_pytree_node(
    _MinimizeState,
    lambda state: ((state.flat_params,), (state.unravel_fn,)),
    lambda data, xs: _MinimizeState(xs[0], data[0]),
)


def _minimize_wrapper() -> tuple[
    Callable[[_Params], _MinimizeState],
    Callable[[Any, Any, _MinimizeState], _MinimizeState],
    Callable[[_MinimizeState], _Params],
]:
    def init_fn(params: _Params) -> _MinimizeState:
        flat_params, unravel_fn = ravel_pytree(params)
        return _MinimizeState(flat_params, unravel_fn)

    def update_fn(
        i: ArrayLike, grad_tree: ArrayLike, opt_state: _MinimizeState
    ) -> _MinimizeState:
        # we don't use update_fn in Minimize, so let it do nothing
        return opt_state

    def get_params(opt_state: _MinimizeState) -> _Params:
        flat_params, unravel_fn = opt_state
        return unravel_fn(flat_params)

    return init_fn, update_fn, get_params


class Minimize(_NumPyroOptim):
    """
    Wrapper class for the JAX minimizer: :func:`~jax.scipy.optimize.minimize`.

    .. warnings: This optimizer is intended to be used with static guides such
        as empty guides (maximum likelihood estimate), delta guides (MAP estimate),
        or :class:`~numpyro.infer.autoguide.AutoLaplaceApproximation`.
        Using this in stochastic setting is either expensive or hard to converge.

    **Example:**

    .. doctest::

        >>> from numpy.testing import assert_allclose
        >>> from jax import random
        >>> import jax.numpy as jnp
        >>> import numpyro
        >>> import numpyro.distributions as dist
        >>> from numpyro.infer import SVI, Trace_ELBO
        >>> from numpyro.infer.autoguide import AutoLaplaceApproximation

        >>> def model(x, y):
        ...     a = numpyro.sample("a", dist.Normal(0, 1))
        ...     b = numpyro.sample("b", dist.Normal(0, 1))
        ...     with numpyro.plate("N", y.shape[0]):
        ...         numpyro.sample("obs", dist.Normal(a + b * x, 0.1), obs=y)

        >>> x = jnp.linspace(0, 10, 100)
        >>> y = 3 * x + 2
        >>> optimizer = numpyro.optim.Minimize()
        >>> guide = AutoLaplaceApproximation(model)
        >>> svi = SVI(model, guide, optimizer, loss=Trace_ELBO())
        >>> init_state = svi.init(random.PRNGKey(0), x, y)
        >>> optimal_state, loss = svi.update(init_state, x, y)
        >>> params = svi.get_params(optimal_state)  # get guide's parameters
        >>> quantiles = guide.quantiles(params, 0.5)  # get means of posterior samples
        >>> assert_allclose(quantiles["a"], 2., atol=1e-3)
        >>> assert_allclose(quantiles["b"], 3., atol=1e-3)
    """

    def __init__(self, method: Literal["BFGS"] = "BFGS", **kwargs) -> None:
        # only BFGS is supported, see https://docs.jax.dev/en/latest/_autosummary/jax.scipy.optimize.minimize.html
        super().__init__(_minimize_wrapper)
        self._method = method
        self._kwargs = kwargs

    def eval_and_update(
        self,
        fn: Callable[[Any], tuple],
        state: _IterOptState,
        forward_mode_differentiation: bool = False,
    ) -> tuple[tuple[Any, None], _IterOptState]:
        i, (flat_params, unravel_fn) = state

        def loss_fn(x):  # noqa: ANN001, ANN202
            x = unravel_fn(x)
            out, aux = fn(x)
            if aux is not None:
                raise ValueError(
                    "Minimize does not support models with mutable states."
                )
            return out

        results = minimize(
            loss_fn, flat_params, (), method=self._method, **self._kwargs
        )
        flat_params, out = results.x, results.fun
        state = (i + 1, _MinimizeState(flat_params, unravel_fn))
        return (out, None), state


def optax_to_numpyro(transformation) -> _NumPyroOptim:  # noqa: ANN001
    """
    This function produces a ``numpyro.optim._NumPyroOptim`` instance from an
    ``optax.GradientTransformation`` so that it can be used with
    ``numpyro.infer.svi.SVI``. It is a lightweight wrapper that recreates the
    ``(init_fn, update_fn, get_params_fn)`` interface defined by
    :mod:`jax.example_libraries.optimizers`.

    :param transformation: An ``optax.GradientTransformation`` instance to wrap.
    :return: An instance of ``numpyro.optim._NumPyroOptim`` wrapping the supplied
        Optax optimizer.
    """
    import optax

    def init_fn(params: _Params) -> tuple[_Params, Any]:
        opt_state = transformation.init(params)
        return params, opt_state

    def update_fn(
        step: ArrayLike,
        grads: ArrayLike,
        state: tuple[_Params, Any],
        value: ArrayLike,
    ) -> tuple[_Params, Any]:
        params, opt_state = state
        updates, opt_state = optax.with_extra_args_support(transformation).update(
            grads, opt_state, params, value=value
        )
        updated_params = optax.apply_updates(params, updates)
        return updated_params, opt_state

    def get_params_fn(state: tuple[_Params, Any]) -> _Params:
        params, _ = state
        return params

    return _NumPyroOptim(
        lambda x, y, z: (x, y, z),
        init_fn,
        update_fn,
        get_params_fn,
        update_with_value=True,
    )
