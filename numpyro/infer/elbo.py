# Copyright Contributors to the Pyro project.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections import OrderedDict, defaultdict
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any, TypedDict, TypeVar
import warnings

from typing_extensions import TypeAlias

import jax
from jax import eval_shape, random, vmap
from jax.lax import stop_gradient
import jax.numpy as jnp
from jax.scipy.special import logsumexp

from numpyro._typing import Message, ModelT, P, TraceT
from numpyro.distributions import ExpandedDistribution, MaskedDistribution
from numpyro.distributions.kl import kl_divergence
from numpyro.distributions.util import scale_and_mask
from numpyro.handlers import (
    CondIndepStackFrame,
    replay,
    seed,
    substitute,
    trace,
)
from numpyro.infer.util import (
    _without_rsample_stop_gradient,
    compute_log_probs,
    get_importance_trace,
    is_identically_one,
)
from numpyro.ops.provenance import eval_provenance
from numpyro.util import _validate_model, check_model_guide_match, find_stack_level

if TYPE_CHECKING:
    T = TypeVar("T")
    mapT: TypeAlias = Callable[[Callable, T], T]
    MutableStateT: TypeAlias = dict[str, Any]
    LossT: TypeAlias = jax.Array | dict[str, jax.Array]


class LossWithMutableState(TypedDict):
    loss: LossT
    mutable_state: MutableStateT | None


def _apply_vmap(fn: Callable, keys: T) -> T:
    return vmap(fn)(keys)


class ELBO:
    """
    Base class for all ELBO objectives.

    Subclasses should implement either :meth:`loss` or :meth:`loss_with_mutable_state`.

    :param num_particles: The number of particles/samples used to form the ELBO
        (gradient) estimators.
    :param vectorize_particles: Whether to use `jax.vmap` to compute ELBOs over the
        num_particles-many particles in parallel. If False use `jax.lax.map`.
        Defaults to True. You can also pass a callable to specify a custom vectorization
        strategy, for example `jax.pmap`.
    """

    """
    Determines whether the ELBO objective can support inference of discrete latent variables.

    Subclasses that are capable of inferring discrete latent variables should override to `True`.
    """
    can_infer_discrete = False

    def __init__(self, num_particles: int = 1, vectorize_particles: bool | mapT = True):
        self.num_particles = num_particles
        self.vectorize_particles = vectorize_particles
        self.vectorize_particles_fn = self._assign_vectorize_particles_fn(
            vectorize_particles
        )

    def _assign_vectorize_particles_fn(self, vectorize_particles: bool | mapT) -> mapT:
        """Assigns a vectorization function to self.vectorize_particles_fn."""
        if callable(vectorize_particles):
            return vectorize_particles
        elif vectorize_particles is True:
            return _apply_vmap
        elif vectorize_particles is False:
            return jax.lax.map
        else:
            raise ValueError(
                "`vectorize_particles` needs to be a boolean or a callable."
            )

    def loss(
        self,
        rng_key: jax.Array,
        param_map: dict[str, jax.Array],
        model: ModelT[P],
        guide: ModelT[P],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> LossT:
        """
        Evaluates the ELBO with an estimator that uses num_particles many samples/particles.

        :param jax.random.PRNGKey rng_key: random number generator seed.
        :param dict param_map: dictionary of current parameter values keyed by site
            name.
        :param model: Python callable with NumPyro primitives for the model.
        :param guide: Python callable with NumPyro primitives for the guide.
        :param args: arguments to the model / guide (these can possibly vary during
            the course of fitting).
        :param kwargs: keyword arguments to the model / guide (these can possibly vary
            during the course of fitting).
        :return: negative of the Evidence Lower Bound (ELBO) to be minimized.
        """
        return self.loss_with_mutable_state(
            rng_key, param_map, model, guide, *args, **kwargs
        )["loss"]

    def loss_with_mutable_state(
        self,
        rng_key: jax.Array,
        param_map: dict[str, jax.Array],
        model: ModelT[P],
        guide: ModelT[P],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> LossWithMutableState:
        """
        Like :meth:`loss` but also update and return the mutable state, which stores the
        values at :func:`~numpyro.mutable` sites.

        :param jax.random.PRNGKey rng_key: random number generator seed.
        :param dict param_map: dictionary of current parameter values keyed by site
            name.
        :param model: Python callable with NumPyro primitives for the model.
        :param guide: Python callable with NumPyro primitives for the guide.
        :param args: arguments to the model / guide (these can possibly vary during
            the course of fitting).
        :param kwargs: keyword arguments to the model / guide (these can possibly vary
            during the course of fitting).
        :return: dictionary containing ELBO loss and the mutable state
        """
        raise NotImplementedError("This ELBO objective does not support mutable state.")


class Trace_ELBO(ELBO):
    """
    A trace implementation of ELBO-based SVI. The estimator is constructed
    along the lines of references [1] and [2]. There are no restrictions on the
    dependency structure of the model or the guide.

    This is the most basic implementation of the Evidence Lower Bound, which is the
    fundamental objective in Variational Inference. This implementation has various
    limitations (for example it only supports random variables with reparameterized
    samplers) but can be used as a template to build more sophisticated loss
    objectives.

    For more details, refer to http://pyro.ai/examples/svi_part_i.html.

    **References:**

    1. *Automated Variational Inference in Probabilistic Programming*,
       David Wingate, Theo Weber
    2. *Black Box Variational Inference*,
       Rajesh Ranganath, Sean Gerrish, David M. Blei

    :param num_particles: The number of particles/samples used to form the ELBO
        (gradient) estimators.
    :param vectorize_particles: Whether to use `jax.vmap` to compute ELBOs over the
        num_particles-many particles in parallel. If False use `jax.lax.map`.
        Defaults to True. You can also pass a callable to specify a custom vectorization
        strategy, for example `jax.pmap`.
    :param multi_sample_guide: Whether to make an assumption that the guide proposes
        multiple samples.
    :param sum_sites: Whether to sum the ELBO contributions from all sites or return the
        contributions as a dictionary keyed by site.
    """

    def __init__(
        self,
        num_particles: int = 1,
        vectorize_particles: bool = True,
        multi_sample_guide: bool = False,
        sum_sites: bool = True,
    ):
        self.multi_sample_guide = multi_sample_guide
        self.sum_sites = sum_sites
        super().__init__(
            num_particles=num_particles, vectorize_particles=vectorize_particles
        )

    def loss_with_mutable_state(
        self,
        rng_key: jax.Array,
        param_map: dict[str, jax.Array],
        model: ModelT[P],
        guide: ModelT[P],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> LossWithMutableState:
        def single_particle_elbo(
            rng_key: jax.Array,
        ) -> tuple[LossT, MutableStateT | None]:
            params = param_map.copy()
            model_seed, guide_seed = random.split(rng_key)
            seeded_guide = seed(guide, guide_seed)
            guide_log_probs, guide_trace = compute_log_probs(
                seeded_guide, args, kwargs, param_map
            )
            mutable_params = {
                name: site["value"]
                for name, site in guide_trace.items()
                if site["type"] == "mutable"
            }
            params.update(mutable_params)
            if self.multi_sample_guide:
                plates = {
                    name: site["value"]
                    for name, site in guide_trace.items()
                    if site["type"] == "plate"
                }

                def compute_model_log_probs(
                    key: jax.Array, latent: dict[str, jax.Array]
                ) -> dict[str, jax.Array]:
                    with seed(rng_seed=key), substitute(data={**latent, **plates}):
                        model_log_probs, model_trace = compute_log_probs(
                            model, args, kwargs, params
                        )
                    _validate_model(model_trace, plate_warning="loose")
                    return model_log_probs

                num_guide_samples = None
                for site in guide_trace.values():
                    if site["type"] == "sample":
                        num_guide_samples = site["value"].shape[0]
                        break
                if num_guide_samples is None:
                    raise ValueError("guide is missing `sample` sites.")
                seeds = random.split(model_seed, num_guide_samples)
                latents = {
                    name: site["value"]
                    for name, site in guide_trace.items()
                    if (site["type"] == "sample" and site["value"].size > 0)
                    or (site["type"] == "deterministic")
                }
                model_log_probs = vmap(compute_model_log_probs)(seeds, latents)
                model_log_probs = jax.tree.map(
                    lambda x: jnp.sum(x, axis=0), model_log_probs
                )
            else:
                seeded_model = seed(model, model_seed)
                replay_model = replay(seeded_model, guide_trace)
                model_log_probs, model_trace = compute_log_probs(
                    replay_model, args, kwargs, params
                )
                check_model_guide_match(model_trace, guide_trace)
                _validate_model(model_trace, plate_warning="loose")
                mutable_params.update(
                    {
                        name: site["value"]
                        for name, site in model_trace.items()
                        if site["type"] == "mutable"
                    }
                )

            # log p(z) - log q(z). We cannot use jax.tree.map(jnp.subtract, ...) because
            # there may be observed sites in `model_log_probs` that are not in
            # `guide_log_probs` and vice versa.
            union = set(model_log_probs).union(guide_log_probs)
            _elbo_particle = {
                name: model_log_probs.get(name, jnp.array(0.0))
                - guide_log_probs.get(name, jnp.array(0.0))
                for name in union
            }
            elbo_particle: LossT
            if self.sum_sites:
                elbo_particle = sum(_elbo_particle.values(), start=jnp.array(0.0))
            else:
                elbo_particle = _elbo_particle

            if mutable_params:
                if self.num_particles == 1:
                    return elbo_particle, mutable_params
                warnings.warn(
                    "mutable state is currently ignored when num_particles > 1."
                )
            return elbo_particle, None

        # Return (-elbo) since by convention we do gradient descent on a loss and
        # the ELBO is a lower bound that needs to be maximized.
        if self.num_particles == 1:
            elbo, mutable_state = single_particle_elbo(rng_key)
            return {
                "loss": jax.tree.map(jnp.negative, elbo),
                "mutable_state": mutable_state,
            }
        else:
            rng_keys = random.split(rng_key, self.num_particles)
            elbos, mutable_state = self.vectorize_particles_fn(
                single_particle_elbo, rng_keys
            )
            return {
                "loss": jax.tree.map(lambda x: -jnp.mean(x), elbos),
                "mutable_state": mutable_state,
            }


def _get_log_prob_sum(site: dict[str, Any]) -> jax.Array:
    if site["intermediates"]:
        log_prob = site["fn"].log_prob(site["value"], site["intermediates"])
    else:
        log_prob = site["fn"].log_prob(site["value"])
    log_prob = scale_and_mask(log_prob, site["scale"])
    return jnp.sum(log_prob)


def _check_mean_field_requirement(model_trace: TraceT, guide_trace: TraceT) -> None:
    """
    Checks that the guide and model sample sites are ordered identically.
    This is sufficient but not necessary for correctness.
    """
    model_sites = [
        name
        for name, site in model_trace.items()
        if site["type"] == "sample" and name in guide_trace
    ]
    guide_sites = [
        name
        for name, site in guide_trace.items()
        if site["type"] == "sample" and name in model_trace
    ]
    assert set(model_sites) == set(guide_sites)
    if model_sites != guide_sites:
        warnings.warn(
            "Failed to verify mean field restriction on the guide. "
            "To eliminate this warning, ensure model and guide sites "
            "occur in the same order.\n"
            + "Model sites:\n  "
            + "\n  ".join(model_sites)
            + "\nGuide sites:\n  "
            + "\n  ".join(guide_sites),
            stacklevel=find_stack_level(),
        )


class TraceMeanField_ELBO(ELBO):
    """
    A trace implementation of ELBO-based SVI. This is currently the only
    ELBO estimator in NumPyro that uses analytic KL divergences when those
    are available.

    :param num_particles: The number of particles/samples used to form the ELBO
        (gradient) estimators.
    :param vectorize_particles: Whether to use `jax.vmap` to compute ELBOs over the
        num_particles-many particles in parallel. If False use `jax.lax.map`.
        Defaults to True. You can also pass a callable to specify a custom vectorization
        strategy, for example `jax.pmap`.
    :param sum_sites: Whether to sum the ELBO contributions from all sites or return the
        contributions as a dictionary keyed by site.

    .. warning:: This estimator may give incorrect results if the mean-field
        condition is not satisfied.
        The mean field condition is a sufficient but not necessary condition for
        this estimator to be correct. The precise condition is that for every
        latent variable `z` in the guide, its parents in the model must not include
        any latent variables that are descendants of `z` in the guide. Here
        'parents in the model' and 'descendants in the guide' is with respect
        to the corresponding (statistical) dependency structure. For example, this
        condition is always satisfied if the model and guide have identical
        dependency structures.
    """

    def __init__(
        self,
        num_particles: int = 1,
        vectorize_particles: bool = True,
        sum_sites: bool = True,
    ) -> None:
        self.sum_sites = sum_sites
        super().__init__(num_particles, vectorize_particles)

    def loss_with_mutable_state(
        self,
        rng_key: jax.Array,
        param_map: dict[str, jax.Array],
        model: ModelT[P],
        guide: ModelT[P],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> LossWithMutableState:
        def single_particle_elbo(
            rng_key: jax.Array,
        ) -> tuple[LossT, MutableStateT | None]:
            params = param_map.copy()
            model_seed, guide_seed = random.split(rng_key)
            seeded_model = seed(model, model_seed)
            seeded_guide = seed(guide, guide_seed)
            subs_guide = substitute(seeded_guide, data=param_map)
            guide_trace = trace(subs_guide).get_trace(*args, **kwargs)
            mutable_params = {
                name: site["value"]
                for name, site in guide_trace.items()
                if site["type"] == "mutable"
            }
            params.update(mutable_params)
            subs_model = substitute(replay(seeded_model, guide_trace), data=params)
            model_trace = trace(subs_model).get_trace(*args, **kwargs)
            mutable_params.update(
                {
                    name: site["value"]
                    for name, site in model_trace.items()
                    if site["type"] == "mutable"
                }
            )
            check_model_guide_match(model_trace, guide_trace)
            _validate_model(model_trace, plate_warning="loose")
            _check_mean_field_requirement(model_trace, guide_trace)

            _elbo_particle = {}
            for name, model_site in model_trace.items():
                if model_site["type"] == "sample":
                    if model_site["is_observed"]:
                        _elbo_particle[name] = _get_log_prob_sum(model_site)
                    else:
                        guide_site = guide_trace[name]
                        try:
                            kl_qp = kl_divergence(guide_site["fn"], model_site["fn"])
                            kl_qp = scale_and_mask(kl_qp, scale=guide_site["scale"])
                            _elbo_particle[name] = -jnp.sum(kl_qp)
                        except NotImplementedError:
                            _elbo_particle[name] = _get_log_prob_sum(
                                model_site
                            ) - _get_log_prob_sum(guide_site)

            # handle auxiliary sites in the guide
            for name, site in guide_trace.items():
                if site["type"] == "sample" and name not in model_trace:
                    assert site["infer"].get("is_auxiliary") or site["is_observed"]
                    _elbo_particle[name] = -_get_log_prob_sum(site)

            elbo_particle: LossT
            if self.sum_sites:
                elbo_particle = sum(_elbo_particle.values(), start=jnp.array(0.0))
            else:
                elbo_particle = _elbo_particle

            if mutable_params:
                if self.num_particles == 1:
                    return elbo_particle, mutable_params
                warnings.warn(
                    "mutable state is currently ignored when num_particles > 1."
                )
            return elbo_particle, None

        if self.num_particles == 1:
            elbo, mutable_state = single_particle_elbo(rng_key)
            return {
                "loss": jax.tree.map(jnp.negative, elbo),
                "mutable_state": mutable_state,
            }
        else:
            rng_keys = random.split(rng_key, self.num_particles)
            elbos, mutable_state = self.vectorize_particles_fn(
                single_particle_elbo, rng_keys
            )
            return {
                "loss": jax.tree.map(lambda x: -jnp.mean(x), elbos),
                "mutable_state": mutable_state,
            }


class RenyiELBO(ELBO):
    r"""
    An implementation of Renyi's :math:`\alpha`-divergence
    variational inference following reference [1].
    In order for the objective to be a strict lower bound, we require
    :math:`\alpha \ge 0`. Note, however, that according to reference [1], depending
    on the dataset :math:`\alpha < 0` might give better results. In the special case
    :math:`\alpha = 0`, the objective function is that of the important weighted
    autoencoder derived in reference [2].

    .. note:: Setting :math:`\alpha < 1` gives a better bound than the usual ELBO.

    :param float alpha: The order of :math:`\alpha`-divergence.
        Here :math:`\alpha \neq 1`. Default is 0.
    :param num_particles: The number of particles/samples
        used to form the objective (gradient) estimator. Default is 2.
    :param vectorize_particles: Whether to use `jax.vmap` to compute ELBOs over the
        num_particles-many particles in parallel. If False use `jax.lax.map`.
        Defaults to True. You can also pass a callable to specify a custom vectorization
        strategy, for example `jax.pmap`.

    Example::

        def model(data):
            with numpyro.plate("batch", 10000, subsample_size=100):
                latent = numpyro.sample("latent", dist.Normal(0, 1))
                batch = numpyro.subsample(data, event_dim=0)
                numpyro.sample("data", dist.Bernoulli(logits=latent), obs=batch)

        def guide(data):
            w_loc = numpyro.param("w_loc", 1.)
            w_scale = numpyro.param("w_scale", 1.)
            with numpyro.plate("batch", 10000, subsample_size=100):
                batch = numpyro.subsample(data, event_dim=0)
                loc = w_loc * batch
                scale = jnp.exp(w_scale * batch)
                numpyro.sample("latent", dist.Normal(loc, scale))

        elbo = RenyiELBO(num_particles=10)
        svi = SVI(model, guide, optax.adam(0.1), elbo)


    **References:**

    1. *Renyi Divergence Variational Inference*, Yingzhen Li, Richard E. Turner
    2. *Importance Weighted Autoencoders*, Yuri Burda, Roger Grosse, Ruslan Salakhutdinov
    """

    def __init__(self, alpha: float = 0, num_particles: int = 2) -> None:
        if alpha == 1:
            raise ValueError(
                "The order alpha should not be equal to 1. Please use ELBO class"
                "for the case alpha = 1."
            )
        self.alpha = alpha
        super().__init__(num_particles=num_particles)

    def _single_particle_elbo(
        self,
        model: ModelT[P],
        guide: ModelT[P],
        param_map: dict[str, jax.Array],
        args: tuple[Any],
        kwargs: dict[str, Any],
        rng_key: jax.Array,
    ) -> tuple[jax.Array, float]:
        model_seed, guide_seed = random.split(rng_key)
        seeded_model = seed(model, model_seed)
        seeded_guide = seed(guide, guide_seed)
        model_trace, guide_trace = get_importance_trace(
            seeded_model, seeded_guide, args, kwargs, param_map
        )
        check_model_guide_match(model_trace, guide_trace)
        _validate_model(model_trace, plate_warning="loose")

        site_plates = {
            name: {frame for frame in site["cond_indep_stack"]}
            for name, site in model_trace.items()
            if site["type"] == "sample"
        }
        # We will compute Renyi elbos separately across dimensions
        # defined in indep_plates. Then the final elbo is the sum
        # of those independent elbos.
        if site_plates:
            indep_plates = set.intersection(*site_plates.values())
        else:
            indep_plates = set()
        for frame in set.union(*site_plates.values()):
            if frame not in indep_plates:
                subsample_size = frame.size
                size = model_trace[frame.name]["args"][0]
                if size > subsample_size:
                    raise ValueError(
                        "RenyiELBO only supports subsampling in plates that are common"
                        " to all sample sites, e.g. a data plate that encloses the"
                        " entire model."
                    )

        indep_plate_scale = 1.0
        for frame in indep_plates:
            subsample_size = frame.size
            size = model_trace[frame.name]["args"][0]
            if size > subsample_size:
                indep_plate_scale = indep_plate_scale * size / subsample_size
        indep_plate_dims = {frame.dim for frame in indep_plates}

        log_densities = {}
        for trace_type, tr in {"guide": guide_trace, "model": model_trace}.items():
            log_densities[trace_type] = jnp.array(0.0)
            for site in tr.values():
                if site["type"] != "sample":
                    continue
                log_prob = site["log_prob"]
                squeeze_axes: tuple[int, ...] = ()
                for dim in range(log_prob.ndim):
                    neg_dim = dim - log_prob.ndim
                    if neg_dim in indep_plate_dims:
                        continue
                    log_prob = jnp.sum(log_prob, axis=dim, keepdims=True)
                    squeeze_axes = squeeze_axes + (dim,)
                log_prob = jnp.squeeze(log_prob, squeeze_axes)
                log_densities[trace_type] = log_densities[trace_type] + log_prob

        # log p(z) - log q(z)
        elbo = log_densities["model"] - log_densities["guide"]
        # Log probabilities at indep_plates dimensions are scaled to MC approximate
        # the "full size" log probabilities. Because we want to compute Renyi elbos
        # separately across indep_plates dimensions, we will remove such scale now.
        # We will apply such scale after getting those Renyi elbos.
        return elbo / indep_plate_scale, indep_plate_scale

    def loss(
        self,
        rng_key: jax.Array,
        param_map: dict[str, jax.Array],
        model: ModelT[P],
        guide: ModelT[P],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> jax.Array:
        plate_key, rng_key = random.split(rng_key)
        model = seed(
            model, plate_key, hide_types=["sample", "prng_key", "control_flow"]
        )
        guide = seed(
            guide, plate_key, hide_types=["sample", "prng_key", "control_flow"]
        )
        # would like to use P.args and P.kwargs, but doesn't play nice with partialing out rng_key
        single_particle_elbo = partial(
            self._single_particle_elbo,
            model,
            guide,
            param_map,
            args,  # type: ignore
            kwargs,  # type: ignore
        )

        rng_keys = random.split(rng_key, self.num_particles)
        elbos, common_plate_scale = self.vectorize_particles_fn(
            single_particle_elbo, rng_keys
        )
        assert common_plate_scale.shape == (self.num_particles,)
        assert elbos.shape[0] == self.num_particles
        scaled_elbos = (1.0 - self.alpha) * elbos
        avg_log_exp = logsumexp(scaled_elbos, axis=0) - jnp.log(self.num_particles)
        assert avg_log_exp.shape == elbos.shape[1:]
        weights = jnp.exp(scaled_elbos - avg_log_exp)
        renyi_elbo = avg_log_exp / (1.0 - self.alpha)
        weighted_elbo = (stop_gradient(weights) * elbos).mean(0)
        assert renyi_elbo.shape == elbos.shape[1:]
        assert weighted_elbo.shape == elbos.shape[1:]
        loss = -(stop_gradient(renyi_elbo - weighted_elbo) + weighted_elbo)
        # common_plate_scale should be the same across particles.
        return loss.sum() * common_plate_scale[0]


def _get_plate_stacks(trace: TraceT) -> dict[str, list[CondIndepStackFrame]]:
    """
    This builds a dict mapping site name to a set of plate stacks. Each
    plate stack is a list of :class:`CondIndepStackFrame`s corresponding to
    a :class:`plate`. This information is used by :class:`Trace_ELBO` and
    :class:`TraceGraph_ELBO`.
    """
    return {
        name: [f for f in node["cond_indep_stack"]]
        for name, node in trace.items()
        if node["type"] == "sample"
    }


class MultiFrameTensor(dict):
    """
    A container for sums of Tensors among different :class:`plate` contexts.
    Used in :class:`~numpyro.infer.elbo.TraceGraph_ELBO` to simplify
    downstream cost computation logic.

    Example::

        downstream_cost = MultiFrameTensor()
        for site in downstream_nodes:
            downstream_cost.add((site["cond_indep_stack"], site["log_prob"]))
        downstream_cost.add(*other_costs.items())  # add in bulk
        summed = downstream_cost.sum_to(target_site["cond_indep_stack"])
    """

    def __init__(self, *items: tuple[list[CondIndepStackFrame], jax.Array]) -> None:
        super().__init__()
        self.add(*items)

    def add(self, *items: tuple[list[CondIndepStackFrame], jax.Array]) -> None:
        """
        Add a collection of (cond_indep_stack, tensor) pairs. Keys are
        ``cond_indep_stack``s, i.e. tuples of :class:`CondIndepStackFrame`s.
        Values are :class:`numpy.ndarray`s.
        """
        for cond_indep_stack, value in items:
            frames = frozenset(f for f in cond_indep_stack)
            assert all(f.dim < 0 and -jnp.ndim(value) <= f.dim for f in frames)
            if frames in self:
                self[frames] = self[frames] + value
            else:
                self[frames] = value

    def sum_to(self, target_frames: list[CondIndepStackFrame]) -> jax.Array:
        total = None
        for frames, value in self.items():
            for f in frames:
                if f not in target_frames and jnp.shape(value)[f.dim] != 1:
                    value = value.sum(f.dim, keepdims=True)
            while jnp.shape(value) and jnp.shape(value)[0] == 1:
                value = value.squeeze(0)
            total = value if total is None else total + value
        return jnp.array(0.0) if total is None else total

    def __repr__(self):
        return "%s(%s)" % (
            type(self).__name__,
            ",\n\t".join(["({}, ...)".format(frames) for frames in self]),
        )


def get_importance_log_probs(
    model: ModelT,
    guide: ModelT,
    args: tuple[Any],
    kwargs: dict[str, Any],
    params: dict[str, jax.Array],
) -> tuple[dict[str, jax.Array], dict[str, jax.Array]]:
    """
    Returns log probabilities at each site for the guide and the model that is run against it.
    """
    model_tr, guide_tr = get_importance_trace(model, guide, args, kwargs, params)
    model_log_probs = {
        name: site["log_prob"]
        for name, site in model_tr.items()
        if site["type"] == "sample"
    }
    guide_log_probs = {
        name: site["log_prob"]
        for name, site in guide_tr.items()
        if site["type"] == "sample"
    }
    return model_log_probs, guide_log_probs


def _substitute_nonreparam(
    data: dict[str, jax.Array], msg: Message
) -> jax.Array | None:
    if msg["name"] in data and not msg["fn"].has_rsample:
        value = msg["fn"](*msg["args"], **msg["kwargs"])
        value = 0 * value + data[msg["name"]]
        return value
    else:
        return None


def _get_latents(
    model: ModelT,
    guide: ModelT,
    args: tuple[Any],
    kwargs: dict[str, Any],
    params: dict[str, jax.Array],
) -> dict[str, jax.Array]:
    model = seed(substitute(model, data=params), rng_seed=0)
    guide = seed(substitute(guide, data=params), rng_seed=0)
    guide_tr = trace(guide).get_trace(*args, **kwargs)
    model_tr = trace(replay(model, guide_tr)).get_trace(*args, **kwargs)
    model_tr.update(guide_tr)
    return {
        name: site["value"]
        for name, site in model_tr.items()
        if site["type"] == "sample" and not site.get("is_observed", False)
    }


def get_nonreparam_deps(
    model: ModelT[P],
    guide: ModelT[P],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    param_map: dict[str, jax.Array],
    latents: dict[str, jax.Array] | None = None,
) -> tuple[dict[str, frozenset[str]], dict[str, frozenset[str]]]:
    """Find dependencies on non-reparameterizable sample sites for each cost term in the model and the guide."""
    if latents is None:
        latents = eval_shape(
            partial(_get_latents, model, guide, args, kwargs, param_map)
        )

    def fn(
        **latents: jax.Array,
    ) -> tuple[dict[str, jax.Array], dict[str, jax.Array]]:
        subs_fn = partial(_substitute_nonreparam, latents)
        subs_model = substitute(seed(model, rng_seed=0), substitute_fn=subs_fn)
        subs_guide = substitute(seed(guide, rng_seed=0), substitute_fn=subs_fn)
        return get_importance_log_probs(subs_model, subs_guide, args, kwargs, param_map)

    model_deps, guide_deps = eval_provenance(fn, **latents)
    return model_deps, guide_deps


class TraceGraph_ELBO(ELBO):
    """
    A TraceGraph implementation of ELBO-based SVI. The gradient estimator
    is constructed along the lines of reference [1] specialized to the case
    of the ELBO. It supports arbitrary dependency structure for the model
    and guide.
    Fine-grained conditional dependency information as recorded in the
    trace is used to reduce the variance of the gradient estimator.
    In particular provenance tracking [2] is used to find the ``cost`` terms
    that depend on each non-reparameterizable sample site.

    References

    [1] `Gradient Estimation Using Stochastic Computation Graphs`,
        John Schulman, Nicolas Heess, Theophane Weber, Pieter Abbeel

    [2] `Nonstandard Interpretations of Probabilistic Programs for Efficient Inference`,
        David Wingate, Noah Goodman, Andreas Stuhlmüller, Jeffrey Siskind
    """

    can_infer_discrete = True

    def __init__(
        self, num_particles: int = 1, vectorize_particles: bool = True
    ) -> None:
        super().__init__(
            num_particles=num_particles, vectorize_particles=vectorize_particles
        )

    def loss(
        self,
        rng_key: jax.Array,
        param_map: dict[str, jax.Array],
        model: ModelT[P],
        guide: ModelT[P],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> jax.Array:
        """
        Evaluates the ELBO with an estimator that uses num_particles many samples/particles.

        :param jax.random.PRNGKey rng_key: random number generator seed.
        :param dict param_map: dictionary of current parameter values keyed by site
            name.
        :param model: Python callable with NumPyro primitives for the model.
        :param guide: Python callable with NumPyro primitives for the guide.
        :param args: arguments to the model / guide (these can possibly vary during
            the course of fitting).
        :param kwargs: keyword arguments to the model / guide (these can possibly vary
            during the course of fitting).
        :return: negative of the Evidence Lower Bound (ELBO) to be minimized.
        """

        def single_particle_elbo(rng_key: jax.Array) -> jax.Array:
            model_seed, guide_seed = random.split(rng_key)
            seeded_model = seed(model, model_seed)
            seeded_guide = seed(guide, guide_seed)
            model_trace, guide_trace = get_importance_trace(
                seeded_model, seeded_guide, args, kwargs, param_map
            )
            check_model_guide_match(model_trace, guide_trace)
            _validate_model(model_trace, plate_warning="strict")

            latents = {}
            for name, site in guide_trace.items():
                if site["type"] == "sample" and not site.get("is_observed", False):
                    latents[name] = site["value"]
            model_deps, guide_deps = get_nonreparam_deps(
                model, guide, args, kwargs, param_map, latents=latents
            )

            elbo = jnp.array(0.0)
            # mapping from non-reparameterizable sample sites to cost terms influenced by each of them
            downstream_costs: dict[str, MultiFrameTensor] = defaultdict(
                lambda: MultiFrameTensor()
            )
            for name, site in model_trace.items():
                if site["type"] == "sample":
                    elbo = elbo + jnp.sum(site["log_prob"])
                    # add the log_prob to each non-reparam sample site upstream
                    for key in model_deps[name]:
                        downstream_costs[key].add(
                            (site["cond_indep_stack"], site["log_prob"])
                        )
            for name, site in guide_trace.items():
                if site["type"] == "sample":
                    log_prob_sum = jnp.sum(site["log_prob"])
                    if not site["fn"].has_rsample:
                        log_prob_sum = stop_gradient(log_prob_sum)
                    elbo = elbo - log_prob_sum
                    # add the -log_prob to each non-reparam sample site upstream
                    for key in guide_deps[name]:
                        downstream_costs[key].add(
                            (site["cond_indep_stack"], -site["log_prob"])
                        )

            for node, cost in downstream_costs.items():
                guide_site = guide_trace[node]
                downstream_cost = cost.sum_to(guide_site["cond_indep_stack"])
                surrogate = jnp.sum(
                    guide_site["log_prob"] * stop_gradient(downstream_cost)
                )
                elbo = elbo + surrogate - stop_gradient(surrogate)

            return elbo

        # Return (-elbo) since by convention we do gradient descent on a loss and
        # the ELBO is a lower bound that needs to be maximized.
        if self.num_particles == 1:
            return -single_particle_elbo(rng_key)
        else:
            rng_keys = random.split(rng_key, self.num_particles)
            return -jnp.mean(
                self.vectorize_particles_fn(single_particle_elbo, rng_keys)
            )


def get_importance_trace_enum(
    model: ModelT[P],
    guide: ModelT[P],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    params: dict[str, jax.Array],
    max_plate_nesting: int,
    model_deps: dict[str, frozenset[str]],
    guide_desc: dict[str, frozenset[str]],
) -> tuple[TraceT, TraceT, frozenset[str]]:
    """
    (EXPERIMENTAL) Returns traces from the enumerated guide and the enumerated model that is run against it.
    The returned traces also store the log probability at each site and the log measure for measure vars.
    """
    import funsor
    from numpyro.contrib.funsor import (
        enum,
        plate_to_enum_plate,
        to_funsor,
        trace as _trace,
    )

    with (
        plate_to_enum_plate(),
        enum(
            first_available_dim=(-max_plate_nesting - 1) if max_plate_nesting else None
        ),
    ):
        guide = substitute(guide, data=params)
        with _without_rsample_stop_gradient():
            guide_trace = _trace(guide).get_trace(*args, **kwargs)
        model = substitute(replay(model, guide_trace), data=params)
        model_trace = _trace(model).get_trace(*args, **kwargs)

    sum_vars: frozenset[str] = frozenset()
    for is_model, tr in zip((True, False), (model_trace, guide_trace)):
        for name, site in tr.items():
            if site["type"] == "sample":
                value = site["value"]
                intermediates = site["intermediates"]
                dim_to_name = site["infer"]["dim_to_name"]

                # compute log factor
                if is_model and model_trace[name]["infer"].get("kl") == "analytic":
                    if not model_deps[name].isdisjoint(guide_desc[name]):
                        raise AssertionError(
                            f"Expected that for use of analytic KL computation for the latent variable `{name}` its "
                            "parents in the model do not include any non-reparameterizable latent variables that "
                            f"are descendants of `{name}` in the guide. But found variable(s) "
                            f"{[var for var in (model_deps[name] & guide_desc[name])]} both in the parents of "
                            f"`{name}` in the model and in the descendants of `{name}` in the guide."
                        )
                    if not model_deps[name].isdisjoint(sum_vars):
                        raise AssertionError(
                            f"Expected that for use of analytic KL computation for the latent variable `{name}` its "
                            "parents in the model do not include any model-side enumerated latent variables, but "
                            f"found enumerated variable(s) {[var for var in (model_deps[name] & sum_vars)]}."
                        )
                    if name not in guide_trace:
                        raise AssertionError(
                            f"Expected that for use of analytic KL computation for the latent variable `{name}` it "
                            "must be present both in the model and the guide traces, but not found in the guide trace."
                        )
                    kl_qp = kl_divergence(
                        guide_trace[name]["fn"], model_trace[name]["fn"]
                    )
                    dim_to_name.update(guide_trace[name]["infer"]["dim_to_name"])
                    site["kl"] = to_funsor(
                        kl_qp, output=funsor.Real, dim_to_name=dim_to_name
                    )
                elif not is_model and (model_trace[name].get("kl") is not None):
                    # skip logq computation if analytic kl was computed
                    pass
                else:
                    if intermediates:
                        log_prob = site["fn"].log_prob(value, intermediates)
                    else:
                        log_prob = site["fn"].log_prob(value)
                    site["log_prob"] = to_funsor(
                        log_prob, output=funsor.Real, dim_to_name=dim_to_name
                    )

                # compute log measure
                if not is_model or not (site["is_observed"] or (name in guide_trace)):
                    if is_model:
                        sum_vars |= frozenset([name])
                    # get rid of masking
                    base_fn = site["fn"]
                    batch_shape = base_fn.batch_shape
                    while isinstance(
                        base_fn, (MaskedDistribution, ExpandedDistribution)
                    ):
                        base_fn = base_fn.base_dist
                    base_fn = base_fn.expand(batch_shape)
                    if intermediates:
                        log_measure = base_fn.log_prob(value, intermediates)
                    else:
                        log_measure = base_fn.log_prob(value)
                    # dice factor
                    if not site["infer"].get("enumerate") == "parallel":
                        log_measure = log_measure - funsor.ops.detach(log_measure)
                    site["log_measure"] = to_funsor(
                        log_measure, output=funsor.Real, dim_to_name=dim_to_name
                    )

    return model_trace, guide_trace, sum_vars


def _partition(
    model_sum_deps: dict[str, frozenset[str]], sum_vars: frozenset[str]
) -> list[tuple[frozenset[str], frozenset[str]]]:
    # Construct a bipartite graph between model_sum_deps and the sum_vars
    neighbors: OrderedDict[str, list[str]] = OrderedDict(
        [(t, []) for t in model_sum_deps.keys()]
    )
    for key, deps in model_sum_deps.items():
        for dim in deps:
            if dim in sum_vars:
                neighbors[key].append(dim)
                neighbors.setdefault(dim, []).append(key)

    # Partition the bipartite graph into connected components for contraction.
    components = []
    while neighbors:
        v, pending = neighbors.popitem()
        component = OrderedDict([(v, None)])  # used as an OrderedSet
        for v in pending:
            component[v] = None
        while pending:
            v = pending.pop()
            for v in neighbors.pop(v):
                if v not in component:
                    component[v] = None
                    pending.append(v)

        # Split this connected component into factors and measures.
        # Append only if component_factors is non-empty
        component_factors = frozenset(v for v in component if v not in sum_vars)
        if component_factors:
            component_measures = frozenset(v for v in component if v in sum_vars)
            components.append((component_factors, component_measures))
    return components


def guess_max_plate_nesting(
    model: ModelT[P],
    guide: ModelT[P],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    param_map: dict[str, jax.Array],
) -> int:
    """Guess maximum plate nesting by performing jax shape inference."""
    model_shapes, guide_shapes = eval_shape(
        partial(
            get_importance_log_probs,
            model,
            guide,
            args,
            kwargs,
            param_map,
        )
    )
    ndims = [
        len(site.shape)
        for sites in (model_shapes, guide_shapes)
        for site in sites.values()
    ]
    max_plate_nesting = max(ndims) if ndims else 0
    return max_plate_nesting


class TraceEnum_ELBO(ELBO):
    """
    (EXPERIMENTAL) A TraceEnum implementation of ELBO-based SVI. The gradient estimator
    is constructed along the lines of reference [1] specialized to the case
    of the ELBO. It supports arbitrary dependency structure for the model
    and guide.

    Fine-grained conditional dependency information as recorded in the
    trace is used to reduce the variance of the gradient estimator.
    In particular provenance tracking [2] is used to find the ``cost`` terms
    that depend on each non-reparameterizable sample site.
    Enumerated variables are eliminated using the TVE algorithm for plated
    factor graphs [3].

    .. note:: Currently, the objective does not support AutoContinous guides.
        We recommend users to use AutoNormal guide as an alternative auto solution.

    References

    [1] `Storchastic: A Framework for General Stochastic Automatic Differentiation`,
        Emile van Kriekenc, Jakub M. Tomczak, Annette ten Teije

    [2] `Nonstandard Interpretations of Probabilistic Programs for Efficient Inference`,
        David Wingate, Noah Goodman, Andreas Stuhlmüller, Jeffrey Siskind

    [3] `Tensor Variable Elimination for Plated Factor Graphs`,
        Fritz Obermeyer, Eli Bingham, Martin Jankowiak, Justin Chiu,
        Neeraj Pradhan, Alexander M. Rush, Noah Goodman
    """

    can_infer_discrete = True

    def __init__(
        self,
        num_particles: int = 1,
        # float("inf") serves as a sentinel to use guess_max_plate_nesting
        max_plate_nesting: int = float("inf"),  # type: ignore
        vectorize_particles: bool = True,
    ) -> None:
        self.max_plate_nesting = max_plate_nesting
        super().__init__(
            num_particles=num_particles, vectorize_particles=vectorize_particles
        )

    def loss(
        self,
        rng_key: jax.Array,
        param_map: dict[str, jax.Array],
        model: ModelT[P],
        guide: ModelT[P],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> jax.Array:
        def single_particle_elbo(rng_key: jax.Array) -> jax.Array:
            import funsor
            from numpyro.contrib.funsor import to_data

            model_seed, guide_seed = random.split(rng_key)

            if self.max_plate_nesting == float("inf"):
                seeded_model = seed(model, model_seed)
                seeded_guide = seed(guide, guide_seed)
                # XXX: We can extract abstract latents here such that they
                # can be reused in get_nonreparam_deps below.
                self.max_plate_nesting = guess_max_plate_nesting(
                    seeded_model, seeded_guide, args, kwargs, param_map
                )

            # get dependencies on nonreparametrizable variables
            _model_deps, guide_deps = get_nonreparam_deps(
                model, guide, args, kwargs, param_map
            )
            # get descendants of variables in the guide
            guide_desc: dict[str, frozenset[str]] = defaultdict(frozenset)
            for name, deps in guide_deps.items():
                for d in deps:
                    if name != d:
                        guide_desc[d] |= frozenset([name])

            seeded_model = seed(model, model_seed)
            seeded_guide = seed(guide, guide_seed)
            model_trace, guide_trace, sum_vars = get_importance_trace_enum(
                seeded_model,
                seeded_guide,
                args,
                kwargs,
                param_map,
                self.max_plate_nesting,
                _model_deps,
                guide_desc,
            )

            # TODO: fix the check of model/guide distribution shapes
            # check_model_guide_match(model_trace, guide_trace)
            _validate_model(model_trace, plate_warning="strict")

            model_vars = frozenset(_model_deps)
            model_sum_deps: dict[str, frozenset[str]] = {
                k: v & sum_vars for k, v in _model_deps.items() if k not in sum_vars
            }
            model_deps: dict[str, frozenset[str]] = {
                k: v - sum_vars for k, v in _model_deps.items() if k not in sum_vars
            }

            # gather cost terms
            cost_terms = []
            for group_names, group_sum_vars in _partition(model_sum_deps, sum_vars):
                if not group_sum_vars:
                    # uncontracted logp cost term
                    assert len(group_names) == 1
                    name = next(iter(group_names))
                    if model_trace[name].get("kl") is not None:
                        cost = -model_trace[name]["kl"]
                        scale = model_trace[name]["scale"]
                        assert scale == guide_trace[name]["scale"]
                        deps = (model_deps[name] | guide_deps[name]) - frozenset([name])
                        del guide_deps[name]
                    else:
                        cost = model_trace[name]["log_prob"]
                        scale = model_trace[name]["scale"]
                        deps = model_deps[name]
                else:
                    # compute contracted cost term
                    group_factors = tuple(
                        model_trace[name]["log_prob"] for name in group_names
                    )
                    group_factors += tuple(
                        model_trace[var]["log_measure"] for var in group_sum_vars
                    )
                    group_factor_vars = frozenset().union(
                        *[f.inputs for f in group_factors]
                    )
                    group_plates = group_factor_vars - model_vars
                    outermost_plates = frozenset.intersection(
                        *(frozenset(f.inputs) & group_plates for f in group_factors)
                    )
                    elim_plates = group_plates - outermost_plates
                    with funsor.interpretations.normalize:
                        cost = funsor.sum_product.sum_product(
                            funsor.ops.logaddexp,
                            funsor.ops.add,
                            group_factors,
                            plates=group_plates,
                            eliminate=group_sum_vars | elim_plates,
                        )
                    # TODO: add memoization
                    cost = funsor.optimizer.apply_optimizer(cost)
                    # incorporate the effects of subsampling and handlers.scale through a common scale factor
                    scales_set = set()
                    for name in group_names | group_sum_vars:
                        site_scale = model_trace[name]["scale"]
                        if site_scale is None:
                            site_scale = 1.0
                        if isinstance(site_scale, jnp.ndarray):
                            raise ValueError(
                                "Enumeration only supports scalar handlers.scale"
                            )
                        scales_set.add(float(site_scale))
                    if len(scales_set) != 1:
                        raise ValueError(
                            "Expected all enumerated sample sites to share a common scale, "
                            f"but found {len(scales_set)} different scales."
                        )
                    scale = next(iter(scales_set))
                    # combine deps
                    deps = frozenset().union(
                        *[model_deps[name] for name in group_names]
                    )
                    # check model guide enumeration constraint
                    for key in deps:
                        site = guide_trace[key]
                        if site["infer"].get("enumerate") == "parallel":
                            for p in (
                                frozenset(site["log_measure"].inputs) & elim_plates
                            ):
                                raise ValueError(
                                    "Expected model enumeration to be no more global than guide enumeration, but found "
                                    f"model enumeration sites upstream of guide site '{key}' in plate('{p}')."
                                    "Try converting some model enumeration sites to guide enumeration sites."
                                )
                cost_terms.append((cost, scale, deps))

            for name, deps in guide_deps.items():
                # -logq cost term
                cost = -guide_trace[name]["log_prob"]
                scale = guide_trace[name]["scale"]
                cost_terms.append((cost, scale, deps))

            # compute elbo
            elbo = jnp.array(0.0)
            for cost, scale, deps in cost_terms:
                if deps:
                    dice_factors = tuple(
                        guide_trace[key]["log_measure"] for key in deps
                    )
                    dice_factor_vars = frozenset().union(
                        *[f.inputs for f in dice_factors]
                    )
                    cost_vars = frozenset(cost.inputs)
                    with funsor.interpretations.normalize:
                        dice_factor = funsor.sum_product.sum_product(
                            funsor.ops.logaddexp,
                            funsor.ops.add,
                            dice_factors,
                            plates=(dice_factor_vars | cost_vars) - model_vars,
                            eliminate=dice_factor_vars - cost_vars,
                        )
                    # TODO: add memoization
                    dice_factor = funsor.optimizer.apply_optimizer(dice_factor)
                    cost = cost * funsor.ops.exp(dice_factor)
                if (scale is not None) and (not is_identically_one(scale)):
                    cost = cost * scale

                elbo = elbo + cost.reduce(funsor.ops.add)

            return to_data(elbo)

        # Return (-elbo) since by convention we do gradient descent on a loss and
        # the ELBO is a lower bound that needs to be maximized.
        if self.num_particles == 1:
            return -single_particle_elbo(rng_key)
        else:
            rng_keys = random.split(rng_key, self.num_particles)
            return -jnp.mean(
                self.vectorize_particles_fn(single_particle_elbo, rng_keys)
            )
