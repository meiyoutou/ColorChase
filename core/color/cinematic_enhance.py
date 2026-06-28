import cv2
import numpy as np


def reshape_lighting(result_img, reference_img, strength=0.6):
    result_lab = cv2.cvtColor(result_img, cv2.COLOR_BGR2LAB).astype(np.float32)
    ref_lab = cv2.cvtColor(reference_img, cv2.COLOR_BGR2LAB).astype(np.float32)

    L_res, a_res, b_res = cv2.split(result_lab)
    L_ref = ref_lab[:, :, 0]

    L_res_flat = L_res.flatten()
    L_ref_resized = cv2.resize(L_ref, (L_res.shape[1], L_res.shape[0]), interpolation=cv2.INTER_LINEAR)
    L_ref_flat = L_ref_resized.flatten()

    src_sorted = np.sort(L_res_flat)
    ref_sorted = np.sort(L_ref_flat)

    L_matched = np.interp(L_res_flat, src_sorted, ref_sorted).reshape(L_res.shape).astype(np.float32)

    L_final = L_res * (1.0 - strength) + L_matched * strength
    L_final = np.clip(L_final, 0, 255)

    high_mask = (L_final > 180).astype(np.float32)
    shadow_mask = (L_final < 80).astype(np.float32)

    a_split = a_res - high_mask * 8.0 + shadow_mask * (-4.0)
    b_split = b_res - high_mask * 6.0 + shadow_mask * 2.0

    a_final = a_res * (1.0 - strength) + a_split * strength
    b_final = b_res * (1.0 - strength) + b_split * strength
    a_final = np.clip(a_final, 0, 255)
    b_final = np.clip(b_final, 0, 255)

    enhanced_lab = cv2.merge([L_final, a_final, b_final]).astype(np.uint8)
    return cv2.cvtColor(enhanced_lab, cv2.COLOR_LAB2BGR)


def add_film_grain(img, intensity=0.008):
    h, w = img.shape[:2]

    result_lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.float32)
    L = result_lab[:, :, 0]

    L_max = L.max()
    if L_max > 0:
        L_norm = L / L_max
    else:
        L_norm = L

    midtone_weight = 2.0 * L_norm * (1.0 - L_norm)
    midtone_weight = np.clip(midtone_weight, 0, 1)

    noise = np.random.normal(0, intensity * 255, (h, w)).astype(np.float32)
    noise *= midtone_weight

    L_grain = np.clip(L + noise, 0, 255)
    result_lab[:, :, 0] = L_grain

    result_bgr = cv2.cvtColor(result_lab.astype(np.uint8), cv2.COLOR_LAB2BGR)
    return result_bgr