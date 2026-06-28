import numpy as np


def aces_tone_map(img: np.ndarray, exposure: float = 1.0) -> np.ndarray:
    x = img.astype(np.float32) * exposure
    a, b, c, d, e = 2.51, 0.03, 2.43, 0.59, 0.14
    mapped = (x * (a * x + b)) / (x * (c * x + d) + e)
    return np.clip(mapped, 0, 1)


def filmic_tone_map(img: np.ndarray, black_white: float = 0.0, shadow_strength: float = 0.35) -> np.ndarray:
    x = img.astype(np.float32)
    x = x / (x + 1.0)
    x = np.clip(x, 0, 1)
    return x


def reinhard_tone_map(img: np.ndarray) -> np.ndarray:
    x = img.astype(np.float32)
    mapped = x / (1.0 + x)
    return np.clip(mapped, 0, 1)


def drago_tone_map(img: np.ndarray, bias: float = 0.85) -> np.ndarray:
    x = img.astype(np.float32)
    lw = np.max(x)
    lw_r = x / lw
    mapped = np.log(1.0 + lw_r) / np.log(1.0 + lw) * (lw_r / (1.0 + lw_r)) ** (bias - 1.0)
    return np.clip(mapped, 0, 1)
