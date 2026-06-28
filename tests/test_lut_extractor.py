import numpy as np
import pytest
from core.color.lut_extractor import extract_lut_from_pair


def test_extract_lut_identity():
    src = np.random.randint(0, 256, (256, 256, 3), dtype=np.uint8)
    tgt = src.copy()
    lut = extract_lut_from_pair(src, tgt, lut_size=17)
    assert lut.shape == (17, 17, 17, 3)
    assert lut.dtype == np.float32
    assert lut.min() >= 0 and lut.max() <= 1
    grid = np.linspace(0, 1, 17)
    for r in range(17):
        for g in range(17):
            for b in range(17):
                diff = np.abs(lut[r, g, b] - np.array([grid[r], grid[g], grid[b]]))
                assert diff.max() < 0.05, f"Identity LUT deviation too large at ({r},{g},{b}): {diff}"


def test_extract_lut_brightness_shift():
    src = np.random.randint(0, 256, (256, 256, 3), dtype=np.uint8)
    tgt = np.clip(src.astype(np.float32) * 0.5, 0, 255).astype(np.uint8)
    lut = extract_lut_from_pair(src, tgt, lut_size=17)
    assert lut.shape == (17, 17, 17, 3)
    assert lut.max() < 0.7, "Brightness-reducing LUT should have max < 0.7"


def test_extract_lut_uint16():
    src = np.random.randint(0, 65536, (128, 128, 3), dtype=np.uint16)
    tgt = src // 2
    lut = extract_lut_from_pair(src, tgt, lut_size=9)
    assert lut.shape == (9, 9, 9, 3)
    assert lut.min() >= 0 and lut.max() <= 1
