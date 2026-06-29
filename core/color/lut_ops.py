import os

import cv2
import numpy as np

from config import BASE_DIR
from core.color.bw_orange_filter import apply_orange_bw_filter


def apply_pro_adjust(original_bgr, stylized_bgr, alpha=1.0, exposure=0.0, contrast=0.0,
                     highlight=0.0, shadow=0.0, vibrance=0.0):
    blended = (original_bgr.astype(np.float32) * (1.0 - alpha) + stylized_bgr.astype(np.float32) * alpha).clip(0, 255)
    lab = cv2.cvtColor(blended.astype(np.uint8), cv2.COLOR_BGR2LAB).astype(np.float32)

    if exposure != 0.0:
        l = lab[:, :, 0] + exposure * 100.0
        lab[:, :, 0] = np.clip(l, 0, 255)

    if contrast != 0.0 or highlight != 0.0 or shadow != 0.0:
        l = lab[:, :, 0] / 255.0
        l_out = l.copy()

        if contrast != 0.0:
            if contrast > 0:
                l_out = l + contrast * 0.8 * (l - 0.5) * (1.0 - (l - 0.5) * (l - 0.5) * 4.0)
            else:
                l_mean = l.mean()
                l_out = l * (1.0 + contrast) + l_mean * (-contrast)

        if highlight != 0.0:
            mask = np.clip((l - 0.5) * 2.0, 0, 1) ** 2
            l_out = l_out + highlight * 0.15 * mask

        if shadow != 0.0:
            mask = np.clip((0.5 - l) * 2.0, 0, 1) ** 2
            l_out = l_out + shadow * 0.15 * mask

        lab[:, :, 0] = np.clip(l_out, 0.0, 1.0) * 255.0

    if vibrance != 0.0:
        scale = 1.0 + vibrance
        a = lab[:, :, 1] - 128.0
        b = lab[:, :, 2] - 128.0
        sat = np.hypot(a, b) + 1e-8
        saturation_ratio = np.clip(1.0 / (sat + 1.0), 0.0, 1.0)
        vib_scale = scale * (1.0 - saturation_ratio) + saturation_ratio
        lab[:, :, 1] = np.clip(a * vib_scale + 128.0, 0, 255)
        lab[:, :, 2] = np.clip(b * vib_scale + 128.0, 0, 255)

    lab = np.clip(lab, 0, 255).astype(np.uint8)
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


def _trilinear_lookup(lut: np.ndarray, values: np.ndarray) -> np.ndarray:
    size = lut.shape[0]
    coords = values * (size - 1)
    coords = np.clip(coords, 0, size - 1)

    x0 = np.floor(coords[:, 0]).astype(np.int32)
    y0 = np.floor(coords[:, 1]).astype(np.int32)
    z0 = np.floor(coords[:, 2]).astype(np.int32)
    x1 = np.clip(x0 + 1, 0, size - 1)
    y1 = np.clip(y0 + 1, 0, size - 1)
    z1 = np.clip(z0 + 1, 0, size - 1)

    dx = (coords[:, 0] - x0).reshape(-1, 1)
    dy = (coords[:, 1] - y0).reshape(-1, 1)
    dz = (coords[:, 2] - z0).reshape(-1, 1)

    c000 = lut[x0, y0, z0]
    c100 = lut[x1, y0, z0]
    c010 = lut[x0, y1, z0]
    c110 = lut[x1, y1, z0]
    c001 = lut[x0, y0, z1]
    c101 = lut[x1, y0, z1]
    c011 = lut[x0, y1, z1]
    c111 = lut[x1, y1, z1]

    c00 = c000 * (1 - dx) + c100 * dx
    c01 = c001 * (1 - dx) + c101 * dx
    c10 = c010 * (1 - dx) + c110 * dx
    c11 = c011 * (1 - dx) + c111 * dx

    c0 = c00 * (1 - dy) + c10 * dy
    c1 = c01 * (1 - dy) + c11 * dy

    return c0 * (1 - dz) + c1 * dz


def _build_identity_lut(size=33):
    lut = np.zeros((size, size, size, 3), dtype=np.float32)
    grid = np.linspace(0, 1, size)
    for i in range(size):
        lut[i, :, :, 0] = grid[i]
        lut[:, i, :, 1] = grid[i]
        lut[:, :, i, 2] = grid[i]
    return lut


def _generate_builtin_profile(name: str) -> np.ndarray:
    size = 33
    grid = np.linspace(0, 1, size)

    if name == "bw":
        lut = np.zeros((size, size, size, 3), dtype=np.float32)
        for i in range(size):
            for j in range(size):
                for k in range(size):
                    gray = 0.299 * grid[i] + 0.587 * grid[j] + 0.114 * grid[k]
                    lut[i, j, k] = [gray, gray, gray]
        return lut
    elif name == "warm":
        lut = np.zeros((size, size, size, 3), dtype=np.float32)
        for i in range(size):
            for j in range(size):
                for k in range(size):
                    r = min(1.0, grid[i] * 1.12)
                    g = grid[j] * 0.92
                    b = grid[k] * 0.78
                    lut[i, j, k] = [r, g, b]
        return lut
    elif name == "cool":
        lut = np.zeros((size, size, size, 3), dtype=np.float32)
        for i in range(size):
            for j in range(size):
                for k in range(size):
                    r = grid[i] * 0.82
                    g = grid[j] * 0.96
                    b = min(1.0, grid[k] * 1.10)
                    lut[i, j, k] = [r, g, b]
        return lut
    elif name == "orange_bw":
        return _generate_orange_bw_lut(size)
    else:
        return _build_identity_lut(size)


def _generate_orange_bw_lut(size=33):
    presets_dir = os.path.join(str(BASE_DIR), "presets")
    os.makedirs(presets_dir, exist_ok=True)
    cache_path = os.path.join(presets_dir, "orange_bw.npy")

    if os.path.exists(cache_path):
        return np.load(cache_path)

    grid = np.linspace(0, 255, size).astype(np.uint8)
    lut = np.zeros((size, size, size, 3), dtype=np.float32)
    patch_size = 8

    for i in range(size):
        r_val = grid[i]
        batch_h = size * size
        img = np.zeros((batch_h * patch_size, patch_size, 3), dtype=np.uint8)
        idx = 0
        for j in range(size):
            g_val = grid[j]
            for k in range(size):
                b_val = grid[k]
                y0 = idx * patch_size
                y1 = y0 + patch_size
                img[y0:y1, :, 2] = r_val
                img[y0:y1, :, 1] = g_val
                img[y0:y1, :, 0] = b_val
                idx += 1

        result = apply_orange_bw_filter(img, strength=1.0).astype(np.float32) / 255.0

        idx = 0
        for j in range(size):
            for k in range(size):
                y0 = idx * patch_size
                y1 = y0 + patch_size
                patch_result = result[y0:y1, :, :]
                mean_color = patch_result.mean(axis=(0, 1))
                lut[i, j, k] = mean_color
                idx += 1

    np.save(cache_path, lut)
    print(f"[Preset] orange_bw LUT cached to {cache_path}")
    return lut
