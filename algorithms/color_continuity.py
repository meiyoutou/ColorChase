import cv2
import numpy as np
from scipy.ndimage import gaussian_filter


def build_transfer_3dlut(target_img, result_img, lut_size=33, sigma=0.8):
    h, w = target_img.shape[:2]
    target_rgb = cv2.cvtColor(target_img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    result_rgb = cv2.cvtColor(result_img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0

    lut_sum = np.zeros((lut_size, lut_size, lut_size, 3), dtype=np.float64)
    lut_count = np.zeros((lut_size, lut_size, lut_size), dtype=np.float64)

    step = max(1, min(h, w) // 512)
    t_flat = target_rgb[::step, ::step].reshape(-1, 3)
    r_flat = result_rgb[::step, ::step].reshape(-1, 3)

    t_idx = np.clip((t_flat * (lut_size - 1)).astype(int), 0, lut_size - 1)

    np.add.at(lut_sum, (t_idx[:, 0], t_idx[:, 1], t_idx[:, 2]), r_flat)
    np.add.at(lut_count, (t_idx[:, 0], t_idx[:, 1], t_idx[:, 2]), 1)

    filled = lut_count > 0
    for ch in range(3):
        channel = lut_sum[:, :, :, ch].copy()
        channel[filled] /= lut_count[filled]

        unfilled_mask = ~filled
        if unfilled_mask.any():
                grid_coords = np.mgrid[0:lut_size, 0:lut_size, 0:lut_size].reshape(3, -1).T.astype(np.float64)
                filled_coords = np.argwhere(filled)
                filled_vals = channel[filled]

                if len(filled_coords) > 0:
                    from scipy.spatial import cKDTree
                    tree = cKDTree(filled_coords)
                    unfilled_coords = np.argwhere(unfilled_mask)
                    _, nearest_idx = tree.query(unfilled_coords, k=min(8, len(filled_coords)))
                    if nearest_idx.ndim == 1:
                        nearest_idx = nearest_idx[:, np.newaxis]
                    weights = np.exp(-np.sum((unfilled_coords[:, np.newaxis, :] - filled_coords[nearest_idx]) ** 2, axis=2) / (2 * (lut_size / 4) ** 2))
                    weights_sum = weights.sum(axis=1, keepdims=True)
                    weights_sum = np.clip(weights_sum, 1e-10, None)
                    weights /= weights_sum
                    channel[unfilled_mask] = (weights * filled_vals[nearest_idx]).sum(axis=1)

        channel = gaussian_filter(channel, sigma=sigma, mode='nearest')

        anchor_mask = filled
        if anchor_mask.any():
            anchor_vals = lut_sum[:, :, :, ch][anchor_mask] / np.maximum(lut_count[anchor_mask], 1)
            blend = 0.85
            channel[anchor_mask] = anchor_vals * blend + channel[anchor_mask] * (1 - blend)

        lut_sum[:, :, :, ch] = channel

    return lut_sum


def apply_3dlut(img, lut):
    h, w = img.shape[:2]
    lut_size = lut.shape[0]

    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0

    r_idx = rgb[:, :, 0] * (lut_size - 1)
    g_idx = rgb[:, :, 1] * (lut_size - 1)
    b_idx = rgb[:, :, 2] * (lut_size - 1)

    r0 = np.floor(r_idx).astype(int)
    g0 = np.floor(g_idx).astype(int)
    b0 = np.floor(b_idx).astype(int)

    r1 = np.minimum(r0 + 1, lut_size - 1)
    g1 = np.minimum(g0 + 1, lut_size - 1)
    b1 = np.minimum(b0 + 1, lut_size - 1)

    rf = (r_idx - r0)[:, :, np.newaxis]
    gf = (g_idx - g0)[:, :, np.newaxis]
    bf = (b_idx - b0)[:, :, np.newaxis]

    c000 = lut[r0, g0, b0]
    c001 = lut[r0, g0, b1]
    c010 = lut[r0, g1, b0]
    c011 = lut[r0, g1, b1]
    c100 = lut[r1, g0, b0]
    c101 = lut[r1, g0, b1]
    c110 = lut[r1, g1, b0]
    c111 = lut[r1, g1, b1]

    c00 = c000 * (1 - bf) + c001 * bf
    c01 = c010 * (1 - bf) + c011 * bf
    c10 = c100 * (1 - bf) + c101 * bf
    c11 = c110 * (1 - bf) + c111 * bf

    c0 = c00 * (1 - gf) + c01 * gf
    c1 = c10 * (1 - gf) + c11 * gf

    result = c0 * (1 - rf) + c1 * rf

    result = np.clip(result * 255, 0, 255).astype(np.uint8)
    return cv2.cvtColor(result, cv2.COLOR_RGB2BGR)


def smooth_transfer_via_3dlut(target_img, result_img, lut_size=33, sigma=0.8, anchor_blend=0.85):
    lut = build_transfer_3dlut(target_img, result_img, lut_size=lut_size, sigma=sigma)
    return apply_3dlut(target_img, lut)


def compute_color_anomaly_map(target_img, result_img, threshold_percentile=95):
    t_lab = cv2.cvtColor(target_img, cv2.COLOR_BGR2LAB).astype(np.float32)
    r_lab = cv2.cvtColor(result_img, cv2.COLOR_BGR2LAB).astype(np.float32)

    delta_a = r_lab[:, :, 1] - t_lab[:, :, 1]
    delta_b = r_lab[:, :, 2] - t_lab[:, :, 2]
    delta_l = r_lab[:, :, 0] - t_lab[:, :, 0]

    delta_e = np.sqrt(delta_l ** 2 + delta_a ** 2 + delta_b ** 2)

    threshold = np.percentile(delta_e, threshold_percentile)

    anomaly = np.clip((delta_e - threshold * 0.7) / (threshold * 0.3 + 1e-6), 0, 1)

    anomaly = cv2.GaussianBlur(anomaly, (5, 5), 1.5)

    return anomaly


def gentle_color_correction(target_img, result_img, reference_img,
                            anomaly_percentile=95,
                            correction_strength=0.3,
                            progress_callback=None):
    def _cb(stage, fraction, message=""):
        if progress_callback is not None:
            progress_callback(stage, fraction, message)

    _cb("lut", 0.1, "构建3D LUT全局映射...")
    smoothed = smooth_transfer_via_3dlut(target_img, result_img, lut_size=33, sigma=0.8)

    _cb("blend", 0.5, "融合平滑映射...")
    result_f = result_img.astype(np.float32)
    smoothed_f = smoothed.astype(np.float32)
    blended = result_f * 0.55 + smoothed_f * 0.45
    blended = np.clip(blended, 0, 255).astype(np.uint8)

    _cb("anomaly", 0.7, "检测色彩异常区域...")
    anomaly_map = compute_color_anomaly_map(target_img, result_img, threshold_percentile=anomaly_percentile)

    _cb("correct", 0.85, "修正异常颜色...")
    mask_3ch = np.stack([anomaly_map] * 3, axis=-1) * correction_strength
    corrected = blended.astype(np.float32) * (1 - mask_3ch) + target_img.astype(np.float32) * mask_3ch
    corrected = np.clip(corrected, 0, 255).astype(np.uint8)

    _cb("done", 1.0, "色彩校正完成")
    return corrected
