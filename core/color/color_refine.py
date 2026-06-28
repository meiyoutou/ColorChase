import cv2
import numpy as np


def refine_color_distribution(result_bgr, reference_bgr, strength=0.7,
                               l_mean_strength=0.8, a_mean_strength=0.0,
                               b_mean_strength=0.8, l_std_strength=0.6,
                               a_std_strength=0.15, b_std_strength=0.3):
    h, w = result_bgr.shape[:2]
    ref_h, ref_w = reference_bgr.shape[:2]

    ref_resized = reference_bgr
    if (ref_h, ref_w) != (h, w):
        ref_resized = cv2.resize(reference_bgr, (w, h), interpolation=cv2.INTER_LINEAR)

    result_lab = cv2.cvtColor(result_bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
    ref_lab = cv2.cvtColor(ref_resized, cv2.COLOR_BGR2LAB).astype(np.float32)

    a_orig = result_lab[:, :, 1].copy()
    b_orig = result_lab[:, :, 2].copy()

    mean_strengths = [l_mean_strength, a_mean_strength, b_mean_strength]
    std_strengths = [l_std_strength, a_std_strength, b_std_strength]

    for c in range(3):
        r_mean = result_lab[:, :, c].mean()
        r_std = result_lab[:, :, c].std() + 1e-6
        ref_mean = ref_lab[:, :, c].mean()
        ref_std = ref_lab[:, :, c].std() + 1e-6

        delta_mean = (ref_mean - r_mean) * mean_strengths[c]

        scale = 1.0 + (ref_std / r_std - 1.0) * std_strengths[c]
        scale = max(scale, 0.5)
        scale = min(scale, 2.0)

        result_lab[:, :, c] = (result_lab[:, :, c] - r_mean) * scale + r_mean + delta_mean

    a_min = a_orig.min()
    a_max = a_orig.max()
    b_min = b_orig.min()
    b_max = b_orig.max()

    a_clip_low = a_min - 15.0
    a_clip_high = a_max + 15.0
    b_clip_low = b_min - 25.0
    b_clip_high = b_max + 25.0

    result_lab[:, :, 1] = np.clip(result_lab[:, :, 1], a_clip_low, a_clip_high)
    result_lab[:, :, 2] = np.clip(result_lab[:, :, 2], b_clip_low, b_clip_high)

    result_lab = np.clip(result_lab, 0, 255).astype(np.uint8)
    return cv2.cvtColor(result_lab, cv2.COLOR_LAB2BGR)