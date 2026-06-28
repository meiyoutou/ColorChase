import re
import xml.etree.ElementTree as ET
import cv2
import numpy as np
from scipy.interpolate import PchipInterpolator


def parse_xmp_preset(file_path: str) -> dict:
    with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
        text = f.read()

    params = {
        'Exposure2012': 0.0,
        'Contrast2012': 0,
        'Highlights2012': 0,
        'Shadows2012': 0,
        'Whites2012': 0,
        'Blacks2012': 0,
        'Temperature': 5500,
        'Tint': 0,
        'Vibrance': 0,
        'Saturation': 0,
        'ToneCurvePV2012': [],
        'ToneCurvePV2012Red': [],
        'ToneCurvePV2012Green': [],
        'ToneCurvePV2012Blue': [],
    }

    scalar_keys = [
        'Exposure2012', 'Contrast2012', 'Highlights2012', 'Shadows2012',
        'Whites2012', 'Blacks2012', 'Temperature', 'Tint', 'Vibrance', 'Saturation',
    ]

    for key in scalar_keys:
        pattern = r'crs:' + key + r'="([^"]*)"'
        m = re.search(pattern, text)
        if m:
            try:
                params[key] = float(m.group(1))
            except ValueError:
                pass
        else:
            pattern2 = r'crs:' + key + r'>([^<]*)<'
            m2 = re.search(pattern2, text)
            if m2:
                try:
                    params[key] = float(m2.group(1))
                except ValueError:
                    pass

    curve_keys = [
        'ToneCurvePV2012', 'ToneCurvePV2012Red',
        'ToneCurvePV2012Green', 'ToneCurvePV2012Blue',
    ]

    for key in curve_keys:
        pattern = r'crs:' + key + r'="([^"]*)"'
        m = re.search(pattern, text)
        if m:
            params[key] = _parse_tone_curve_value(m.group(1))
        else:
            pattern2 = r'crs:' + key + r'>([^<]*)<'
            m2 = re.search(pattern2, text)
            if m2:
                params[key] = _parse_tone_curve_value(m2.group(1))

    return params


def _parse_tone_curve_value(val_str: str) -> list:
    if not val_str or not val_str.strip():
        return []
    points = []
    for pair in val_str.strip().split(';'):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split(',')
        if len(parts) >= 2:
            try:
                x = float(parts[0].strip())
                y = float(parts[1].strip())
                points.append((x, y))
            except ValueError:
                continue
    return points


def bake_xmp_to_lut(params: dict, lut_size: int = 33) -> np.ndarray:
    r_vals = np.linspace(0, 1, lut_size, dtype=np.float32)
    g_vals = np.linspace(0, 1, lut_size, dtype=np.float32)
    b_vals = np.linspace(0, 1, lut_size, dtype=np.float32)

    rg, gg, bg = np.meshgrid(r_vals, g_vals, b_vals, indexing='ij')
    rgb = np.stack([rg, gg, bg], axis=-1)
    rgb = rgb.reshape(-1, 3).copy()

    rgb = _apply_white_balance(rgb, params)
    rgb = _apply_exposure(rgb, params)
    rgb = _apply_contrast(rgb, params)
    rgb = _apply_highlights_shadows(rgb, params)
    rgb = _apply_tone_curves(rgb, params)
    rgb = _apply_vibrance_saturation(rgb, params)

    rgb = np.clip(rgb, 0.0, 1.0)
    lut = rgb.reshape(lut_size, lut_size, lut_size, 3).astype(np.float32)
    return lut


def _apply_white_balance(rgb: np.ndarray, params: dict) -> np.ndarray:
    temperature = params.get('Temperature', 5500)
    tint = params.get('Tint', 0)

    if temperature == 5500 and tint == 0:
        return rgb

    temp_ratio = (temperature - 5500) / 5500
    r_gain = 1.0 + temp_ratio * 0.4
    b_gain = 1.0 - temp_ratio * 0.4
    g_offset = tint * 0.002

    rgb[:, 0] *= r_gain
    rgb[:, 2] *= b_gain
    rgb[:, 1] += g_offset

    return rgb


def _apply_exposure(rgb: np.ndarray, params: dict) -> np.ndarray:
    exposure = params.get('Exposure2012', 0.0)
    if exposure == 0.0:
        return rgb

    gain = np.float32(2.0 ** exposure)
    rgb *= gain
    return rgb


def _apply_contrast(rgb: np.ndarray, params: dict) -> np.ndarray:
    contrast = params.get('Contrast2012', 0)
    if contrast == 0:
        return rgb

    factor = 1.0 + contrast / 100.0
    rgb = (rgb - 0.5) * factor + 0.5
    return rgb


def _apply_highlights_shadows(rgb: np.ndarray, params: dict) -> np.ndarray:
    highlights = params.get('Highlights2012', 0)
    shadows = params.get('Shadows2012', 0)
    whites = params.get('Whites2012', 0)
    blacks = params.get('Blacks2012', 0)

    if highlights == 0 and shadows == 0 and whites == 0 and blacks == 0:
        return rgb

    lum = 0.299 * rgb[:, 0] + 0.587 * rgb[:, 1] + 0.114 * rgb[:, 2]

    hi_mask = np.clip((lum - 0.5) * 2.0, 0, 1)
    sh_mask = np.clip((0.5 - lum) * 2.0, 0, 1)

    hi_shift = (highlights / 100.0) * 0.3 + (whites / 100.0) * 0.15
    sh_shift = (shadows / 100.0) * 0.3 + (blacks / 100.0) * 0.15

    shift = hi_mask * hi_shift + sh_mask * sh_shift
    rgb += shift[:, np.newaxis]

    return rgb


def _apply_tone_curves(rgb: np.ndarray, params: dict) -> np.ndarray:
    master_pts = params.get('ToneCurvePV2012', [])
    r_pts = params.get('ToneCurvePV2012Red', [])
    g_pts = params.get('ToneCurvePV2012Green', [])
    b_pts = params.get('ToneCurvePV2012Blue', [])

    if not master_pts and not r_pts and not g_pts and not b_pts:
        return rgb

    x_sample = np.linspace(0, 255, 256, dtype=np.float32)

    if master_pts and len(master_pts) >= 2:
        master_lut = _build_curve_lut(master_pts, x_sample)
        rgb[:, 0] = np.interp(rgb[:, 0] * 255, x_sample, master_lut) / 255.0
        rgb[:, 1] = np.interp(rgb[:, 1] * 255, x_sample, master_lut) / 255.0
        rgb[:, 2] = np.interp(rgb[:, 2] * 255, x_sample, master_lut) / 255.0

    if r_pts and len(r_pts) >= 2:
        r_lut = _build_curve_lut(r_pts, x_sample)
        rgb[:, 0] = np.interp(rgb[:, 0] * 255, x_sample, r_lut) / 255.0

    if g_pts and len(g_pts) >= 2:
        g_lut = _build_curve_lut(g_pts, x_sample)
        rgb[:, 1] = np.interp(rgb[:, 1] * 255, x_sample, g_lut) / 255.0

    if b_pts and len(b_pts) >= 2:
        b_lut = _build_curve_lut(b_pts, x_sample)
        rgb[:, 2] = np.interp(rgb[:, 2] * 255, x_sample, b_lut) / 255.0

    return rgb


def _build_curve_lut(points: list, x_sample: np.ndarray) -> np.ndarray:
    try:
        pts = sorted(points, key=lambda p: p[0])
        xp = np.array([p[0] for p in pts], dtype=np.float32)
        yp = np.array([p[1] for p in pts], dtype=np.float32)
        interp = PchipInterpolator(xp, yp)
        result = interp(x_sample)
        return np.clip(result, 0, 255).astype(np.float32)
    except Exception:
        xp = np.array([p[0] for p in points], dtype=np.float32)
        yp = np.array([p[1] for p in points], dtype=np.float32)
        result = np.interp(x_sample, xp, yp)
        return np.clip(result, 0, 255).astype(np.float32)


def _apply_vibrance_saturation(rgb: np.ndarray, params: dict) -> np.ndarray:
    vibrance = params.get('Vibrance', 0)
    saturation = params.get('Saturation', 0)

    if vibrance == 0 and saturation == 0:
        return rgb

    r, g, b = rgb[:, 0], rgb[:, 1], rgb[:, 2]

    c_max = np.maximum(np.maximum(r, g), b)
    c_min = np.minimum(np.minimum(r, g), b)
    delta = c_max - c_min
    sat = np.where(c_max > 0, delta / c_max, 0)

    if vibrance != 0:
        vib_factor = 1.0 + (vibrance / 100.0) * (1.0 - sat)
        rgb[:, 0] = c_max - (c_max - r) * vib_factor
        rgb[:, 1] = c_max - (c_max - g) * vib_factor
        rgb[:, 2] = c_max - (c_max - b) * vib_factor

    if saturation != 0:
        gray = 0.299 * rgb[:, 0] + 0.587 * rgb[:, 1] + 0.114 * rgb[:, 2]
        sat_factor = 1.0 + saturation / 100.0
        rgb[:, 0] = gray + (rgb[:, 0] - gray) * sat_factor
        rgb[:, 1] = gray + (rgb[:, 1] - gray) * sat_factor
        rgb[:, 2] = gray + (rgb[:, 2] - gray) * sat_factor

    return rgb
