import os
import json
import numpy as np


def create_style_dict():
    return {
        "name": "",
        "camera": "",
        "tone_curve": [],
        "color_matrix": [],
        "lut3d_path": "",
        "skin_protection": {},
        "highlight_rolloff": {},
        "shadow_tint": {},
        "style_embedding": [],
    }


def save_style_dict(style_dict, dir_path):
    os.makedirs(dir_path, exist_ok=True)
    path = os.path.join(dir_path, "style.ccs")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(style_dict, f, ensure_ascii=False, indent=2)
    return path


def save_lut_as_npy(lut_3d, dir_path):
    os.makedirs(dir_path, exist_ok=True)
    path = os.path.join(dir_path, "lut_global.npy")
    if os.path.exists(path):
        os.remove(path)
    np.save(path, lut_3d)
    return path


def save_lut_as_cube(lut_3d, dir_path):
    os.makedirs(dir_path, exist_ok=True)
    path = os.path.join(dir_path, "lut_global.cube")
    size = lut_3d.shape[0]
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"LUT_3D_SIZE {size}\n")
        f.write("DOMAIN_MIN 0.0 0.0 0.0\n")
        f.write("DOMAIN_MAX 1.0 1.0 1.0\n")
        for b in range(size):
            for g in range(size):
                for r in range(size):
                    c = lut_3d[r, g, b]
                    f.write(f"{c[0]:.6f} {c[1]:.6f} {c[2]:.6f}\n")
    return path
