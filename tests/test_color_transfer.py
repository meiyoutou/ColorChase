import numpy as np
import pytest
from algorithms.color_transfer import transfer_color


def test_transfer_luminance_basic():
    target = np.random.randint(50, 200, (128, 128, 3), dtype=np.uint8)
    reference = np.random.randint(50, 200, (128, 128, 3), dtype=np.uint8)
    result = transfer_color(target, reference, algorithm="luminance_partition", blend_strength=0.85)
    assert result.shape == target.shape
    assert result.dtype == np.uint8
    assert result.min() >= 0 and result.max() <= 255


def test_transfer_reinhard_basic():
    target = np.random.randint(50, 200, (64, 64, 3), dtype=np.uint8)
    reference = np.random.randint(50, 200, (64, 64, 3), dtype=np.uint8)
    result = transfer_color(target, reference, algorithm="reinhard", blend_strength=1.0)
    assert result.shape == target.shape
    assert result.dtype == np.uint8


def test_transfer_blend_zero():
    target = np.random.randint(50, 200, (64, 64, 3), dtype=np.uint8)
    reference = np.random.randint(50, 200, (64, 64, 3), dtype=np.uint8)
    result = transfer_color(target, reference, algorithm="luminance_partition", blend_strength=0.0)
    diff = np.abs(result.astype(int) - target.astype(int))
    assert diff.max() <= 2, "blend_strength=0 should produce near-identical output"
