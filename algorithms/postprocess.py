import cv2
import numpy as np
from .segmentation import full_segmentation, detect_skin_region, detect_sky_region
from .color_continuity import gentle_color_correction, smooth_transfer_via_3dlut


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -20, 20)))


def _distance_transform_soft_mask(binary_mask, transition_width=40):
    mask_uint8 = (binary_mask > 0.5).astype(np.uint8) * 255
    dist = cv2.distanceTransform(mask_uint8, cv2.DIST_L2, 5)
    dist_inv = cv2.distanceTransform(255 - mask_uint8, cv2.DIST_L2, 5)
    signed_dist = dist - dist_inv
    soft = _sigmoid(signed_dist / max(transition_width, 1))
    return soft.astype(np.float32)


def _guided_filter_mask(guide_img, mask, radius=8, eps=1e-2):
    guide_gray = cv2.cvtColor(guide_img, cv2.COLOR_BGR2GRAY)
    mask_u8 = (mask * 255).astype(np.uint8)
    try:
        refined = cv2.ximgproc.guidedFilter(guide_gray, mask_u8, radius, eps)
    except Exception:
        refined = cv2.bilateralFilter(mask_u8, 9, 75, 75)
    return refined.astype(np.float32) / 255.0


def _generate_soft_masks(target_img, transition_width=None):
    if transition_width is None:
        h = target_img.shape[0]
        transition_width = max(20, min(60, h // 20))

    raw_masks = full_segmentation(target_img)
    soft_masks = {}

    for name, mask in raw_masks.items():
        binary = (mask > 0.5).astype(np.float32)
        if binary.sum() < 10 or (1 - binary).sum() < 10:
            soft = mask
        else:
            soft = _distance_transform_soft_mask(binary, transition_width)
        soft = _guided_filter_mask(target_img, soft, radius=8, eps=1e-2)
        soft_masks[name] = np.clip(soft, 0, 1).astype(np.float32)

    return soft_masks


def _build_laplacian_pyramid(img, levels=4):
    pyramid = []
    current = img.astype(np.float32)
    for _ in range(levels):
        down = cv2.pyrDown(current)
        up = cv2.pyrUp(down, dstsize=(current.shape[1], current.shape[0]))
        lap = current - up
        pyramid.append(lap)
        current = down
    pyramid.append(current)
    return pyramid


def _reconstruct_from_laplacian(pyramid):
    current = pyramid[-1]
    for i in range(len(pyramid) - 2, -1, -1):
        current = cv2.pyrUp(current, dstsize=(pyramid[i].shape[1], pyramid[i].shape[0]))
        current = current + pyramid[i]
    return current


def _pyramid_blend(img_a, img_b, mask, levels=4):
    mask_pyramid = []
    mask_current = mask.astype(np.float32)
    for _ in range(levels):
        mask_pyramid.append(mask_current)
        mask_current = cv2.pyrDown(mask_current)

    lap_a = _build_laplacian_pyramid(img_a, levels)
    lap_b = _build_laplacian_pyramid(img_b, levels)

    blended_pyramid = []
    for i in range(levels):
        m = mask_pyramid[i][:, :, np.newaxis]
        blended = lap_a[i] * (1 - m) + lap_b[i] * m
        blended_pyramid.append(blended)

    blended_pyramid.append(lap_a[-1] * (1 - mask_current[:, :, np.newaxis]) +
                           lap_b[-1] * mask_current[:, :, np.newaxis])

    result = _reconstruct_from_laplacian(blended_pyramid)
    return np.clip(result, 0, 255).astype(np.uint8)


def _lab_color_continuity(img, sigma_color=12, sigma_space=8):
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.float32)
    l_ch = lab[:, :, 0].copy()
    a_u8 = lab[:, :, 1].astype(np.uint8)
    b_u8 = lab[:, :, 2].astype(np.uint8)
    a_smooth = cv2.bilateralFilter(a_u8, d=7, sigmaColor=sigma_color, sigmaSpace=sigma_space)
    b_smooth = cv2.bilateralFilter(b_u8, d=7, sigmaColor=sigma_color, sigmaSpace=sigma_space)
    lab[:, :, 0] = l_ch
    lab[:, :, 1] = a_smooth.astype(np.float32)
    lab[:, :, 2] = b_smooth.astype(np.float32)
    return cv2.cvtColor(lab.astype(np.uint8), cv2.COLOR_LAB2BGR)


def _aces_tone_map(img):
    x = img.astype(np.float32) / 255.0
    a, b, c, d, e = 2.51, 0.03, 2.43, 0.59, 0.14
    mapped = (x * (a * x + b)) / (x * (c * x + d) + e)
    mapped = np.clip(mapped, 0, 1)
    return (mapped * 255).astype(np.uint8)


def _film_look(img, shadow_lift=0.02, highlight_compress=0.05, micro_contrast=1.08):
    x = img.astype(np.float32) / 255.0
    x = x * (1 - shadow_lift) + shadow_lift
    x = x * (1 - highlight_compress) + highlight_compress * (1 - (1 - x) ** 1.5)
    mean = np.mean(x)
    x = (x - mean) * micro_contrast + mean
    x = np.clip(x, 0, 1)
    return (x * 255).astype(np.uint8)


def _detail_enhance(result_img, target_img, alpha=0.3):
    target_yuv = cv2.cvtColor(target_img, cv2.COLOR_BGR2YUV)
    result_yuv = cv2.cvtColor(result_img, cv2.COLOR_BGR2YUV)
    target_y = target_yuv[:, :, 0].astype(np.float32)
    result_y = result_yuv[:, :, 0].astype(np.float32)
    detail = target_y - cv2.GaussianBlur(target_y, (0, 0), 3)
    enhanced_y = result_y + detail * alpha
    result_yuv[:, :, 0] = np.clip(enhanced_y, 0, 255).astype(np.uint8)
    return cv2.cvtColor(result_yuv, cv2.COLOR_YUV2BGR)


def _global_fusion(target_img, transfer_img, soft_masks, region_strengths):
    h, w = target_img.shape[:2]
    weight_sum = np.zeros((h, w), dtype=np.float32)
    weighted_transfer = np.zeros((h, w, 3), dtype=np.float32)

    for region_name, strength in region_strengths.items():
        if region_name in soft_masks:
            mask = soft_masks[region_name]
            weighted_transfer += transfer_img.astype(np.float32) * mask[:, :, np.newaxis] * strength
            weight_sum += mask * strength

    weight_sum = np.clip(weight_sum, 1e-6, None)
    weight_sum_norm = weight_sum / weight_sum.max()
    weight_sum_norm = np.clip(weight_sum_norm, 0, 1)

    blend_mask = weight_sum_norm

    result = _pyramid_blend(target_img, transfer_img, blend_mask, levels=4)

    return result


def regional_transfer(
    target_img: np.ndarray,
    reference_img: np.ndarray,
    transfer_func,
    base_strength: float = 0.85,
    skin_strength: float = 0.35,
    sky_strength: float = 1.0,
    highlight_strength: float = 0.5,
    shadow_strength: float = 0.6,
    progress_callback=None,
) -> np.ndarray:
    def _cb(stage, fraction, message=""):
        if progress_callback is not None:
            progress_callback(stage, fraction, message)

    _cb("segment", 0.05, "语义分割中（皮肤/天空/高光/阴影）...")
    soft_masks = _generate_soft_masks(target_img)

    _cb("transfer", 0.15, "执行色彩迁移...")
    full_transfer = transfer_func(target_img, reference_img)

    region_strengths = {
        'skin': skin_strength * base_strength,
        'sky': sky_strength * base_strength,
        'highlight': highlight_strength * base_strength,
        'shadow': shadow_strength * base_strength,
        'midtone': base_strength,
    }

    _cb("blend", 0.55, "分区加权混合中...")
    result = _global_fusion(target_img, full_transfer, soft_masks, region_strengths)

    _cb("color_correct", 0.65, "3D LUT色彩平滑...")
    result = gentle_color_correction(
        target_img, result, reference_img,
        anomaly_percentile=95,
        correction_strength=0.25,
        progress_callback=lambda s, f, m: _cb("color_correct", 0.65 + f * 0.1, m),
    )

    _cb("detail", 0.82, "细节增强...")
    result = _detail_enhance(result, target_img, alpha=0.3)

    _cb("film", 0.92, "胶片质感处理...")
    result = _film_look(result, shadow_lift=0.01, highlight_compress=0.03, micro_contrast=1.02)

    _cb("done", 1.0, "分区追色完成")
    return result


def enhance_transfer_result(
    result_img: np.ndarray,
    target_img: np.ndarray,
    reference_img: np.ndarray,
    skin_protection: float = 0.4,
    highlight_protection: float = 0.3,
    shadow_protection: float = 0.25,
    edge_preserve: float = 0.12,
    detail_alpha: float = 0.35,
    progress_callback=None,
) -> np.ndarray:
    def _cb(stage, fraction, message=""):
        if progress_callback is not None:
            progress_callback(stage, fraction, message)

    _cb("segment", 0.05, "语义分割中...")
    soft_masks = _generate_soft_masks(target_img)
    enhanced = result_img.copy()

    _cb("protect", 0.15, "皮肤/高光/阴影保护...")
    target_lab = cv2.cvtColor(target_img, cv2.COLOR_BGR2LAB).astype(np.float32)
    result_lab = cv2.cvtColor(enhanced, cv2.COLOR_BGR2LAB).astype(np.float32)

    skin_mask = soft_masks['skin']
    for ch in [1, 2]:
        result_lab[:, :, ch] = (
            result_lab[:, :, ch] * (1 - skin_mask * skin_protection) +
            target_lab[:, :, ch] * skin_mask * skin_protection
        )

    highlight_mask = soft_masks['highlight']
    result_lab[:, :, 0] = (
        result_lab[:, :, 0] * (1 - highlight_mask * highlight_protection) +
        target_lab[:, :, 0] * highlight_mask * highlight_protection
    )

    shadow_mask = soft_masks['shadow']
    result_lab[:, :, 0] = (
        result_lab[:, :, 0] * (1 - shadow_mask * shadow_protection) +
        target_lab[:, :, 0] * shadow_mask * shadow_protection
    )

    enhanced = cv2.cvtColor(np.clip(result_lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)

    _cb("edge", 0.25, "边缘保护...")
    target_gray = cv2.cvtColor(target_img, cv2.COLOR_BGR2GRAY)
    try:
        from algorithms.metrics.content_similarity import _calculate_ldc_edge, _get_ldc_model
        ldc_model = _get_ldc_model()
        if ldc_model is not None:
            target_rgb = cv2.cvtColor(target_img, cv2.COLOR_BGR2RGB)
            edge_map = _calculate_ldc_edge(target_rgb)
            if edge_map.ndim == 3:
                edge_mask = edge_map[:, :, 0]
            else:
                edge_mask = edge_map
            edge_mask = cv2.resize(edge_mask, (target_gray.shape[1], target_gray.shape[0]))
            edge_mask = np.clip(edge_mask, 0, 1).astype(np.float32)
            edge_mask = cv2.GaussianBlur(edge_mask, (7, 7), 2)
        else:
            raise RuntimeError("LDC model not available")
    except Exception:
        edges = cv2.Canny(target_gray, 30, 100)
        edges_dilated = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
        edge_mask = (edges_dilated > 0).astype(np.float32) / 255.0
        edge_mask = cv2.GaussianBlur(edge_mask, (7, 7), 2)
    edge_mask_3ch = np.stack([edge_mask] * 3, axis=-1) * edge_preserve
    enhanced = (enhanced.astype(np.float32) * (1 - edge_mask_3ch) +
                target_img.astype(np.float32) * edge_mask_3ch).astype(np.uint8)

    _cb("detail", 0.35, "细节增强...")
    enhanced = _detail_enhance(enhanced, target_img, detail_alpha)

    _cb("color_correct", 0.50, "3D LUT色彩平滑 + 异常修正...")
    enhanced = gentle_color_correction(
        target_img, enhanced, reference_img,
        anomaly_percentile=95,
        correction_strength=0.2,
        progress_callback=lambda s, f, m: _cb("color_correct", 0.5 + f * 0.3, m),
    )

    _cb("film", 0.88, "胶片质感处理...")
    enhanced = _film_look(enhanced, shadow_lift=0.01, highlight_compress=0.02, micro_contrast=1.01)

    _cb("done", 1.0, "智能处理完成")
    return enhanced
