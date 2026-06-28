import cv2
import inspect
import numpy as np


def reinhard_transfer(target_img, reference_img):
    t_lab = cv2.cvtColor(target_img, cv2.COLOR_BGR2LAB).astype(np.float64)
    r_lab = cv2.cvtColor(reference_img, cv2.COLOR_BGR2LAB).astype(np.float64)

    result = t_lab.copy()
    for i in range(3):
        t_mean, t_std = t_lab[:, :, i].mean(), t_lab[:, :, i].std()
        r_mean, r_std = r_lab[:, :, i].mean(), r_lab[:, :, i].std()
        if t_std > 1e-6:
            result[:, :, i] = (t_lab[:, :, i] - t_mean) * (r_std / t_std) + r_mean
        else:
            result[:, :, i] = t_lab[:, :, i] - t_mean + r_mean

    result = np.clip(result, 0, 255).astype(np.uint8)
    return cv2.cvtColor(result, cv2.COLOR_LAB2BGR)


def _match_channel(cdf_t, cdf_r, t_values):
    lookup = np.zeros(256, dtype=np.uint8)
    for i in range(256):
        idx = np.argmin(np.abs(cdf_r - cdf_t[i]))
        lookup[i] = idx
    return lookup[t_values]


def histogram_matching(target_img, reference_img):
    result = target_img.copy()
    t_lab = cv2.cvtColor(target_img, cv2.COLOR_BGR2LAB)
    r_lab = cv2.cvtColor(reference_img, cv2.COLOR_BGR2LAB)

    for ch in range(3):
        t_ch = t_lab[:, :, ch].flatten()
        r_ch = r_lab[:, :, ch].flatten()

        t_hist, _ = np.histogram(t_ch, bins=256, range=(0, 256))
        r_hist, _ = np.histogram(r_ch, bins=256, range=(0, 256))

        t_cdf = t_hist.cumsum().astype(np.float64)
        r_cdf = r_hist.cumsum().astype(np.float64)

        t_cdf /= t_cdf[-1] if t_cdf[-1] > 0 else 1
        r_cdf /= r_cdf[-1] if r_cdf[-1] > 0 else 1

        matched = _match_channel(t_cdf, r_cdf, t_lab[:, :, ch])
        result[:, :, ch] = matched.reshape(t_lab[:, :, ch].shape)

    return cv2.cvtColor(result, cv2.COLOR_LAB2BGR)


def _get_luminance_mask(lab_img, low_thresh, high_thresh):
    l_ch = lab_img[:, :, 0].astype(np.float64) / 255.0
    mask = np.ones_like(l_ch, dtype=np.float32)
    k = 0.15
    if low_thresh is not None:
        mask *= 1.0 / (1.0 + np.exp(-k * (l_ch - low_thresh)))
    if high_thresh is not None:
        mask *= 1.0 / (1.0 + np.exp(k * (l_ch - high_thresh)))
    return mask.astype(np.float32)


def _blend_with_mask(base, overlay, mask):
    mask_3ch = np.stack([mask] * 3, axis=-1)
    return (base * (1 - mask_3ch) + overlay * mask_3ch).astype(np.uint8)


def luminance_partition_transfer(target_img, reference_img, blend_strength=0.85):
    t_lab = cv2.cvtColor(target_img, cv2.COLOR_BGR2LAB).astype(np.float64)
    r_lab = cv2.cvtColor(reference_img, cv2.COLOR_BGR2LAB).astype(np.float64)

    t_l = t_lab[:, :, 0] / 255.0
    r_l = r_lab[:, :, 0] / 255.0

    t_low = np.percentile(t_l, 33)
    t_mid = np.percentile(t_l, 66)
    r_low = np.percentile(r_l, 33)
    r_mid = np.percentile(r_l, 66)

    regions = [
        (None, t_low, None, r_low),
        (t_low, t_mid, r_low, r_mid),
        (t_mid, None, r_mid, None),
    ]

    result = target_img.copy().astype(np.float64)

    for t_lo, t_hi, r_lo, r_hi in regions:
        t_mask = _get_luminance_mask(t_lab, t_lo, t_hi)
        r_mask = _get_luminance_mask(r_lab, r_lo, r_hi)

        t_region_lab = t_lab.copy()
        r_region_lab = r_lab.copy()

        for ch in range(3):
            t_vals = t_lab[:, :, ch][t_mask > 0.5]
            r_vals = r_lab[:, :, ch][r_mask > 0.5]

            if len(t_vals) > 0 and len(r_vals) > 0:
                t_mean, t_std = t_vals.mean(), t_vals.std()
                r_mean, r_std = r_vals.mean(), r_vals.std()
                if t_std > 1e-6:
                    mapped = (t_lab[:, :, ch] - t_mean) * (r_std / t_std) + r_mean
                else:
                    mapped = t_lab[:, :, ch] - t_mean + r_mean
                t_region_lab[:, :, ch] = mapped

        mapped_img = np.clip(t_region_lab, 0, 255).astype(np.uint8)
        mapped_bgr = cv2.cvtColor(mapped_img, cv2.COLOR_LAB2BGR).astype(np.float64)

        result = _blend_with_mask(result, mapped_bgr, t_mask)

    original = target_img.astype(np.float64)
    result = original * (1 - blend_strength) + result * blend_strength
    return np.clip(result, 0, 255).astype(np.uint8)


def generate_3dlut_from_reinhard(target_img, reference_img, lut_size=17):
    t_lab = cv2.cvtColor(target_img, cv2.COLOR_BGR2LAB).astype(np.float64)
    r_lab = cv2.cvtColor(reference_img, cv2.COLOR_BGR2LAB).astype(np.float64)

    params = []
    for ch in range(3):
        t_mean, t_std = t_lab[:, :, ch].mean(), t_lab[:, :, ch].std()
        r_mean, r_std = r_lab[:, :, ch].mean(), r_lab[:, :, ch].std()
        params.append((t_mean, t_std, r_mean, r_std))

    lut = np.zeros((lut_size, lut_size, lut_size, 3), dtype=np.float32)

    for ri in range(lut_size):
        for gi in range(lut_size):
            for bi in range(lut_size):
                r_val = ri / (lut_size - 1) * 255.0
                g_val = gi / (lut_size - 1) * 255.0
                b_val = bi / (lut_size - 1) * 255.0

                pixel = np.array([[[b_val, g_val, r_val]]], dtype=np.uint8)
                lab = cv2.cvtColor(pixel, cv2.COLOR_BGR2LAB).astype(np.float64)

                for ch in range(3):
                    t_m, t_s, r_m, r_s = params[ch]
                    if t_s > 1e-6:
                        lab[0, 0, ch] = (lab[0, 0, ch] - t_m) * (r_s / t_s) + r_m
                    else:
                        lab[0, 0, ch] = lab[0, 0, ch] - t_m + r_m

                lab = np.clip(lab, 0, 255).astype(np.uint8)
                bgr = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

                lut[ri, gi, bi, 0] = bgr[0, 0, 2] / 255.0
                lut[ri, gi, bi, 1] = bgr[0, 0, 1] / 255.0
                lut[ri, gi, bi, 2] = bgr[0, 0, 0] / 255.0

    return lut


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

    rf = r_idx - r0
    gf = g_idx - g0
    bf = b_idx - b0

    def _lerp(a, b, t):
        return a * (1 - t) + b * t

    c000 = lut[r0, g0, b0]
    c001 = lut[r0, g0, b1]
    c010 = lut[r0, g1, b0]
    c011 = lut[r0, g1, b1]
    c100 = lut[r1, g0, b0]
    c101 = lut[r1, g0, b1]
    c110 = lut[r1, g1, b0]
    c111 = lut[r1, g1, b1]

    rf3 = rf[:, :, np.newaxis]
    gf3 = gf[:, :, np.newaxis]
    bf3 = bf[:, :, np.newaxis]

    c00 = _lerp(c000, c001, bf3)
    c01 = _lerp(c010, c011, bf3)
    c10 = _lerp(c100, c101, bf3)
    c11 = _lerp(c110, c111, bf3)

    c0 = _lerp(c00, c01, gf3)
    c1 = _lerp(c10, c11, gf3)

    result = _lerp(c0, c1, rf3)

    result = np.clip(result * 255, 0, 255).astype(np.uint8)
    return cv2.cvtColor(result, cv2.COLOR_RGB2BGR)


ALGORITHMS = {
    "reinhard": reinhard_transfer,
    "histogram": histogram_matching,
    "luminance_partition": luminance_partition_transfer,
}


def transfer_color(target_img, reference_img, algorithm="luminance_partition", **kwargs):
    if algorithm not in ALGORITHMS:
        raise ValueError(f"Unknown algorithm: {algorithm}. Available: {list(ALGORITHMS.keys())}")
    handler = ALGORITHMS[algorithm]
    signature = inspect.signature(handler)
    supported_kwargs = {
        key: value
        for key, value in kwargs.items()
        if key in signature.parameters
    }
    return handler(target_img, reference_img, **supported_kwargs)
