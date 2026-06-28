import numpy as np
from scipy.spatial import cKDTree
from scipy.interpolate import RBFInterpolator


def _identity_lut(lut_size):
    grid = np.linspace(0, 1, lut_size, dtype=np.float32)
    r_grid, g_grid, b_grid = np.meshgrid(grid, grid, grid, indexing='ij')
    return np.stack([r_grid, g_grid, b_grid], axis=-1).astype(np.float32)


def extract_lut_from_pair(source_img_rgb, target_img_rgb, lut_size=33, max_samples=8000, rbf_max_samples=2500):
    h, w, _ = source_img_rgb.shape

    if source_img_rgb.dtype == np.uint16:
        norm = 65535.0
    else:
        norm = 255.0

    if (
        source_img_rgb.shape == target_img_rgb.shape
        and source_img_rgb.dtype == target_img_rgb.dtype
        and np.array_equal(source_img_rgb, target_img_rgb)
    ):
        return _identity_lut(lut_size)

    step = max(1, min(h, w) // 800)
    src_sampled = source_img_rgb[::step, ::step].reshape(-1, 3).astype(np.float32) / norm
    tgt_sampled = target_img_rgb[::step, ::step].reshape(-1, 3).astype(np.float32) / norm

    grid_coords = np.linspace(0, 1, lut_size)
    r_grid, g_grid, b_grid = np.meshgrid(grid_coords, grid_coords, grid_coords, indexing='ij')
    grid_points = np.stack([r_grid, g_grid, b_grid], axis=-1).reshape(-1, 3)

    if len(src_sampled) > max_samples:
        rng = np.random.default_rng(42)
        idx = rng.choice(len(src_sampled), max_samples, replace=False)
        src_sub = src_sampled[idx]
        tgt_sub = tgt_sampled[idx]
    else:
        src_sub = src_sampled
        tgt_sub = tgt_sampled

    if len(src_sub) <= rbf_max_samples:
        try:
            rbf = RBFInterpolator(src_sub, tgt_sub, kernel='thin_plate_spline', smoothing=0.01)
            lut_values = rbf(grid_points)
            lut = lut_values.reshape(lut_size, lut_size, lut_size, 3).astype(np.float32)
            lut = np.clip(lut, 0, 1)
            return lut
        except Exception:
            pass

    tree = cKDTree(src_sub)

    k = min(32, len(src_sub))
    _, indices = tree.query(grid_points, k=k)

    if indices.ndim == 1:
        indices = indices[:, np.newaxis]

    dists = np.linalg.norm(
        grid_points[:, np.newaxis, :] - src_sub[indices],
        axis=2
    )
    sigma = 2.0 / (lut_size - 1)
    weights = np.exp(-dists ** 2 / (2 * sigma ** 2))
    weights_sum = weights.sum(axis=1, keepdims=True)
    weights_sum = np.clip(weights_sum, 1e-10, None)
    weights /= weights_sum

    tgt_values = tgt_sub[indices]
    lut_values = np.einsum('ij,ijk->ik', weights, tgt_values)

    lut = lut_values.reshape(lut_size, lut_size, lut_size, 3).astype(np.float32)
    lut = np.clip(lut, 0, 1)

    return lut


if __name__ == '__main__':
    np.random.seed(42)
    src = np.random.randint(0, 256, (512, 512, 3), dtype=np.uint8)
    tgt = np.clip(src.astype(np.float32) * 0.8 + 30, 0, 255).astype(np.uint8)

    import time
    t0 = time.time()
    lut = extract_lut_from_pair(src, tgt, lut_size=33)
    t1 = time.time()

    print(f"LUT shape: {lut.shape}")
    print(f"LUT dtype: {lut.dtype}")
    print(f"LUT min: {lut.min():.4f}, max: {lut.max():.4f}")
    print(f"Extraction time: {t1 - t0:.3f}s")
    assert lut.shape == (33, 33, 33, 3), f"Wrong shape: {lut.shape}"
    assert lut.dtype == np.float32, f"Wrong dtype: {lut.dtype}"
    assert lut.min() >= 0 and lut.max() <= 1, "LUT values out of range"
    print("All tests passed!")
