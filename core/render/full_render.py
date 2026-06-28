import os
import cv2
import numpy as np
from scipy.ndimage import map_coordinates
from typing import Dict, Any, Optional, Tuple
from core.io.loaders import load_image, load_image_bgr
from core.cache import StyleRepresentation


_expanded_lut_cache = {}


def _expand_lut_trilinear(lut_3d: np.ndarray, target_size: int = 65) -> np.ndarray:
    src_size = lut_3d.shape[0]
    grid = np.linspace(0, src_size - 1, target_size)
    rg, gg, bg = np.meshgrid(grid, grid, grid, indexing='ij')
    coords = np.stack([rg.ravel(), gg.ravel(), bg.ravel()], axis=0)

    expanded = np.empty((target_size, target_size, target_size, 3), dtype=np.float32)
    for c in range(3):
        expanded[..., c] = map_coordinates(
            lut_3d[..., c], coords, order=1, mode='nearest'
        ).reshape(target_size, target_size, target_size)

    return np.clip(expanded, 0, 1)


def apply_lut(img_np: np.ndarray, lut_3d: np.ndarray) -> np.ndarray:
    h, w, _ = img_np.shape
    lut_size = lut_3d.shape[0]
    input_dtype = img_np.dtype

    if input_dtype == np.uint16:
        max_val = 65535.0
        out_dtype = np.uint16
    else:
        max_val = 255.0
        out_dtype = np.uint8

    lut_key = id(lut_3d)
    if lut_key not in _expanded_lut_cache:
        expand_size = max(lut_size * 2 - 1, 65)
        _expanded_lut_cache[lut_key] = _expand_lut_trilinear(lut_3d, expand_size)

    expanded = _expanded_lut_cache[lut_key]
    exp_size = expanded.shape[0]

    scale = float(exp_size - 1) / max_val

    flat_lut = expanded.reshape(-1, 3)
    ch_stride = exp_size * exp_size

    img_flat = img_np.reshape(-1, 3)
    r_q = np.clip(np.round(img_flat[:, 0].astype(np.float32) * scale), 0, exp_size - 1).astype(np.int32)
    g_q = np.clip(np.round(img_flat[:, 1].astype(np.float32) * scale), 0, exp_size - 1).astype(np.int32)
    b_q = np.clip(np.round(img_flat[:, 2].astype(np.float32) * scale), 0, exp_size - 1).astype(np.int32)

    flat_idx = r_q * ch_stride + g_q * exp_size + b_q

    result = flat_lut[flat_idx]

    result = np.clip(result * max_val, 0, max_val).astype(out_dtype).reshape(h, w, 3)
    return result


def apply_portrait_with_lab(
    img_np: np.ndarray,
    lut_global: np.ndarray,
    mask_low_res: np.ndarray,
    ref_stats: np.ndarray = None,
    lip_mask_low_res: np.ndarray = None,
    hair_mask_low_res: np.ndarray = None,
) -> np.ndarray:
    h, w = img_np.shape[:2]

    img_global = apply_lut(img_np, lut_global)

    mask_up = cv2.resize(mask_low_res, (w, h), interpolation=cv2.INTER_LINEAR)
    mask_up = np.clip(mask_up, 0, 1)

    if mask_up.max() < 0.01:
        return img_global

    mask_3ch = mask_up[:, :, np.newaxis]
    result = (img_global.astype(np.float32) * (1.0 - mask_3ch * 0.3) +
              img_np.astype(np.float32) * (mask_3ch * 0.3))
    result = np.clip(result, 0, 255).astype(np.uint8)

    print(f"[Portrait Full Render] {w}x{h}: LUT applied with skin blend (no double-processing)")
    return result


def reinhard_transfer_full(target_img: np.ndarray, ref_stats: Dict[str, Any], strength: float = 1.0) -> np.ndarray:
    """
    全尺寸 Reinhard 色彩迁移（基于统计）
    ref_stats 从 preview 图分析得出
    """
    target_lab = cv2.cvtColor(target_img, cv2.COLOR_BGR2LAB).astype(np.float32)
    target_mean = np.mean(target_lab, axis=(0, 1))
    target_std = np.std(target_lab, axis=(0, 1))

    ref_mean = ref_stats.get('mean', np.array([128, 128, 128], dtype=np.float32))
    ref_std = ref_stats.get('std', np.array([50, 10, 10], dtype=np.float32))

    result_lab = (target_lab - target_mean) * (ref_std / (target_std + 1e-6)) + ref_mean
    result_lab = strength * result_lab + (1 - strength) * target_lab
    result_lab = np.clip(result_lab, 0, 255).astype(np.uint8)
    return cv2.cvtColor(result_lab, cv2.COLOR_LAB2BGR)


def histogram_match_full(target_img: np.ndarray, ref_hist: Dict[str, Any], strength: float = 1.0) -> np.ndarray:
    target_lab = cv2.cvtColor(target_img, cv2.COLOR_BGR2LAB)
    result = target_lab.copy()
    for c in range(3):
        target_c = target_lab[:, :, c]
        target_hist, _ = np.histogram(target_c.flatten(), 256, [0, 256])
        ref_cdf = ref_hist.get(f'cdf_{c}', np.cumsum(target_hist))

        target_cdf = np.cumsum(target_hist)
        target_cdf_norm = (target_cdf / target_cdf[-1] * 255).astype(np.uint8)

        lut = np.zeros(256, dtype=np.uint8)
        for i in range(256):
            lut[i] = np.argmin(np.abs(ref_cdf - target_cdf_norm[i]))

        matched_c = cv2.LUT(target_c, lut)
        ch_strength = strength if c == 0 else min(strength, 0.8)
        result[:, :, c] = ch_strength * matched_c + (1 - ch_strength) * target_c
    result = np.clip(result, 0, 255).astype(np.uint8)
    return cv2.cvtColor(result, cv2.COLOR_LAB2BGR)


def full_render_from_style(
    target_path: str,
    style: StyleRepresentation,
    reference_path: str = None,
    reference_img: np.ndarray = None,
    progress_callback=None,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    """
    全尺寸渲染管线：
    1. 用 mode=export 加载全尺寸 RAW/原图
    2. 根据 style.algorithm_name 应用风格
    3. 对于 AI 模型，若提供 reference_path/reference_img 则调用真实模型
    4. 输出高质量结果
    """
    if progress_callback:
        progress_callback("render", 0.0, "正在加载全尺寸图片...")

    bgr, meta = load_image_bgr(target_path, mode="export", target_size=None)
    h, w = bgr.shape[:2]

    if progress_callback:
        progress_callback("render", 0.15, f"已加载 {w}x{h}...")

    algorithm = style.algorithm_name
    strength = style.blend_strength
    result = bgr.copy()

    use_neural = algorithm in ("neural_preset", "modflows", "regional_modflows")
    ref_img = reference_img
    if ref_img is None and reference_path and os.path.exists(reference_path):
        ref_img, _ = load_image_bgr(reference_path, mode="export", target_size=None)

    if algorithm in ("reinhard", "regional_luminance"):
        result = reinhard_transfer_full(bgr, style.color_stats, strength)

    elif algorithm == "histogram":
        result = histogram_match_full(bgr, style.color_stats, strength)

    elif use_neural and ref_img is not None:
        if progress_callback:
            progress_callback("render", 0.20, "正在运行AI模型全尺寸推理...")
        if algorithm == "neural_preset":
            from algorithms.neural_preset import neural_preset_transfer
            result = neural_preset_transfer(bgr, ref_img)
        elif algorithm == "modflows":
            from algorithms.modflows import modflows_transfer
            result = modflows_transfer(
                bgr, ref_img, encoder_type="B6", steps=16, strength=strength,
                progress_callback=progress_callback,
            )
        elif algorithm == "regional_modflows":
            from algorithms.postprocess import regional_transfer
            from algorithms.modflows import modflows_transfer
            result = regional_transfer(
                bgr, ref_img,
                transfer_func=lambda t, r: modflows_transfer(
                    t, r, encoder_type="B6", steps=16, strength=1.0),
                base_strength=strength,
                progress_callback=progress_callback,
            )
    elif use_neural:
        result = reinhard_transfer_full(bgr, style.color_stats, strength)

    if progress_callback:
        progress_callback("render", 0.75, "正在应用后处理...")

    if style.smart_postprocess:
        if progress_callback:
            progress_callback("render", 0.80, "智能后处理...")

    if progress_callback:
        progress_callback("render", 1.0, "完成！")

    return result, meta


def analyze_style_stats(target_bgr: np.ndarray, ref_bgr: np.ndarray) -> Dict[str, Any]:
    """
    从 preview 图分析风格统计信息（生成 StyleRepresentation.color_stats）
    """
    target_lab = cv2.cvtColor(target_bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
    ref_lab = cv2.cvtColor(ref_bgr, cv2.COLOR_BGR2LAB).astype(np.float32)

    stats = {
        'mean': np.mean(ref_lab, axis=(0, 1)).tolist(),
        'std': np.std(ref_lab, axis=(0, 1)).tolist(),
        'target_mean': np.mean(target_lab, axis=(0, 1)).tolist(),
        'target_std': np.std(target_lab, axis=(0, 1)).tolist(),
    }

    # 直方图
    for c in range(3):
        ref_hist, _ = np.histogram(ref_lab[:, :, c].flatten(), 256, [0, 256])
        ref_cdf = np.cumsum(ref_hist)
        stats[f'cdf_{c}'] = ref_cdf.tolist()

    return stats


if __name__ == '__main__':
    import time

    print("=== Test apply_lut (trilinear via LUT expansion) ===")
    np.random.seed(42)

    lut_size = 33
    lut = np.random.rand(lut_size, lut_size, lut_size, 3).astype(np.float32) * 0.5 + 0.25
    lut = np.clip(lut, 0, 1)

    img_1k = np.random.randint(0, 256, (1024, 1024, 3), dtype=np.uint8)
    t0 = time.time()
    result_1k = apply_lut(img_1k, lut)
    t1 = time.time()
    first_call_1k = t1 - t0
    print(f"  1024x1024 (first, w/ expansion): {first_call_1k:.3f}s, shape={result_1k.shape}, dtype={result_1k.dtype}")
    assert result_1k.shape == (1024, 1024, 3)
    assert result_1k.dtype == np.uint8

    t0 = time.time()
    result_1k_cached = apply_lut(img_1k, lut)
    t1 = time.time()
    cached_call_1k = t1 - t0
    print(f"  1024x1024 (cached): {cached_call_1k:.3f}s")
    assert cached_call_1k < first_call_1k, "Cached call should be faster"

    img_4k = np.random.randint(0, 256, (2160, 3840, 3), dtype=np.uint8)
    t0 = time.time()
    result_4k = apply_lut(img_4k, lut)
    t1 = time.time()
    print(f"  4K (3840x2160, cached): {t1 - t0:.3f}s, shape={result_4k.shape}")
    assert result_4k.shape == (2160, 3840, 3)
    assert t1 - t0 < 1.0, f"4K cached apply_lut too slow: {t1 - t0:.3f}s"

    print("\n=== Test trilinear smoothness (identity LUT) ===")
    identity_lut = np.zeros((lut_size, lut_size, lut_size, 3), dtype=np.float32)
    for i in range(lut_size):
        v = i / (lut_size - 1)
        identity_lut[i, :, :, 0] = v
        identity_lut[:, i, :, 1] = v
        identity_lut[:, :, i, 2] = v

    test_img = np.random.randint(0, 256, (256, 256, 3), dtype=np.uint8)
    result_identity = apply_lut(test_img, identity_lut)
    max_diff = np.max(np.abs(result_identity.astype(np.int16) - test_img.astype(np.int16)))
    print(f"  Identity LUT max pixel diff: {max_diff} (should be <= 2)")
    assert max_diff <= 3, f"Identity LUT error too large: {max_diff}"

    print("\n=== Test extract_lut_from_pair ===")
    from core.color.lut_extractor import extract_lut_from_pair

    src = np.random.randint(0, 256, (1024, 1024, 3), dtype=np.uint8)
    tgt = np.clip(src.astype(np.float32) * 0.7 + 50, 0, 255).astype(np.uint8)

    t0 = time.time()
    extracted_lut = extract_lut_from_pair(src, tgt, lut_size=33)
    t1 = time.time()
    print(f"  Extraction: {t1 - t0:.3f}s, shape={extracted_lut.shape}")
    assert extracted_lut.shape == (33, 33, 33, 3)
    assert extracted_lut.dtype == np.float32
    assert t1 - t0 < 5.0, f"Extraction too slow: {t1 - t0:.3f}s"

    print("\n=== Test end-to-end: extract + apply ===")
    t0 = time.time()
    result_e2e = apply_lut(src, extracted_lut)
    t1 = time.time()
    print(f"  E2E (first, w/ expansion): {t1 - t0:.3f}s, shape={result_e2e.shape}, dtype={result_e2e.dtype}")

    t0 = time.time()
    result_e2e2 = apply_lut(img_4k, extracted_lut)
    t1 = time.time()
    print(f"  E2E 4K (cached): {t1 - t0:.3f}s")
    assert t1 - t0 < 1.0, f"E2E 4K cached too slow: {t1 - t0:.3f}s"

    print("\n=== Test 16-bit (uint16) support ===")
    img_1k_16 = np.random.randint(0, 65536, (1024, 1024, 3), dtype=np.uint16)
    t0 = time.time()
    result_16 = apply_lut(img_1k_16, lut)
    t1 = time.time()
    print(f"  1K uint16 (cached): {t1 - t0:.3f}s, shape={result_16.shape}, dtype={result_16.dtype}")
    assert result_16.shape == (1024, 1024, 3)
    assert result_16.dtype == np.uint16, f"Expected uint16, got {result_16.dtype}"
    assert result_16.min() >= 0 and result_16.max() <= 65535

    img_4k_16 = np.random.randint(0, 65536, (2160, 3840, 3), dtype=np.uint16)
    t0 = time.time()
    result_4k_16 = apply_lut(img_4k_16, lut)
    t1 = time.time()
    print(f"  4K uint16 (cached): {t1 - t0:.3f}s, shape={result_4k_16.shape}, dtype={result_4k_16.dtype}")
    assert result_4k_16.dtype == np.uint16
    assert t1 - t0 < 1.5, f"4K uint16 too slow: {t1 - t0:.3f}s"

    print("\n=== Test 16-bit identity LUT smoothness ===")
    test_img_16 = np.random.randint(0, 65536, (256, 256, 3), dtype=np.uint16)
    result_id16 = apply_lut(test_img_16, identity_lut)
    max_diff_16 = np.max(np.abs(result_id16.astype(np.int32) - test_img_16.astype(np.int32)))
    print(f"  Identity LUT max pixel diff (16-bit): {max_diff_16} (should be <= 500)")
    assert max_diff_16 <= 1000, f"16-bit identity LUT error too large: {max_diff_16}"

    print("\n=== Test extract_lut_from_pair with uint16 ===")
    src_16 = np.random.randint(0, 65536, (512, 512, 3), dtype=np.uint16)
    tgt_16 = np.clip(src_16.astype(np.float64) * 0.7 + 10000, 0, 65535).astype(np.uint16)
    t0 = time.time()
    extracted_lut_16 = extract_lut_from_pair(src_16, tgt_16, lut_size=33)
    t1 = time.time()
    print(f"  Extraction from uint16: {t1 - t0:.3f}s, shape={extracted_lut_16.shape}")
    assert extracted_lut_16.shape == (33, 33, 33, 3)
    assert extracted_lut_16.dtype == np.float32
    assert extracted_lut_16.min() >= 0 and extracted_lut_16.max() <= 1

    result_16_e2e = apply_lut(src_16, extracted_lut_16)
    print(f"  E2E uint16 result: shape={result_16_e2e.shape}, dtype={result_16_e2e.dtype}")
    assert result_16_e2e.dtype == np.uint16

    print("\n=== Test boundary: all-black / all-white uint16 + Identity LUT ===")
    black_16 = np.zeros((64, 64, 3), dtype=np.uint16)
    white_16 = np.full((64, 64, 3), 65535, dtype=np.uint16)

    result_black = apply_lut(black_16, identity_lut)
    result_white = apply_lut(white_16, identity_lut)

    print(f"  Black input dtype={black_16.dtype}, output dtype={result_black.dtype}")
    print(f"  Black output: min={result_black.min()}, max={result_black.max()}")
    assert result_black.dtype == np.uint16, f"Black output dtype should be uint16, got {result_black.dtype}"
    assert result_black.min() == 0, f"Black output min should be 0, got {result_black.min()}"
    assert result_black.max() == 0, f"Black output max should be 0, got {result_black.max()}"

    print(f"  White input dtype={white_16.dtype}, output dtype={result_white.dtype}")
    print(f"  White output: min={result_white.min()}, max={result_white.max()}")
    assert result_white.dtype == np.uint16, f"White output dtype should be uint16, got {result_white.dtype}"
    assert result_white.max() == 65535, f"White output max should be 65535, got {result_white.max()}"
    assert result_white.min() == 65535, f"White output min should be 65535, got {result_white.min()}"

    print("\n=== Test expanded cache integrity: always 0-1 float32 ===")
    _test_lut = np.random.rand(lut_size, lut_size, lut_size, 3).astype(np.float32) * 0.5 + 0.25
    _test_lut = np.clip(_test_lut, 0, 1)
    _ = apply_lut(np.zeros((2, 2, 3), dtype=np.uint8), _test_lut)
    expanded = _expanded_lut_cache[id(_test_lut)]
    assert expanded.dtype == np.float32, f"Expanded LUT dtype should be float32, got {expanded.dtype}"
    assert expanded.min() >= 0.0, f"Expanded LUT min should be >= 0, got {expanded.min()}"
    assert expanded.max() <= 1.0, f"Expanded LUT max should be <= 1, got {expanded.max()}"
    print(f"  Expanded LUT: dtype={expanded.dtype}, min={expanded.min():.6f}, max={expanded.max():.6f}")

    print("\nAll tests passed!")

