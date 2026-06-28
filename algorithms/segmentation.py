import cv2
import numpy as np
from PIL import Image


_mp_selfie = None


def _get_selfie_segmenter():
    global _mp_selfie
    if _mp_selfie is None:
        try:
            import mediapipe as mp
            _mp_selfie = mp.solutions.selfie_segmentation.SelfieSegmentation(
                model_selection=1
            )
        except Exception:
            _mp_selfie = None
    return _mp_selfie


def detect_skin_region(img_bgr):
    h, w = img_bgr.shape[:2]
    segmenter = _get_selfie_segmenter()

    if segmenter is not None:
        try:
            rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
            result = segmenter.process(rgb)
            if result.segmentation_mask is not None:
                mask = result.segmentation_mask
                mask = cv2.resize(mask, (w, h))
                person_mask = (mask > 0.5).astype(np.float32)

                hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV).astype(np.float32)
                skin_in_person = (
                    (hsv[:, :, 0] > 0) & (hsv[:, :, 0] < 50) &
                    (hsv[:, :, 1] > 30) & (hsv[:, :, 1] < 200) &
                    (hsv[:, :, 2] > 60) & (hsv[:, :, 2] < 255)
                ).astype(np.float32)

                skin_mask = person_mask * skin_in_person
                skin_mask = cv2.GaussianBlur(skin_mask, (5, 5), 2)
                return np.clip(skin_mask, 0, 1)
        except Exception:
            pass

    lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
    skin_mask = (
        (lab[:, :, 1] > 110) & (lab[:, :, 1] < 160) &
        (lab[:, :, 2] > 120) & (lab[:, :, 2] < 175) &
        (lab[:, :, 0] > 40) & (lab[:, :, 0] < 220)
    ).astype(np.float32)
    skin_mask = cv2.GaussianBlur(skin_mask, (5, 5), 2)
    return np.clip(skin_mask, 0, 1)


def detect_person_region(img_bgr):
    h, w = img_bgr.shape[:2]
    segmenter = _get_selfie_segmenter()

    if segmenter is not None:
        try:
            rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
            result = segmenter.process(rgb)
            if result.segmentation_mask is not None:
                mask = result.segmentation_mask
                mask = cv2.resize(mask, (w, h))
                person_mask = (mask > 0.5).astype(np.float32)
                person_mask = cv2.GaussianBlur(person_mask, (5, 5), 2)
                return np.clip(person_mask, 0, 1)
        except Exception:
            pass

    return np.zeros((h, w), dtype=np.float32)


def detect_sky_region(img_bgr):
    h, w = img_bgr.shape[:2]
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV).astype(np.float32)

    sky_mask = (
        (hsv[:, :, 0] > 85) & (hsv[:, :, 0] < 135) &
        (hsv[:, :, 1] > 20) & (hsv[:, :, 1] < 255) &
        (hsv[:, :, 2] > 100) & (hsv[:, :, 2] < 255)
    ).astype(np.float32)

    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150)
    edges_dilated = cv2.dilate(edges, np.ones((5, 5), np.uint8), iterations=2)

    top_region = np.zeros((h, w), dtype=np.float32)
    sky_line = int(h * 0.6)
    top_region[:sky_line, :] = 1.0

    sky_mask = sky_mask * top_region
    sky_mask[edges_dilated > 0] = 0
    sky_mask = cv2.GaussianBlur(sky_mask, (11, 11), 3)
    return np.clip(sky_mask, 0, 1)


def detect_luminance_regions(img_bgr):
    lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
    l_ch = lab[:, :, 0] / 255.0

    shadow_mask = np.clip(1.0 - l_ch * 3.0, 0, 1)
    shadow_mask = (l_ch < 0.25).astype(np.float32)

    highlight_mask = np.clip((l_ch - 0.75) * 4.0, 0, 1)
    highlight_mask = (l_ch > 0.8).astype(np.float32)

    midtone_mask = np.ones_like(l_ch)
    midtone_mask -= shadow_mask * 0.8
    midtone_mask -= highlight_mask * 0.8
    midtone_mask = np.clip(midtone_mask, 0, 1)

    shadow_mask = cv2.GaussianBlur(shadow_mask, (7, 7), 2)
    highlight_mask = cv2.GaussianBlur(highlight_mask, (7, 7), 2)
    midtone_mask = cv2.GaussianBlur(midtone_mask, (7, 7), 2)

    return {
        'shadow': np.clip(shadow_mask, 0, 1),
        'highlight': np.clip(highlight_mask, 0, 1),
        'midtone': np.clip(midtone_mask, 0, 1),
    }


def full_segmentation(img_bgr):
    skin_mask = detect_skin_region(img_bgr)
    sky_mask = detect_sky_region(img_bgr)
    lum_regions = detect_luminance_regions(img_bgr)

    return {
        'skin': skin_mask,
        'sky': sky_mask,
        'shadow': lum_regions['shadow'],
        'highlight': lum_regions['highlight'],
        'midtone': lum_regions['midtone'],
    }


def visualize_segmentation(img_bgr, masks):
    vis = img_bgr.copy()
    colors = {
        'skin': (0, 255, 255),
        'sky': (255, 200, 0),
        'shadow': (0, 0, 255),
        'highlight': (0, 255, 0),
        'midtone': (255, 0, 255),
    }
    overlay = vis.copy()
    for name, mask in masks.items():
        if name in colors:
            color = colors[name]
            mask_3ch = np.stack([mask] * 3, axis=-1)
            color_arr = np.array(color, dtype=np.float32).reshape(1, 1, 3)
            overlay = (overlay * (1 - mask_3ch * 0.4) + color_arr * mask_3ch * 0.4).astype(np.uint8)

    return cv2.addWeighted(vis, 0.5, overlay, 0.5, 0)
