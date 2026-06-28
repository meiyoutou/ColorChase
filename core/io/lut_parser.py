import os
import re
import struct
import numpy as np


def parse_lut_file(file_path: str) -> np.ndarray:
    if not os.path.exists(file_path):
        raise ValueError(f"LUT file not found: {file_path}")

    ext = os.path.splitext(file_path)[1].lower()

    with open(file_path, 'rb') as f:
        raw_bytes = f.read()

    text = raw_bytes.decode('utf-8', errors='replace')
    text = text.replace('\r\n', '\n').replace('\r', '\n')

    if ext == '.cube':
        return _parse_cube(text, file_path)
    elif ext == '.3dl':
        return _parse_3dl(text, file_path)
    elif ext == '.csp':
        return _parse_csp(text, file_path)
    elif ext == '.spi3d':
        return _parse_spi3d(text, file_path)
    elif ext == '.spi1d':
        return _parse_spi1d(text, file_path)
    elif ext == '.lut':
        return _parse_lut(text, file_path)
    elif ext == '.pf3':
        return _parse_pf3(raw_bytes, file_path)
    elif ext == '.xmp':
        from core.io.xmp_baker import parse_xmp_preset, bake_xmp_to_lut
        params = parse_xmp_preset(file_path)
        return bake_xmp_to_lut(params)
    else:
        raise ValueError(f"Unsupported LUT format: {ext}")


def _parse_float_line(line):
    parts = line.strip().split()
    if len(parts) >= 3:
        return [float(x) for x in parts[:3]]
    return None


def _build_identity_3d(size):
    lut = np.zeros((size, size, size, 3), dtype=np.float32)
    for i in range(size):
        v = i / (size - 1)
        lut[i, :, :, 0] = v
        lut[:, i, :, 1] = v
        lut[:, :, i, 2] = v
    return lut


def _parse_cube(text, file_path):
    title = None
    lut_3d_size = None
    domain_min = np.array([0.0, 0.0, 0.0], dtype=np.float32)
    domain_max = np.array([1.0, 1.0, 1.0], dtype=np.float32)
    data_lines = []

    for line in text.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        upper = line.upper()
        if upper.startswith('TITLE'):
            title = line.split(maxsplit=1)[1].strip('" ')
            continue

        if upper.startswith('DOMAIN_MIN'):
            parts = line.split()
            if len(parts) >= 4:
                domain_min = np.array([float(x) for x in parts[1:4]], dtype=np.float32)
            continue

        if upper.startswith('DOMAIN_MAX'):
            parts = line.split()
            if len(parts) >= 4:
                domain_max = np.array([float(x) for x in parts[1:4]], dtype=np.float32)
            continue

        if upper.startswith('LUT_3D_SIZE'):
            parts = line.split()
            if len(parts) >= 2:
                lut_3d_size = int(parts[1])
            continue

        if upper.startswith('LUT_1D_SIZE'):
            raise ValueError("1D .cube LUTs should be converted with .spi1d extension")

        vals = _parse_float_line(line)
        if vals is not None:
            data_lines.append(vals)

    if lut_3d_size is None:
        raise ValueError("Missing LUT_3D_SIZE in .cube file")

    expected = lut_3d_size ** 3
    if len(data_lines) < expected:
        raise ValueError(
            f"Expected {expected} entries for size {lut_3d_size}, got {len(data_lines)}"
        )

    lut = np.array(data_lines[:expected], dtype=np.float32)
    lut = lut.reshape(lut_3d_size, lut_3d_size, lut_3d_size, 3)

    domain_range = domain_max - domain_min
    if np.any(domain_range != np.array([1.0, 1.0, 1.0])):
        lut = (lut - domain_min) / np.where(domain_range == 0, 1.0, domain_range)

    lut = np.clip(lut, 0.0, 1.0)
    return lut


def _parse_3dl(text, file_path):
    size = None
    data_lines = []

    for line in text.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        vals = _parse_float_line(line)
        if vals is not None:
            data_lines.append(vals)

    if not data_lines:
        raise ValueError("No data found in .3dl file")

    total = len(data_lines)
    size_f = round(total ** (1.0 / 3.0))
    if size_f ** 3 != total:
        size_f = round((total + 1) ** (1.0 / 3.0))
        if size_f ** 3 == total:
            pass

    for candidate in [size_f, 17, 33, 65]:
        if candidate ** 3 == total:
            size = candidate
            break

    if size is None:
        size = 33
        data_lines = data_lines[:size ** 3]

    if size ** 3 > len(data_lines):
        raise ValueError(f"Insufficient data for inferred size {size}: got {len(data_lines)}")

    lut = np.array(data_lines[:size ** 3], dtype=np.float32)

    max_val = lut.max()
    if max_val > 1.01:
        if max_val <= 255.1:
            lut /= 255.0
        elif max_val <= 1023.1:
            lut /= 1023.0
        elif max_val <= 4095.1:
            lut /= 4095.0
        elif max_val <= 65535.1:
            lut /= 65535.0
        else:
            lut /= max_val

    lut = lut.reshape(size, size, size, 3)
    lut = np.clip(lut, 0.0, 1.0)
    return lut


def _srgb_to_linear_single(c):
    c = c / 255.0
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb_single(c):
    if c <= 0.0031308:
        return c * 12.92
    return 1.055 * (c ** (1.0 / 2.4)) - 0.055


def _rgb_to_lab_single(r, g, b):
    r_lin = _srgb_to_linear_single(r)
    g_lin = _srgb_to_linear_single(g)
    b_lin = _srgb_to_linear_single(b)

    x = r_lin * 0.4124564 + g_lin * 0.3575761 + b_lin * 0.1804375
    y = r_lin * 0.2126729 + g_lin * 0.7151522 + b_lin * 0.0721750
    z = r_lin * 0.0193339 + g_lin * 0.1191920 + b_lin * 0.9503041

    xn, yn, zn = 0.95047, 1.0, 1.08883

    def f(t):
        if t > 0.008856:
            return t ** (1.0 / 3.0)
        return 7.787 * t + 16.0 / 116.0

    fy = f(y / yn)
    L = 116.0 * fy - 16.0
    a = 500.0 * (f(x / xn) - fy)
    b = 200.0 * (fy - f(z / zn))

    return L, a, b


def _lab_to_rgb_single(L, a, b):
    xn, yn, zn = 0.95047, 1.0, 1.08883

    fy = (L + 16.0) / 116.0
    fx = fy + a / 500.0
    fz = fy - b / 200.0

    def f_inv(t):
        if t > 0.206897:
            return t ** 3.0
        return (t - 16.0 / 116.0) / 7.787

    x = f_inv(fx) * xn
    y = f_inv(fy) * yn
    z = f_inv(fz) * zn

    r_lin = x * 3.2404542 + y * -1.5371385 + z * -0.4985314
    g_lin = x * -0.9692660 + y * 1.8760108 + z * 0.0415560
    b_lin = x * 0.0556434 + y * -0.2040259 + z * 1.0572252

    r_lin = max(0.0, min(1.0, r_lin))
    g_lin = max(0.0, min(1.0, g_lin))
    b_lin = max(0.0, min(1.0, b_lin))

    r = _linear_to_srgb_single(r_lin) * 255.0
    g = _linear_to_srgb_single(g_lin) * 255.0
    b = _linear_to_srgb_single(b_lin) * 255.0

    return max(0.0, min(255.0, r)), max(0.0, min(255.0, g)), max(0.0, min(255.0, b))


def _parse_pf3(raw_bytes, file_path):
    color_rgb = None
    density = 1.0

    text = raw_bytes.decode('utf-8', errors='replace').replace('\r\n', '\n')

    if text.strip().startswith('<') or text.strip().startswith('<?xml'):
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(text)

            for elem in root.iter():
                tag = elem.tag.lower()
                if tag in ('color', 'pf3'):
                    r = elem.get('r') or elem.get('R') or elem.get('red') or elem.get('Red')
                    if r is not None:
                        g = elem.get('g') or elem.get('G') or elem.get('green') or elem.get('Green') or 0
                        b = elem.get('b') or elem.get('B') or elem.get('blue') or elem.get('Blue') or 0
                        color_rgb = (int(float(r)), int(float(g)), int(float(b)))
                        d = elem.get('density') or elem.get('Density')
                        if d is not None:
                            density = float(d)
                        break

            if color_rgb is None:
                for elem in root.iter():
                    r = elem.get('r') or elem.get('R') or elem.get('red') or elem.get('Red')
                    if r is not None:
                        g = elem.get('g') or elem.get('G') or elem.get('green') or elem.get('Green') or '0'
                        b = elem.get('b') or elem.get('B') or elem.get('blue') or elem.get('Blue') or '0'
                        try:
                            color_rgb = (int(float(r)), int(float(g)), int(float(b)))
                            d = elem.get('density') or elem.get('Density')
                            if d is not None:
                                density = float(d)
                            break
                        except (ValueError, TypeError):
                            continue
        except Exception as e:
            raise ValueError(f"Failed to parse .pf3 XML: {e}")
    else:
        color_rgb = _try_parse_binary_pf3(raw_bytes)

    if color_rgb is None:
        lines = text.strip().split('\n')
        for line in lines:
            line = line.strip()
            if not line:
                continue
            parts = line.replace(',', ' ').split()
            nums = []
            for p in parts:
                try:
                    nums.append(float(p))
                except ValueError:
                    continue
            if len(nums) >= 3:
                color_rgb = (
                    int(min(255, max(0, nums[0]))),
                    int(min(255, max(0, nums[1]))),
                    int(min(255, max(0, nums[2]))),
                )
                if len(nums) >= 4:
                    density = float(nums[3])
                break

    if color_rgb is None:
        color_rgb = _try_parse_binary_pf3(raw_bytes)

    if color_rgb is None:
        try:
            lut = _parse_cube(text, file_path)
            center_idx = lut.shape[0] // 2
            c = lut[center_idx, center_idx, center_idx]
            color_rgb = (int(c[0] * 255), int(c[1] * 255), int(c[2] * 255))
        except Exception:
            pass

    if color_rgb is None:
        raise ValueError("Could not extract color from .pf3 file")

    density = max(0.0, min(1.0, density))
    alpha = density * 0.5

    r, g, b = color_rgb
    L_c, a_c, b_c = _rgb_to_lab_single(r, g, b)

    size_3d = 33
    lut = np.zeros((size_3d, size_3d, size_3d, 3), dtype=np.float32)
    grid = np.linspace(0, 255, size_3d)

    for ir in range(size_3d):
        for ig in range(size_3d):
            for ib in range(size_3d):
                gr, gg, gb = grid[ir], grid[ig], grid[ib]
                L_i, a_i, b_i = _rgb_to_lab_single(gr, gg, gb)

                L_new = L_i * (1.0 - alpha) + L_c * alpha
                a_new = a_i * (1.0 - alpha) + a_c * alpha
                b_new = b_i * (1.0 - alpha) + b_c * alpha

                out_r, out_g, out_b = _lab_to_rgb_single(L_new, a_new, b_new)
                lut[ir, ig, ib, 0] = out_r / 255.0
                lut[ir, ig, ib, 1] = out_g / 255.0
                lut[ir, ig, ib, 2] = out_b / 255.0

    lut = np.clip(lut, 0.0, 1.0)
    return lut


def _try_parse_binary_pf3(raw_bytes):
    try:
        data = np.frombuffer(raw_bytes, dtype=np.float32)
        if len(data) >= 3:
            return (
                int(min(255, max(0, data[0] * 255))),
                int(min(255, max(0, data[1] * 255))),
                int(min(255, max(0, data[2] * 255))),
            )
    except Exception:
        pass

    try:
        data = np.frombuffer(raw_bytes, dtype=np.float64)
        if len(data) >= 3:
            return (
                int(min(255, max(0, data[0] * 255))),
                int(min(255, max(0, data[1] * 255))),
                int(min(255, max(0, data[2] * 255))),
            )
    except Exception:
        pass

    return None


def _parse_csp(text, file_path):
    size = None
    in_start = None
    out_start = None
    data_lines = []

    for line in text.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        upper = line.upper()
        if 'PRE-LUT' in upper or 'POST-LUT' in upper:
            continue

        if upper.startswith('2') or upper.startswith('3'):
            parts = line.split()
            try:
                val = int(parts[0])
                if size is None and 3 <= val <= 256:
                    size = val
                    continue
            except (ValueError, IndexError):
                pass

        vals = _parse_float_line(line)
        if vals is not None:
            data_lines.append(vals)

    if not data_lines:
        raise ValueError("No numeric data found in .csp file")

    total = len(data_lines)
    if size is None:
        size_f = round(total ** (1.0 / 3.0))
        for c in [size_f, 17, 33, 65]:
            if c ** 3 == total:
                size = c
                break
        if size is None:
            size = 33

    count = size ** 3
    if count > len(data_lines):
        raise ValueError(f"Insufficient data for size {size} in .csp: got {len(data_lines)}")

    lut = np.array(data_lines[:count], dtype=np.float32)

    max_val = lut.max()
    if max_val > 1.01:
        if max_val <= 255.1:
            lut /= 255.0
        elif max_val <= 4095.1:
            lut /= 4095.0
        elif max_val <= 65535.1:
            lut /= 65535.0
        else:
            lut /= max_val

    lut = lut.reshape(size, size, size, 3)
    lut = np.clip(lut, 0.0, 1.0)
    return lut


def _parse_spi3d(text, file_path):
    size = None
    data_lines = []

    for line in text.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        if line.upper().startswith('SPI') or line.upper().startswith('VERSION'):
            continue

        parts = line.split()
        if len(parts) == 1 and size is None:
            try:
                v = int(parts[0])
                if 3 <= v <= 256:
                    size = v
                    continue
            except ValueError:
                pass

        vals = _parse_float_line(line)
        if vals is not None:
            data_lines.append(vals)

    if not data_lines:
        raise ValueError("No data found in .spi3d file")

    total = len(data_lines)
    if size is None:
        size_f = round(total ** (1.0 / 3.0))
        for c in [size_f, 17, 33, 65]:
            if c ** 3 == total:
                size = c
                break
        if size is None:
            size = 33

    count = size ** 3
    if count > len(data_lines):
        raise ValueError(f"Insufficient data for size {size} in .spi3d: got {len(data_lines)}")

    lut = np.array(data_lines[:count], dtype=np.float32)
    lut = lut.reshape(size, size, size, 3)
    lut = np.clip(lut, 0.0, 1.0)
    return lut


def _parse_spi1d(text, file_path):
    components = [[], [], []]
    current_comp = -1

    for line in text.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        upper = line.upper()
        if upper.startswith('VERSION') or upper.startswith('FROM') or upper.startswith('TO') or upper.startswith('COMPONENTS'):
            continue

        if upper.startswith('LENGTH'):
            current_comp += 1
            continue

        parts = line.split()
        try:
            v = float(parts[0])
            if 0 <= current_comp < 3:
                components[current_comp].append(v)
        except (ValueError, IndexError):
            pass

    max_len = max(len(c) for c in components)
    if max_len == 0:
        raise ValueError("No data found in .spi1d file")

    for i in range(3):
        if len(components[i]) == 0:
            x = np.linspace(0, 1, max_len)
            components[i] = x.tolist()
        elif len(components[i]) < max_len:
            x = np.linspace(0, len(components[i]) - 1, max_len)
            components[i] = np.interp(x, np.arange(len(components[i])), components[i]).tolist()

    size_3d = 33
    grid = np.linspace(0, 1, size_3d)
    rg, gg, bg = np.meshgrid(grid, grid, grid, indexing='ij')

    r_idx = rg.ravel()
    g_idx = gg.ravel()
    b_idx = bg.ravel()

    r_len = len(components[0])
    g_len = len(components[1])
    b_len = len(components[2])

    r_scaled = np.interp(r_idx, np.linspace(0, 1, r_len), components[0])
    g_scaled = np.interp(g_idx, np.linspace(0, 1, g_len), components[1])
    b_scaled = np.interp(b_idx, np.linspace(0, 1, b_len), components[2])

    lut = np.stack([r_scaled, g_scaled, b_scaled], axis=-1).astype(np.float32)
    lut = lut.reshape(size_3d, size_3d, size_3d, 3)
    lut = np.clip(lut, 0.0, 1.0)
    return lut


def _parse_lut(text, file_path):
    size = None
    data_lines = []

    for line in text.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        vals = _parse_float_line(line)
        if vals is not None:
            data_lines.append(vals)

    if not data_lines:
        raise ValueError("No data found in .lut file")

    total = len(data_lines)
    for candidate in [17, 33, 65]:
        if candidate ** 3 == total:
            size = candidate
            break

    if size is None:
        size_f = round(total ** (1.0 / 3.0))
        for c in [size_f, 17, 33, 65]:
            if c ** 3 <= total:
                size = c
                break
        if size is None:
            size = 33

    count = size ** 3
    if count > len(data_lines):
        raise ValueError(f"Insufficient data for inferred size {size} in .lut: got {len(data_lines)}")

    lut = np.array(data_lines[:count], dtype=np.float32)

    max_val = lut.max()
    if max_val > 1.01:
        if max_val <= 255.1:
            lut /= 255.0
        elif max_val <= 1023.1:
            lut /= 1023.0
        elif max_val <= 4095.1:
            lut /= 4095.0
        elif max_val <= 65535.1:
            lut /= 65535.0
        else:
            lut /= max_val

    lut = lut.reshape(size, size, size, 3)
    lut = np.clip(lut, 0.0, 1.0)
    return lut