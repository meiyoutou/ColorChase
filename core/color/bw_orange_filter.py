import cv2
import numpy as np


def apply_orange_bw_filter(img_bgr: np.ndarray, strength: float = 1.0) -> np.ndarray:
    is_uint8 = img_bgr.dtype == np.uint8
    if is_uint8:
        img_float = img_bgr.astype(np.float32) / 255.0
    else:
        img_float = img_bgr.astype(np.float32)
    img_float = np.clip(img_float, 0.0, 1.0)

    b, g, r = img_float[:, :, 0], img_float[:, :, 1], img_float[:, :, 2]
    gray = 0.35 * r + 0.55 * g + 0.10 * b

    L = gray.copy()
    sin_term = 0.15 * np.sin((L - 0.5) * np.pi)
    L_s = L + sin_term
    L_s = np.clip(L_s, 0.0, 1.0)

    low_freq = cv2.GaussianBlur(L_s, (0, 0), sigmaX=30)
    high_freq = L_s - low_freq
    gray_enhanced = L_s + high_freq * 0.3
    gray_enhanced = np.clip(gray_enhanced, 0.0, 1.0)

    gray_3ch = np.stack([gray_enhanced, gray_enhanced, gray_enhanced], axis=-1)
    strength = np.clip(float(strength), 0.0, 1.0)
    result = img_float * (1.0 - strength) + gray_3ch * strength
    result = np.clip(result, 0.0, 1.0)

    if is_uint8:
        return (result * 255.0).astype(np.uint8)
    return result