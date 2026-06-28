import cv2
import numpy as np


def reconstruct_clean_skin(result_img, source_img=None, mask_skin=None, strength=0.7,
                            ref_a_mean=None, ref_b_mean=None):
    h, w = result_img.shape[:2]

    res_f = result_img.astype(np.float32) / 255.0
    res_lab = cv2.cvtColor(res_f, cv2.COLOR_BGR2Lab)

    L_res, a_res, b_res = cv2.split(res_lab)

    if mask_skin is None:
        mask_2d = np.ones((h, w), dtype=np.float32)
    elif mask_skin.ndim == 2:
        mask_2d = mask_skin.astype(np.float32)
    else:
        mask_2d = mask_skin[:, :, 0].astype(np.float32)

    mask_soft = cv2.GaussianBlur(mask_2d, (0, 0), sigmaX=6, sigmaY=6)
    mask_soft = np.clip(mask_soft, 0, 1)
    skin_region = mask_soft > 0.2

    if source_img is not None:
        src_f = source_img.astype(np.float32) / 255.0
        src_lab = cv2.cvtColor(src_f, cv2.COLOR_BGR2Lab)
        _, a_src, b_src = cv2.split(src_lab)
        src_skin_a_mean = float(a_src[mask_soft > 0.2].mean()) if skin_region.sum() > 0 else float(a_src.mean())
        src_skin_b_mean = float(b_src[mask_soft > 0.2].mean()) if skin_region.sum() > 0 else float(b_src.mean())
        res_skin_a_mean = float(a_res[mask_soft > 0.2].mean()) if skin_region.sum() > 0 else float(a_res.mean())
        res_skin_b_mean = float(b_res[mask_soft > 0.2].mean()) if skin_region.sum() > 0 else float(b_res.mean())
        target_a = src_skin_a_mean * 0.3 + res_skin_a_mean * 0.7
        target_b = src_skin_b_mean * 0.3 + res_skin_b_mean * 0.7
    elif ref_a_mean is not None and ref_b_mean is not None:
        target_a = ref_a_mean
        target_b = ref_b_mean
    else:
        target_a = a_res.mean()
        target_b = b_res.mean()

    alpha = strength * mask_soft
    a_clean = a_res * (1.0 - alpha) + target_a * alpha
    b_clean = b_res * (1.0 - alpha) + target_b * alpha

    a_final = a_res * (1.0 - mask_soft) + a_clean * mask_soft
    b_final = b_res * (1.0 - mask_soft) + b_clean * mask_soft

    protected_lab = cv2.merge([L_res, a_final, b_final])

    protected_bgr_f = cv2.cvtColor(protected_lab, cv2.COLOR_Lab2BGR)
    protected_bgr = (protected_bgr_f * 255).astype(np.uint8)

    mask_3ch = mask_soft[:, :, np.newaxis]

    result_final = (result_img.astype(np.float32) * (1.0 - mask_3ch) +
                    protected_bgr.astype(np.float32) * mask_3ch)
    result_final = result_final.astype(np.uint8)

    if skin_region.sum() > 100:
        print(f"[Skin Reconstruct] target a={target_a:.1f} b={target_b:.1f} -> "
              f"convergence strength={strength}, skin_area={skin_region.sum():.0f}px")

    return result_final, mask_soft


def colorize_highlights(result_img, hair_mask=None, mask_skin=None):
    h, w = result_img.shape[:2]

    res_lab = cv2.cvtColor(result_img, cv2.COLOR_BGR2LAB).astype(np.float32)
    L, a, b = cv2.split(res_lab)

    if hair_mask is not None:
        if hair_mask.ndim == 2:
            skin_mask = hair_mask.astype(np.float32)
        else:
            skin_mask = hair_mask[:, :, 0].astype(np.float32)
    elif mask_skin is not None:
        if mask_skin.ndim == 2:
            skin_mask = mask_skin.astype(np.float32)
        else:
            skin_mask = mask_skin[:, :, 0].astype(np.float32)
    else:
        skin_mask = np.zeros((h, w), dtype=np.float32)

    highlight_mask = (L > 190).astype(np.float32)
    non_skin = highlight_mask * (1.0 - np.clip(skin_mask, 0, 1))
    non_skin = cv2.GaussianBlur(non_skin, (0, 0), sigmaX=6, sigmaY=6)
    non_skin = np.clip(non_skin, 0, 1)

    if non_skin.max() < 0.01:
        print("[Highlights] no non-skin highlight pixels (L>190) found")
        return result_img

    a_colored = a - non_skin * 12.0
    b_colored = b - non_skin * 12.0

    colored_lab = cv2.merge([L, a_colored, b_colored]).astype(np.uint8)
    colored_bgr = cv2.cvtColor(colored_lab, cv2.COLOR_LAB2BGR)

    print(f"[Highlights] colored {non_skin.sum():.0f} non-skin highlight pixels (L>190) -> cyan-blue")
    return colored_bgr


def preserve_makeup(source_img, result_img, reference_img=None, mask_skin=None,
                    lip_mask=None, saturation_boost=1.4, v_ref_avg=None):
    h, w = source_img.shape[:2]

    if lip_mask is not None:
        if lip_mask.ndim == 2:
            lip_2d = lip_mask.astype(np.float32)
        else:
            lip_2d = lip_mask[:, :, 0].astype(np.float32)
    else:
        lip_2d = np.zeros((h, w), dtype=np.float32)

    if lip_2d.sum() < 30:
        print(f"[Makeup] no lip pixels detected (pixels={lip_2d.sum():.0f})")
        return result_img

    res_hsv = cv2.cvtColor(result_img, cv2.COLOR_BGR2HSV).astype(np.float32)

    if v_ref_avg is None:
        if reference_img is not None:
            ref_hsv = cv2.cvtColor(reference_img, cv2.COLOR_BGR2HSV).astype(np.float32)
            v_ref_avg = float(ref_hsv[:, :, 2].mean())
        else:
            v_ref_avg = float(res_hsv[:, :, 2].mean())

    H_res, S_res, V_res = [ch.copy() for ch in cv2.split(res_hsv)]

    lip_mask_soft = cv2.GaussianBlur(lip_2d, (0, 0), sigmaX=3, sigmaY=3)
    lip_mask_soft = np.clip(lip_mask_soft, 0, 1)

    S_boosted = S_res * (1.0 + lip_mask_soft * (saturation_boost - 1.0))
    S_boosted = np.clip(S_boosted, 0, 255)

    v_ratio = min(v_ref_avg / max(V_res.mean(), 1.0), 1.3)
    V_adjusted = V_res * (1.0 + lip_mask_soft * (v_ratio - 1.0))
    V_adjusted = np.clip(V_adjusted, 0, 255)

    enhanced_hsv = cv2.merge([
        H_res.astype(np.uint8),
        S_boosted.astype(np.uint8),
        V_adjusted.astype(np.uint8)
    ])
    enhanced_bgr = cv2.cvtColor(enhanced_hsv, cv2.COLOR_HSV2BGR)

    lip_mask_3ch = lip_mask_soft[:, :, np.newaxis]
    result_final = (result_img.astype(np.float32) * (1.0 - lip_mask_3ch) +
                    enhanced_bgr.astype(np.float32) * lip_mask_3ch)
    result_final = np.clip(result_final, 0, 255).astype(np.uint8)

    print(f"[Makeup] lipstick preserved & boosted: pixels={lip_2d.sum():.0f}, "
          f"saturation x{saturation_boost}, V_ref={v_ref_avg:.0f}")
    return result_final
