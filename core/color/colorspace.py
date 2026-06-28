import numpy as np
from typing import Optional, Tuple


_SRGB_GAMMA = 2.2


def _srgb_to_linear(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0, 1)
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0, 1)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * (x ** (1.0 / 2.4)) - 0.055)


def to_linear_rgb(rgb: np.ndarray, is_linear: bool = False) -> np.ndarray:
    if is_linear:
        return rgb.astype(np.float32)
    return _srgb_to_linear(rgb.astype(np.float32) / 255.0 if rgb.max() > 1.0 else rgb.astype(np.float32))


def to_srgb_rgb(rgb: np.ndarray, was_linear: bool = True) -> np.ndarray:
    if not was_linear:
        return rgb
    srgb = _linear_to_srgb(np.clip(rgb, 0, 1))
    return (srgb * 255).astype(np.uint8)


def detect_colorspace(rgb: np.ndarray, meta: dict) -> str:
    if meta.get('is_linear', False):
        return 'linear'
    if meta.get('colorspace') == 'linear_raw':
        return 'linear_raw'
    if meta.get('colorspace') == 'linear':
        return 'linear'
    return 'srgb'


def apply_icc_transform(rgb: np.ndarray, icc_bytes: bytes) -> np.ndarray:
    try:
        from PIL import Image, ImageCms
        src_profile = ImageCms.ImageCmsProfile(ImageCms.core.profile_from_string(icc_bytes))
        dst_profile = ImageCms.createProfile('sRGB')
        pil_img = Image.fromarray((np.clip(rgb, 0, 1) * 255).astype(np.uint8), 'RGB')
        transformed = ImageCms.profileToProfile(pil_img, src_profile, dst_profile)
        return np.array(transformed, dtype=np.float32) / 255.0
    except Exception:
        return rgb


def normalize_to_working_space(rgb: np.ndarray, meta: dict, working_space: str = 'srgb') -> Tuple[np.ndarray, dict]:
    colorspace = detect_colorspace(rgb, meta)
    result = rgb.copy()
    new_meta = meta.copy()

    if colorspace in ('linear', 'linear_raw'):
        if working_space == 'srgb':
            result = _linear_to_srgb(np.clip(result, 0, 1))
            new_meta['is_linear'] = False
            new_meta['colorspace'] = 'srgb'
        elif working_space == 'linear':
            pass
    elif colorspace == 'srgb':
        if working_space == 'linear':
            result = _srgb_to_linear(np.clip(result, 0, 1))
            new_meta['is_linear'] = True
            new_meta['colorspace'] = 'linear'

    new_meta['working_space'] = working_space
    return result, new_meta
