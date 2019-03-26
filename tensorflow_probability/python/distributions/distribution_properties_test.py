# Copyright 2018 The TensorFlow Probability Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Property-based testing for TFP distributions."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import functools
import inspect
import os
import traceback

from absl import flags
from absl import logging
import hypothesis as hp
from hypothesis import strategies as hps
from hypothesis.extra import numpy as hpnp
import numpy as np
import six
import tensorflow as tf
import tensorflow_probability as tfp
from tensorflow_probability.python.bijectors import hypothesis_testlib as bijector_hps
from tensorflow.python.framework import test_util  # pylint: disable=g-direct-tensorflow-import

tfd = tfp.distributions

flags.DEFINE_enum('tf_mode', 'graph', ['eager', 'graph'],
                  'TF execution mode to use')

FLAGS = flags.FLAGS


def derandomize_hypothesis():
  # Use --test_env=TFP_DERANDOMIZE_HYPOTHESIS=0 to get random coverage.
  return bool(os.environ.get('TFP_DERANDOMIZE_HYPOTHESIS', 1))


MUTEX_PARAMS = [
    set(['logits', 'probs']),
    set(['rate', 'log_rate']),
    set(['scale', 'scale_tril', 'scale_diag', 'scale_identity_multiplier']),
]

SPECIAL_DISTS = [
    'ConditionalDistribution',
    'ConditionalTransformedDistribution',
    'Distribution',
    'Empirical',
    'Independent',
    'MixtureSameFamily',
    'TransformedDistribution',
]


def instantiable_dists():
  result = {}
  for (dist_name, dist_class) in six.iteritems(tfd.__dict__):
    if (not inspect.isclass(dist_class) or
        not issubclass(dist_class, tfd.Distribution) or
        dist_name in SPECIAL_DISTS):
      continue
    try:
      params_event_ndims = dist_class._params_event_ndims()
    except NotImplementedError:
      logging.warning('Unable to test tfd.%s: %s', dist_name,
                      traceback.format_exc())
      continue
    result[dist_name] = (dist_class, params_event_ndims)

  del result['InverseGamma'][1]['rate']  # deprecated parameter

  # Empirical._params_event_ndims depends on `self.event_ndims`, so we have to
  # explicitly list these entries.
  result['Empirical|event_ndims=0'] = (  #
      functools.partial(tfd.Empirical, event_ndims=0), dict(samples=1))
  result['Empirical|event_ndims=1'] = (  #
      functools.partial(tfd.Empirical, event_ndims=1), dict(samples=2))
  result['Empirical|event_ndims=2'] = (  #
      functools.partial(tfd.Empirical, event_ndims=2), dict(samples=3))

  result['Independent'] = (tfd.Independent, None)
  result['MixtureSameFamily'] = (tfd.MixtureSameFamily, None)
  result['TransformedDistribution'] = (tfd.TransformedDistribution, None)
  return result


# INSTANTIABLE_DISTS is a map from str->(DistClass, params_event_ndims)
INSTANTIABLE_DISTS = instantiable_dists()
del instantiable_dists


# pylint is unable to handle @hps.composite (e.g. complains "No value for
# argument 'batch_shape' in function call"), so disable this lint for the file.

# pylint: disable=no-value-for-parameter


def rank_only_shapes(mindims, maxdims):
  return hps.integers(
      min_value=mindims, max_value=maxdims).map(tf.TensorShape(None).with_rank)


def compute_rank_and_fullsize_reqd(draw, batch_shape, current_batch_shape,
                                   is_last_param):
  """Returns a param rank and a list of bools for full-size-required by axis.

  Args:
    draw: Hypothesis data sampler.
    batch_shape: Target broadcasted batch shape.
    current_batch_shape: Broadcasted batch shape of params selected thus far.
      This is ignored for non-last parameters.
    is_last_param: bool indicator of whether this is the last param (in which
      case, we must achieve the target batch_shape).

  Returns:
    param_batch_rank: Sampled rank for this parameter.
    force_fullsize_dim: `param_batch_rank`-sized list of bool indicating whether
      the corresponding axis of the parameter must be full-sized (True) or is
      allowed to be 1 (i.e., broadcast) (False).
  """
  batch_rank = batch_shape.ndims
  if is_last_param:
    # We must force full size dim on any mismatched axes, and proper rank.
    full_rank_current = tf.broadcast_static_shape(
        current_batch_shape, tf.TensorShape([1] * batch_rank))
    # Identify axes in which the target shape is not yet matched.
    axis_is_mismatched = [
        full_rank_current[i] != batch_shape[i] for i in range(batch_rank)
    ]
    min_rank = batch_rank
    if current_batch_shape.ndims == batch_rank:
      # Current rank might be already correct, but we could have a case like
      # batch_shape=[4,3,2] and current_batch_shape=[4,1,2], in which case
      # we must have at least 2 axes on this param's batch shape.
      min_rank -= (axis_is_mismatched + [True]).index(True)
    param_batch_rank = draw(rank_only_shapes(min_rank, batch_rank)).ndims
    # Get the last param_batch_rank (possibly 0!) items.
    force_fullsize_dim = axis_is_mismatched[batch_rank - param_batch_rank:]
  else:
    # There are remaining params to be drawn, so we will be able to force full
    # size axes on subsequent params.
    param_batch_rank = draw(rank_only_shapes(0, batch_rank)).ndims
    force_fullsize_dim = [False] * param_batch_rank
  return param_batch_rank, force_fullsize_dim


@hps.composite
def broadcasting_shapes(draw, batch_shape, param_names):
  """Draws a set of parameter batch shapes that broadcast to `batch_shape`.

  For each parameter we need to choose its batch rank, and whether or not each
  axis i is 1 or batch_shape[i]. This function chooses a set of shapes that
  have possibly mismatched ranks, and possibly broadcasting axes, with the
  promise that the broadcast of the set of all shapes matches `batch_shape`.

  Args:
    draw: Hypothesis sampler.
    batch_shape: `tf.TensorShape`, the target (fully-defined) batch shape .
    param_names: Iterable of `str`, the parameters whose batch shapes need
      determination.

  Returns:
    param_batch_shapes: `dict` of `str->tf.TensorShape` where the set of
        shapes broadcast to `batch_shape`. The shapes are fully defined.
  """
  batch_rank = batch_shape.ndims
  result = {}
  remaining_params = set(param_names)
  current_batch_shape = tf.TensorShape([])
  while remaining_params:
    next_param = draw(hps.one_of(map(hps.just, remaining_params)))
    remaining_params.remove(next_param)
    param_batch_rank, force_fullsize_dim = compute_rank_and_fullsize_reqd(
        draw,
        batch_shape,
        current_batch_shape,
        is_last_param=not remaining_params)

    # Get the last param_batch_rank (possibly 0!) dimensions.
    param_batch_shape = batch_shape[batch_rank - param_batch_rank:].as_list()
    for i, force_fullsize in enumerate(force_fullsize_dim):
      if not force_fullsize and draw(hps.booleans()):
        # Choose to make this param broadcast against some other param.
        param_batch_shape[i] = 1
    param_batch_shape = tf.TensorShape(param_batch_shape)
    current_batch_shape = tf.broadcast_static_shape(current_batch_shape,
                                                    param_batch_shape)
    result[next_param] = param_batch_shape
  return result


@hps.composite
def valid_slices(draw, batch_shape):
  """Samples a legal (possibly empty) slice for shape batch_shape."""
  # We build up a list of slices in several stages:
  # 1. Choose 0 to batch_rank slices to come before an Ellipsis (...).
  # 2. Decide whether or not to add an Ellipsis; if using, updating the indexing
  #    used (e.g. batch_shape[i]) to identify safe bounds.
  # 3. Choose 0 to [remaining_dims] slices to come last.
  # 4. Decide where to insert between 0 and 4 newaxis slices.
  batch_shape = tf.TensorShape(batch_shape).as_list()
  slices = []
  batch_rank = len(batch_shape)
  arbitrary_slices = hps.tuples(
      hps.one_of(hps.just(None), hps.integers(min_value=-100, max_value=100)),
      hps.one_of(hps.just(None), hps.integers(min_value=-100, max_value=100)),
      hps.one_of(
          hps.just(None),
          hps.integers(min_value=-100, max_value=100).filter(lambda x: x != 0))
  ).map(lambda tup: slice(*tup))

  # 1. Choose 0 to batch_rank slices to come before an Ellipsis (...).
  nslc_before_ellipsis = draw(hps.integers(min_value=0, max_value=batch_rank))
  for i in range(nslc_before_ellipsis):
    slc = draw(
        hps.one_of(
            hps.integers(min_value=0, max_value=batch_shape[i] - 1),
            arbitrary_slices))
    slices.append(slc)
  # 2. Decide whether or not to add an Ellipsis; if using, updating the indexing
  #    used (e.g. batch_shape[i]) to identify safe bounds.
  has_ellipsis = draw(hps.booleans().map(lambda x: (Ellipsis, x)))[1]
  nslc_after_ellipsis = draw(
      hps.integers(min_value=0, max_value=batch_rank - nslc_before_ellipsis))
  if has_ellipsis:
    slices.append(Ellipsis)
    remain_start, remain_end = (batch_rank - nslc_after_ellipsis, batch_rank)
  else:
    remain_start = nslc_before_ellipsis
    remain_end = nslc_before_ellipsis + nslc_after_ellipsis
  # 3. Choose 0 to [remaining_dims] slices to come last.
  for i in range(remain_start, remain_end):
    slc = draw(
        hps.one_of(
            hps.integers(min_value=0, max_value=batch_shape[i] - 1),
            arbitrary_slices))
    slices.append(slc)
  # 4. Decide where to insert between 0 and 4 newaxis slices.
  newaxis_positions = draw(
      hps.lists(hps.integers(min_value=0, max_value=len(slices)), max_size=4))
  for i in sorted(newaxis_positions, reverse=True):
    slices.insert(i, tf.newaxis)
  slices = tuple(slices)
  # Since `d[0]` ==> `d.__getitem__(0)` instead of `d.__getitem__((0,))`;
  # and similarly `d[:3]` ==> `d.__getitem__(slice(None, 3))` instead of
  # `d.__getitem__((slice(None, 3),))`; it is useful to test such scenarios.
  if len(slices) == 1 and draw(hps.booleans()):
    # Sometimes only a single item non-tuple.
    return slices[0]
  return slices


def stringify_slices(slices):
  """Returns a list of strings describing the items in `slices`."""
  pretty_slices = []
  slices = slices if isinstance(slices, tuple) else (slices,)
  for slc in slices:
    if slc == Ellipsis:
      pretty_slices.append('...')
    elif isinstance(slc, slice):
      pretty_slices.append('{}:{}:{}'.format(
          *['' if s is None else s for s in (slc.start, slc.stop, slc.step)]))
    elif isinstance(slc, int) or tf.is_tensor(slc):
      pretty_slices.append(str(slc))
    elif slc is tf.newaxis:
      pretty_slices.append('tf.newaxis')
    else:
      raise ValueError('Unexpected slice type: {}'.format(type(slc)))
  return pretty_slices


@hps.composite
def batch_shapes(draw, min_ndims=0, max_ndims=3, min_lastdimsize=1):
  shape = draw(rank_only_shapes(min_ndims, max_ndims))
  rank = shape.ndims
  if rank > 0:

    def resize_lastdim(x):
      return x[:-1] + (max(x[-1], min_lastdimsize),)

    shape = draw(
        hpnp.array_shapes(min_dims=rank, max_dims=rank).map(resize_lastdim).map(
            tf.TensorShape))
  return shape


def single_param(constraint_fn, param_shape):
  """Draws the value of a single distribution parameter."""
  # TODO(bjp): Allow a wider range of floats.
  # float32s = hps.floats(
  #     np.finfo(np.float32).min / 2, np.finfo(np.float32).max / 2,
  #     allow_nan=False, allow_infinity=False)
  float32s = hps.floats(-200, 200, allow_nan=False, allow_infinity=False)

  def mapper(x):
    result = tf.debugging.assert_all_finite(
        constraint_fn(tf.convert_to_tensor(value=x)),
        message='param non-finite')
    if tf.executing_eagerly():
      # TODO(b/128974935): Eager segfault when Tensors retained by hypothesis?
      return result.numpy()
    return result

  return hpnp.arrays(
      dtype=np.float32, shape=param_shape, elements=float32s).map(mapper)


# TODO(b/128974935): Use hps.composite
# @hps.composite
def broadcasting_params(draw, dist_name, batch_shape, event_dim=None):
  """Draws a dict of parameters which should yield the given batch shape."""
  _, params_event_ndims = INSTANTIABLE_DISTS[dist_name]
  if event_dim is None:
    event_dim = draw(hps.integers(min_value=2, max_value=6))

  remaining_params = set(params_event_ndims.keys())
  params_to_use = []
  while remaining_params:
    param = draw(hps.one_of(map(hps.just, remaining_params)))
    params_to_use.append(param)
    remaining_params.remove(param)
    for mutex_set in MUTEX_PARAMS:
      if param in mutex_set:
        remaining_params -= mutex_set

  param_batch_shapes = draw(broadcasting_shapes(batch_shape, params_to_use))
  params_kwargs = dict()
  for param in params_to_use:
    param_batch_shape = param_batch_shapes[param]
    param_event_rank = params_event_ndims[param]
    params_kwargs[param] = tf.convert_to_tensor(
        value=draw(
            single_param(
                constraint_for(dist_name, param),
                param_batch_shape.as_list() + [event_dim] * param_event_rank)),
        dtype=tf.float32)
  return params_kwargs


# TODO(b/128974935): Use hps.composite
# @hps.composite
def independents(draw, batch_shape=None, event_dim=None):
  reinterpreted_batch_ndims = draw(hps.integers(min_value=0, max_value=2))
  if batch_shape is None:
    batch_shape = draw(batch_shapes(min_ndims=reinterpreted_batch_ndims))
  else:  # This independent adds some batch dims to its underlying distribution.
    batch_shape = batch_shape.concatenate(
        draw(
            batch_shapes(
                min_ndims=reinterpreted_batch_ndims,
                max_ndims=reinterpreted_batch_ndims)))
  underlying, batch_shape = distributions(
      draw,
      batch_shape=batch_shape,
      event_dim=event_dim,
      eligibility_filter=lambda name: name != 'Independent')
  logging.info(
      'underlying distribution: %s; parameters used: %s', underlying,
      [k for k, v in six.iteritems(underlying.parameters) if v is not None])
  return (tfd.Independent(
      underlying,
      reinterpreted_batch_ndims=reinterpreted_batch_ndims,
      validate_args=True),
          batch_shape[:len(batch_shape) - reinterpreted_batch_ndims])


# TODO(b/128974935): Use hps.composite
# @hps.composite
def transformed_distributions(draw, batch_shape=None, event_dim=None):
  bijector = bijector_hps.unconstrained_bijectors(draw)
  logging.info('TD bijector: %s', bijector)
  if batch_shape is None:
    batch_shape = draw(batch_shapes())
  underlying_batch_shape = batch_shape
  batch_shape_arg = None
  if draw(hps.booleans()):
    # Use batch_shape overrides.
    underlying_batch_shape = tf.TensorShape([])  # scalar underlying batch
    batch_shape_arg = batch_shape
  # TODO(b/128974935): Use the composite distributions(..).map(..).filter(..)
  # underlyings = distributions(
  #     batch_shape=underlying_batch_shape, event_dim=event_dim).map(
  #         lambda dist_and_batch_shape: dist_and_batch_shape[0]).filter(
  #         bijector_hps.distribution_filter_for(bijector))
  # to_transform = draw(underlyings)
  to_transform, _ = distributions(
      draw,
      batch_shape=underlying_batch_shape,
      event_dim=event_dim,
      eligibility_filter=lambda name: name != 'TransformedDistribution')
  while not bijector_hps.distribution_filter_for(bijector)(to_transform):
    to_transform, _ = distributions(
        draw, batch_shape=underlying_batch_shape, event_dim=event_dim)

  logging.info(
      'TD underlying distribution: %s; parameters used: %s', to_transform,
      [k for k, v in six.iteritems(to_transform.parameters) if v is not None])
  return (tfd.TransformedDistribution(
      bijector=bijector,
      distribution=to_transform,
      batch_shape=batch_shape_arg,
      validate_args=True), batch_shape)


# TODO(b/128974935): Use hps.composite
# @hps.composite
def mixtures_same_family(draw, batch_shape=None, event_dim=None):
  if batch_shape is None:
    # Ensure the components dist has at least one batch dim (a component dim).
    batch_shape = draw(batch_shapes(min_ndims=1, min_lastdimsize=2))
  else:  # This mixture adds a batch dim to its underlying components dist.
    batch_shape = batch_shape.concatenate(
        draw(batch_shapes(min_ndims=1, max_ndims=1, min_lastdimsize=2)))

  component_dist, _ = distributions(
      draw,
      batch_shape=batch_shape,
      event_dim=event_dim,
      eligibility_filter=lambda name: name != 'MixtureSameFamily')
  logging.info(
      'component distribution: %s; parameters used: %s', component_dist,
      [k for k, v in six.iteritems(component_dist.parameters) if v is not None])
  # scalar or same-shaped categorical?
  mixture_batch_shape = draw(
      hps.one_of(hps.just(batch_shape[:-1]), hps.just(tf.TensorShape([]))))
  mixture_dist, _ = distributions(
      draw,
      dist_name='Categorical',
      batch_shape=mixture_batch_shape,
      event_dim=batch_shape.as_list()[-1])
  logging.info(
      'mixture distribution: %s; parameters used: %s', mixture_dist,
      [k for k, v in six.iteritems(mixture_dist.parameters) if v is not None])
  return (tfd.MixtureSameFamily(
      components_distribution=component_dist,
      mixture_distribution=mixture_dist,
      validate_args=True), batch_shape[:-1])


def assert_shapes_unchanged(target_shaped_dict, possibly_bcast_dict):
  for param, target_param_val in six.iteritems(target_shaped_dict):
    np.testing.assert_array_equal(target_param_val.shape.as_list(),
                                  possibly_bcast_dict[param].shape.as_list())


# TODO(b/128974935): Use hps.composite
# @hps.composite
def distributions(draw,
                  dist_name=None,
                  batch_shape=None,
                  event_dim=None,
                  eligibility_filter=lambda name: True):
  """Samples one a set of supported distributions."""
  if dist_name is None:

    def dist_filter(dist_name):
      if not eligibility_filter(dist_name):
        return False
      dist_cls, _ = INSTANTIABLE_DISTS[dist_name]
      if (tf.executing_eagerly() and
          issubclass(dist_cls, tfd.TransformedDistribution)):
        # TODO(b/128974935): Eager+transformed dist don't play nicely.
        return False
      return True

    dist_name = draw(
        hps.one_of(
            map(hps.just,
                [k for k in INSTANTIABLE_DISTS.keys() if dist_filter(k)])))

  dist_cls, _ = INSTANTIABLE_DISTS[dist_name]
  if dist_name == 'Independent':
    return independents(draw, batch_shape, event_dim)
  if dist_name == 'MixtureSameFamily':
    return mixtures_same_family(draw, batch_shape, event_dim)
  if dist_name == 'TransformedDistribution':
    return transformed_distributions(draw, batch_shape, event_dim)

  if batch_shape is None:
    batch_shape = draw(batch_shapes())

  params_kwargs = broadcasting_params(
      draw, dist_name, batch_shape, event_dim=event_dim)
  params_constrained = constraint_for(dist_name)(params_kwargs)
  assert_shapes_unchanged(params_kwargs, params_constrained)
  params_constrained['validate_args'] = True
  return dist_cls(**params_constrained), batch_shape


def maybe_seed(seed):
  seed = int(seed)  # TODO(b/129287396): drop the int(..)
  return tf.compat.v1.set_random_seed(seed) if tf.executing_eagerly() else seed


@test_util.run_all_in_graph_and_eager_modes
class DistributionSlicingTest(tf.test.TestCase):

  def _test_slicing(self, data, dist, batch_shape):
    slices = data.draw(valid_slices(batch_shape))
    slice_str = 'dist[{}]'.format(', '.join(stringify_slices(slices)))
    logging.info('slice used: %s', slice_str)
    # Make sure the slice string appears in Hypothesis' attempted example log,
    # by drawing and discarding it.
    data.draw(hps.just(slice_str))
    if not slices:  # Nothing further to check.
      return
    sliced_zeros = np.zeros(batch_shape)[slices]
    sliced_dist = dist[slices]
    self.assertAllEqual(sliced_zeros.shape, sliced_dist.batch_shape)

    try:
      seed = data.draw(
          hpnp.arrays(dtype=np.int64, shape=[]).filter(lambda x: x != 0))
      samples = self.evaluate(dist.sample(seed=maybe_seed(seed)))

      if not sliced_zeros.size:
        # TODO(b/128924708): Fix distributions that fail on degenerate empty
        #     shapes, e.g. Multinomial, DirichletMultinomial, ...
        return

      sliced_samples = self.evaluate(sliced_dist.sample(seed=maybe_seed(seed)))
    except NotImplementedError as e:
      # TODO(b/34701635): Binomial needs a sampler.
      if 'sample_n is not implemented: Binomial' in str(e):
        return
      raise
    except tf.errors.UnimplementedError as e:
      if 'Unhandled input dimensions' in str(e) or 'rank not in' in str(e):
        # Some cases can fail with 'Unhandled input dimensions \d+' or
        # 'inputs rank not in [0,6]: \d+'
        return
      raise

    # Come up with the slices for samples (which must also include event dims).
    sample_slices = (
        tuple(slices) if isinstance(slices, collections.Sequence) else
        (slices,))
    if Ellipsis not in sample_slices:
      sample_slices += (Ellipsis,)
    sample_slices += tuple([slice(None)] * dist.event_shape.ndims)

    # Report sub-sliced samples (on which we compare log_prob) to hypothesis.
    data.draw(hps.just(samples[sample_slices]))
    self.assertAllEqual(samples[sample_slices].shape, sliced_samples.shape)
    try:
      try:
        lp = self.evaluate(dist.log_prob(samples))
      except tf.errors.InvalidArgumentError:
        # TODO(b/129271256): d.log_prob(d.sample()) should not fail
        #     validate_args checks.
        # We only tolerate this case for the non-sliced dist.
        return
      sliced_lp = self.evaluate(sliced_dist.log_prob(samples[sample_slices]))
    except tf.errors.UnimplementedError as e:
      if 'Unhandled input dimensions' in str(e) or 'rank not in' in str(e):
        # Some cases can fail with 'Unhandled input dimensions \d+' or
        # 'inputs rank not in [0,6]: \d+'
        return
      raise
    # TODO(b/128708201): Better numerics for Geometric/Beta?
    # Eigen can return quite different results for packet vs non-packet ops.
    # To work around this, we use a much larger rtol for the last 3
    # (assuming packet size 4) elements.
    packetized_lp = lp[slices].reshape(-1)[:-3]
    packetized_sliced_lp = sliced_lp.reshape(-1)[:-3]
    rtol = (0.1 if any(
        x in dist.name for x in ('Geometric', 'Beta', 'Dirichlet')) else 0.02)
    self.assertAllClose(packetized_lp, packetized_sliced_lp, rtol=rtol)
    possibly_nonpacket_lp = lp[slices].reshape(-1)[-3:]
    possibly_nonpacket_sliced_lp = sliced_lp.reshape(-1)[-3:]
    rtol = 0.4
    self.assertAllClose(
        possibly_nonpacket_lp, possibly_nonpacket_sliced_lp, rtol=rtol)

  def _run_test(self, data):
    tf.compat.v1.set_random_seed(  # TODO(b/129287396): drop the int(..)
        int(
            data.draw(
                hpnp.arrays(dtype=np.int64,
                            shape=[]).filter(lambda x: x != 0))))
    # TODO(b/128974935): Avoid passing in data.draw using hps.composite
    # dist, batch_shape = data.draw(distributions())
    dist, batch_shape = distributions(data.draw)
    logging.info(
        'distribution: %s; parameters used: %s', dist,
        [k for k, v in six.iteritems(dist.parameters) if v is not None])
    self.assertAllEqual(batch_shape, dist.batch_shape)

    with self.assertRaisesRegexp(TypeError, 'not iterable'):
      iter(dist)  # __getitem__ magically makes an object iterable.

    self._test_slicing(data, dist, batch_shape)

    # TODO(bjp): Enable sampling and log_prob checks. Currently, too many errors
    #     from out-of-domain samples.
    # self.evaluate(dist.log_prob(dist.sample()))

  @hp.given(hps.data())
  @hp.settings(
      deadline=None,
      suppress_health_check=[hp.HealthCheck.too_slow],
      derandomize=derandomize_hypothesis())
  def testDistributions(self, data):
    if tf.executing_eagerly() != (FLAGS.tf_mode == 'eager'): return
    self._run_test(data)


# Functions used to constrain randomly sampled parameter ndarrays.
# TODO(b/128518790): Eliminate / minimize the fudge factors in here.


def identity_fn(x):
  return x


def softplus_plus_eps(eps=1e-6):
  return lambda x: tf.nn.softplus(x) + eps


def sigmoid_plus_eps(eps=1e-6):
  return lambda x: tf.sigmoid(x) * (1 - eps) + eps


def ensure_high_gt_low(low, high):
  """Returns a value with shape matching `high` and gt broadcastable `low`."""
  new_high = tf.maximum(low + tf.abs(low) * .1 + .1, high)
  reduce_dims = []
  if new_high.shape.ndims > high.shape.ndims:
    reduced_leading_axes = tf.range(new_high.shape.ndims - high.shape.ndims)
    new_high = tf.math.reduce_max(
        input_tensor=new_high, axis=reduced_leading_axes)
  reduce_dims = [
      d for d in range(high.shape.ndims) if high.shape[d] < new_high.shape[d]
  ]
  if reduce_dims:
    new_high = tf.math.reduce_max(
        input_tensor=new_high, axis=reduce_dims, keepdims=True)
  return new_high


def symmetric(x):
  return (x + tf.linalg.transpose(x)) / 2


def positive_definite(x):
  shp = x.shape.as_list()
  psd = (tf.matmul(x, x, transpose_b=True) +
         .1 * tf.linalg.eye(shp[-1], batch_shape=shp[:-2]))
  return symmetric(psd)


def fix_triangular(d):
  peak = ensure_high_gt_low(d['low'], d['peak'])
  high = ensure_high_gt_low(peak, d['high'])
  return dict(d, peak=peak, high=high)


def fix_wishart(d):
  df = d['df']
  scale = d.get('scale', d.get('scale_tril'))
  return dict(d, df=tf.maximum(df, tf.cast(scale.shape[-1], df.dtype)))


CONSTRAINTS = {
    'atol':
        tf.nn.softplus,
    'rtol':
        tf.nn.softplus,
    'concentration':
        softplus_plus_eps(),
    'concentration0':
        softplus_plus_eps(),
    'concentration1':
        softplus_plus_eps(),
    'covariance_matrix':
        positive_definite,
    'df':
        softplus_plus_eps(),
    'Chi2WithAbsDf.df':
        softplus_plus_eps(1),  # does floor(abs(x)) for some reason
    'InverseGaussian.loc':
        softplus_plus_eps(),
    'VonMisesFisher.mean_direction':  # max ndims is 5
        lambda x: tf.nn.l2_normalize(tf.nn.sigmoid(x[..., :5]) + 1e-6, -1),
    'Categorical.probs':
        tf.nn.softmax,
    'ExpRelaxedOneHotCategorical.probs':
        tf.nn.softmax,
    'Multinomial.probs':
        tf.nn.softmax,
    'OneHotCategorical.probs':
        tf.nn.softmax,
    'RelaxedCategorical.probs':
        tf.nn.softmax,
    'Zipf.power':
        softplus_plus_eps(1 + 1e-6),  # strictly > 1
    'Geometric.logits':  # TODO(b/128410109): re-enable down to -50
        lambda x: tf.maximum(x, -16.),  # works around the bug
    'Geometric.probs':
        sigmoid_plus_eps(),
    'Binomial.probs':
        tf.sigmoid,
    'NegativeBinomial.probs':
        tf.sigmoid,
    'Bernoulli.probs':
        tf.sigmoid,
    'RelaxedBernoulli.probs':
        tf.sigmoid,
    'mixing_concentration':
        softplus_plus_eps(),
    'mixing_rate':
        softplus_plus_eps(),
    'rate':
        softplus_plus_eps(),
    'scale':
        softplus_plus_eps(),
    'Wishart.scale':
        positive_definite,
    'scale_diag':
        softplus_plus_eps(),
    'MultivariateNormalDiagWithSoftplusScale.scale_diag':
        lambda x: tf.maximum(x, -87.),  # softplus(-87) ~= 1e-38
    'scale_identity_multiplier':
        softplus_plus_eps(),
    'scale_tril':
        lambda x: tf.linalg.band_part(  # pylint: disable=g-long-lambda
            tfd.matrix_diag_transform(x, softplus_plus_eps()), -1, 0),
    'temperature':
        softplus_plus_eps(),
    'total_count':
        lambda x: tf.floor(tf.sigmoid(x / 100) * 100) + 1,
    'Bernoulli':
        lambda d: dict(d, dtype=tf.float32),
    'LKJ':
        lambda d: dict(d, concentration=d['concentration'] + 1, dimension=3),
    'Triangular':
        fix_triangular,
    'TruncatedNormal':
        lambda d: dict(d, high=ensure_high_gt_low(d['low'], d['high'])),
    'Uniform':
        lambda d: dict(d, high=ensure_high_gt_low(d['low'], d['high'])),
    'Wishart':
        fix_wishart,
    'Zipf':
        lambda d: dict(d, dtype=tf.float32),
}


def constraint_for(dist=None, param=None):
  if param is not None:
    return CONSTRAINTS.get('{}.{}'.format(dist, param),
                           CONSTRAINTS.get(param, identity_fn))
  return CONSTRAINTS.get(dist, identity_fn)


if __name__ == '__main__':
  tf.test.main()
