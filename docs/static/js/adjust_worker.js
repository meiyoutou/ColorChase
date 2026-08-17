const SRGB_LUT = new Float32Array(256);
for (let i = 0; i < 256; i++) {
    const c = i / 255;
    SRGB_LUT[i] = c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
}

function linearToSrgb(c) {
    if (c <= 0) return 0;
    if (c >= 1) return 255;
    return Math.round((c <= 0.0031308 ? 12.92 * c : 1.055 * Math.pow(c, 1 / 2.4) - 0.055) * 255);
}

const D6 = 6 / 29, D62 = D6 * D6, D63 = D6 * D6 * D6;
const INV3D62 = 1 / (3 * D62), FOUR29 = 4 / 29;

function xyzF(t) {
    return t > D63 ? Math.cbrt(t) : t * INV3D62 + FOUR29;
}

function xyzFInv(t) {
    return t > D6 ? t * t * t : 3 * D62 * (t - FOUR29);
}

function rgbToLab(r, g, b) {
    const x = 0.4124564 * SRGB_LUT[r] + 0.3575761 * SRGB_LUT[g] + 0.1804375 * SRGB_LUT[b];
    const y = 0.2126729 * SRGB_LUT[r] + 0.7151522 * SRGB_LUT[g] + 0.0721750 * SRGB_LUT[b];
    const z = 0.0193339 * SRGB_LUT[r] + 0.1191920 * SRGB_LUT[g] + 0.9503041 * SRGB_LUT[b];
    const fy = xyzF(y);
    const L = 116 * fy - 16;
    const A = 500 * (xyzF(x / 0.95047) - fy) + 128;
    const B = 200 * (fy - xyzF(z / 1.08883)) + 128;
    return [L * 2.55, A, B];
}

function labToRgb(L, A, B) {
    const fy = (L / 2.55 + 16) / 116;
    const fx = (A - 128) / 500 + fy;
    const fz = fy - (B - 128) / 200;
    const x = 0.95047 * xyzFInv(fx);
    const y = xyzFInv(fy);
    const z = 1.08883 * xyzFInv(fz);
    const rl =  3.2404542 * x - 1.5371385 * y - 0.4985314 * z;
    const gl = -0.9692660 * x + 1.8760108 * y + 0.0415560 * z;
    const bl =  0.0556434 * x - 0.2040259 * y + 1.0572252 * z;
    return [linearToSrgb(rl), linearToSrgb(gl), linearToSrgb(bl)];
}

function getMeanL(rArr, gArr, bArr, N) {
    let sum = 0;
    for (let i = 0; i < N; i++) {
        sum += 0.299 * rArr[i] + 0.587 * gArr[i] + 0.114 * bArr[i];
    }
    return sum / (N * 255);
}

function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

self.onmessage = function (e) {
    const od = e.data.originalData;
    const sd = e.data.stylizedData;
    const p = e.data.params;
    const N = od.length >> 2;

    if (N === 0) { self.postMessage({ buffer: new Uint8ClampedArray().buffer }); return; }

    const alpha  = p.intensity / 100;
    const expo   = (p.exposure - 100) * 0.02;
    const cont   = (p.contrast - 100) * 0.005;
    const hl     = (p.highlight - 100) * 0.02;
    const sh     = (p.shadow - 100) * 0.02;
    const vib    = (p.vibrance - 100) * 0.005;

    const oR = new Float32Array(N), oG = new Float32Array(N), oB = new Float32Array(N);
    const sR = new Float32Array(N), sG = new Float32Array(N), sB = new Float32Array(N);
    for (let i = 0, j = 0; i < od.length; i += 4, j++) {
        oR[j] = od[i]; oG[j] = od[i + 1]; oB[j] = od[i + 2];
        sR[j] = sd[i]; sG[j] = sd[i + 1]; sB[j] = sd[i + 2];
    }

    const noTone = expo === 0 && cont === 0 && hl === 0 && sh === 0;
    const noVib  = vib === 0;
    const fastPath = alpha === 1.0 && noTone && noVib;

    const out = new Uint8ClampedArray(N << 2);
    if (fastPath) {
        out.set(sd);
        self.postMessage({ buffer: out.buffer }, [out.buffer]);
        return;
    }

    let lMean = 0;
    if (cont < 0) lMean = getMeanL(oR, oG, oB, N);

    const oma = 1 - alpha;

    for (let p = 0; p < N; p++) {
        let R, G, B;
        if (alpha !== 1.0) {
            R = oR[p] * oma + sR[p] * alpha;
            G = oG[p] * oma + sG[p] * alpha;
            B = oB[p] * oma + sB[p] * alpha;
            R = clamp(R, 0, 255);
            G = clamp(G, 0, 255);
            B = clamp(B, 0, 255);
            R = Math.round(R); G = Math.round(G); B = Math.round(B);
        } else {
            R = sR[p]; G = sG[p]; B = sB[p];
        }

        const Lab = rgbToLab(R, G, B);
        let L = Lab[0], A = Lab[1], Cb = Lab[2];

        if (expo !== 0) {
            L = clamp(L + expo * 100, 0, 255);
        }

        if (!noTone) {
            const ln = L / 255;
            let lo = ln;

            if (cont > 0) {
                const d = ln - 0.5;
                lo = ln + cont * 0.8 * d * (1 - d * d * 4);
            } else if (cont < 0) {
                lo = ln * (1 + cont) + lMean * (-cont);
            }

            if (hl !== 0) {
                const mh = clamp((ln - 0.5) * 2, 0, 1);
                lo += hl * 0.15 * mh * mh;
            }
            if (sh !== 0) {
                const ms = clamp((0.5 - ln) * 2, 0, 1);
                lo += sh * 0.15 * ms * ms;
            }

            L = clamp(lo * 255, 0, 255);
        }

        if (!noVib) {
            const scale = 1 + vib;
            const ad = A - 128, bd = Cb - 128;
            const sat = Math.hypot(ad, bd) + 1e-8;
            const satRatio = Math.min(1 / (sat + 1), 1);
            const vs = scale * (1 - satRatio) + satRatio;
            A = clamp(ad * vs + 128, 0, 255);
            Cb = clamp(bd * vs + 128, 0, 255);
        }

        const rgb = labToRgb(L, A, Cb);
        const idx = p << 2;
        out[idx] = rgb[0]; out[idx + 1] = rgb[1]; out[idx + 2] = rgb[2]; out[idx + 3] = 255;
    }

    self.postMessage({ buffer: out.buffer }, [out.buffer]);
};
