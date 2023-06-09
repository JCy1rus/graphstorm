"""
    Copyright 2023 Contributors

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.
"""
import os

import numpy as np
from numpy.testing import assert_equal, assert_almost_equal

from graphstorm.gconstruct.transform import (NumericalMinMaxTransform)

def test_fp_transform():
    # test NumericalMinMaxTransform pre-process
    transform = NumericalMinMaxTransform("test", "test")
    feats = np.random.randn(100)

    max_val, min_val = transform.pre_process(feats)["test"]
    max_v = np.amax(feats)
    min_v = np.amin(feats)
    assert len(max_val.shape) == 1
    assert len(min_val.shape) == 1
    assert_equal(max_val[0], max_v)
    assert_equal(min_val[0], min_v)

    feats = np.random.randn(100, 1)
    max_val, min_val = transform.pre_process(feats)["test"]
    max_v = np.amax(feats)
    min_v = np.amin(feats)
    assert len(max_val.shape) == 1
    assert len(min_val.shape) == 1
    assert_equal(max_val[0], max_v)
    assert_equal(min_val[0], min_v)

    feats = np.random.randn(100, 10)
    max_val, min_val = transform.pre_process(feats)["test"]
    assert len(max_val.shape) == 1
    assert len(min_val.shape) == 1
    assert len(max_val) == 10
    assert len(min_val) == 10
    for i in range(10):
        max_v = np.amax(feats[:,i])
        min_v = np.amin(feats[:,i])
        assert_equal(max_val[i], max_v)
        assert_equal(min_val[i], min_v)

    feats = np.random.randn(100)
    feats[0] = 10.
    feats[1] = -10.
    transform = NumericalMinMaxTransform("test", "test", max_bound=5., min_bound=-5.)
    max_val, min_val = transform.pre_process(feats)["test"]
    max_v = np.amax(feats)
    min_v = np.amin(feats)
    assert len(max_val.shape) == 1
    assert len(min_val.shape) == 1
    assert_equal(max_val[0], 5.)
    assert_equal(min_val[0], -5.)

    feats = np.random.randn(100, 1)
    feats[0][0] = 10.
    feats[1][0] = -10.
    transform = NumericalMinMaxTransform("test", "test", max_bound=5., min_bound=-5.)
    max_val, min_val = transform.pre_process(feats)["test"]
    max_v = np.amax(feats)
    min_v = np.amin(feats)
    assert len(max_val.shape) == 1
    assert len(min_val.shape) == 1
    assert_equal(max_val[0], 5.)
    assert_equal(min_val[0], -5.)

    feats = np.random.randn(100, 10)
    feats[0] = 10.
    feats[1] = -10.
    transform = NumericalMinMaxTransform("test", "test", max_bound=5., min_bound=-5.)
    max_val, min_val = transform.pre_process(feats)["test"]
    assert len(max_val.shape) == 1
    assert len(min_val.shape) == 1
    assert len(max_val) == 10
    assert len(min_val) == 10
    for i in range(10):
        max_v = np.amax(feats[:,i])
        min_v = np.amin(feats[:,i])
        assert_equal(max_val[i], 5.)
        assert_equal(min_val[i], -5.)

    # Test collect info
    transform = NumericalMinMaxTransform("test", "test")
    info = [(np.array([1.]), np.array([-1.])),
            (np.array([2.]), np.array([-0.5])),
            (np.array([0.5]), np.array([-0.1]))]
    transform.update_info(info)
    assert len(transform._max_val) == 1
    assert len(transform._min_val) == 1
    assert_equal(transform._max_val[0], 2.)
    assert_equal(transform._min_val[0], -1.)

    info = [(np.array([1., 2., 3.]), np.array([-1., -2., 0.5])),
            (np.array([2., 1., 3.]), np.array([-0.5, -3., 0.1])),
            (np.array([0.5, 3., 1.]), np.array([-0.1, -2., 0.3]))]
    transform.update_info(info)
    assert len(transform._max_val) == 3
    assert len(transform._min_val) == 3
    assert_equal(transform._max_val[0], 2.)
    assert_equal(transform._min_val[0], -1.)

def test_fp_min_max_transform():
    transform = NumericalMinMaxTransform("test", "test")
    max_val = np.array([2.])
    min_val = np.array([-1.])
    transform._max_val = max_val
    transform._min_val = min_val
    feats = np.random.randn(100)
    norm_feats = transform(feats)["test"]
    feats[feats > max_val] = max_val
    feats[feats < min_val] = min_val
    assert_almost_equal(norm_feats, (feats-min_val)/(max_val-min_val), decimal=6)

    feats = np.random.randn(100, 1)
    norm_feats = transform(feats)["test"]
    feats[feats > max_val] = max_val
    feats[feats < min_val] = min_val
    assert_almost_equal(norm_feats, (feats-min_val)/(max_val-min_val), decimal=6)

    transform = NumericalMinMaxTransform("test", "test")
    max_val = np.array([2., 3., 0.])
    min_val = np.array([-1., 1., -0.5])
    transform._max_val = max_val
    transform._min_val = min_val
    feats = np.random.randn(10, 3)
    norm_feats = transform(feats)["test"]
    for i in range(3):
        new_feats = feats[:,i]
        new_feats[new_feats > max_val[i]] = max_val[i]
        new_feats[new_feats < min_val[i]] = min_val[i]
        assert_almost_equal(norm_feats[:,i], (new_feats-min_val[i])/(max_val[i]-min_val[i]), decimal=6)

if __name__ == '__main__':
    test_fp_transform()
    test_fp_min_max_transform()