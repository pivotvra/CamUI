"""Image rendering for the live views (all inputs are float32 bands in 0–1)."""
import cv2
import numpy as np

VIEWS = {
    "overlay": "Overlay (green = IR, magenta = RED)",
    "edges": "Edges (green = IR, magenta = RED)",
    "checker": "Checkerboard IR / RED",
    "blink": "Blink IR ↔ RED",
    "diff": "Difference",
    "ndvi": "NDVI",
    "ndvi_masked": "NDVI vegetation only",
    "ir": "IR band",
    "red": "RED band (aligned)",
}


def _ndvi_lut():
    # red → yellow → green, like the usual RdYlGn NDVI palette
    stops = [(0.0, (165, 0, 38)), (0.25, (244, 109, 67)), (0.5, (255, 255, 191)),
             (0.75, (102, 189, 99)), (1.0, (0, 104, 55))]
    lut = np.zeros((256, 1, 3), np.uint8)
    for i in range(256):
        x = i / 255
        for (x0, c0), (x1, c1) in zip(stops, stops[1:]):
            if x <= x1:
                f = (x - x0) / (x1 - x0)
                rgb = [c0[k] + f * (c1[k] - c0[k]) for k in range(3)]
                lut[i, 0] = (rgb[2], rgb[1], rgb[0])
                break
    return lut


NDVI_LUT = _ndvi_lut()


def stretch(band, valid=None, lo=1, hi=99):
    """Percentile contrast stretch to 0–1 (so IR and RED look alike)."""
    sample = band[valid] if valid is not None and valid.any() else band
    if sample.size == 0:
        return band
    a, b = np.percentile(sample, (lo, hi))
    if b - a < 1e-6:
        return np.clip(band - a, 0, 1)
    return np.clip((band - a) / (b - a), 0, 1)


def to_u8(band01):
    return (np.clip(band01, 0, 1) * 255).astype(np.uint8)


def gray_bgr(band01):
    return cv2.cvtColor(to_u8(band01), cv2.COLOR_GRAY2BGR)


def overlay(ir, red, valid):
    i, r = stretch(ir, valid), stretch(red, valid)
    out = cv2.merge([to_u8(r), to_u8(i), to_u8(r)])
    out[~valid] //= 3
    return out


def edges(ir, red, valid):
    def mag(b):
        b = cv2.GaussianBlur(b.astype(np.float32), (0, 0), 1.0)
        m = cv2.magnitude(cv2.Sobel(b, cv2.CV_32F, 1, 0), cv2.Sobel(b, cv2.CV_32F, 0, 1))
        m[~valid] = 0
        return stretch(m, valid, 50, 99.5)
    gi, gr = mag(ir), mag(red)
    return cv2.merge([to_u8(gr), to_u8(gi), to_u8(gr)])


def checker(ir, red, valid, cells=8):
    i, r = stretch(ir, valid), stretch(red, valid)
    h, w = i.shape
    size = max(8, w // cells)
    yy, xx = np.mgrid[0:h, 0:w]
    use_red = ((yy // size + xx // size) % 2).astype(bool)
    out = np.where(use_red, r, i)
    img = gray_bgr(out)
    img[~valid] //= 3
    return img


def diff(ir, red, valid):
    d = np.abs(stretch(ir, valid) - stretch(red, valid))
    d[~valid] = 0
    return cv2.applyColorMap(to_u8(np.clip(d * 2, 0, 1)), cv2.COLORMAP_INFERNO)


def ndvi_color(ndvi, vmin, vmax, valid):
    x = (np.nan_to_num(ndvi, nan=vmin) - vmin) / max(1e-6, vmax - vmin)
    img = cv2.applyColorMap(to_u8(x), NDVI_LUT)
    img[~valid] = (40, 40, 40)
    return img


def ndvi_masked(ndvi, ir, vmin, vmax, threshold, valid):
    img = ndvi_color(ndvi, vmin, vmax, valid)
    background = gray_bgr(stretch(ir, valid) * 0.6)
    veg = valid & (np.nan_to_num(ndvi, nan=-1) > threshold)
    img[~veg] = background[~veg]
    return img


def draw_zones(img, row_start, row_end, n_zones, zone_values=None, flip_h=False):
    h, w = img.shape[:2]
    y0, y1 = int(row_start * h), int(row_end * h)
    thick = max(1, w // 400)
    cv2.rectangle(img, (0, y0), (w - 1, y1), (255, 255, 255), thick)
    zw = w / n_zones
    for i in range(1, n_zones):
        x = int(i * zw)
        cv2.line(img, (x, y0), (x, y1), (255, 255, 255), thick)
    if zone_values:
        scale = max(0.4, w / 1600)
        for i, v in enumerate(zone_values):
            k = n_zones - 1 - i if flip_h else i
            text = "-" if v is None else f"{v:.2f}"
            x = int(k * zw + zw / 2) - int(30 * scale)
            y = max(int(20 * scale), y0 - int(8 * scale))
            cv2.putText(img, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), thick + 2, cv2.LINE_AA)
            cv2.putText(img, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), thick, cv2.LINE_AA)
    return img


def colorbar(img, vmin, vmax):
    h, w = img.shape[:2]
    bw, bh = max(8, w // 60), int(h * 0.5)
    x0, y0 = w - bw - 10, int(h * 0.25)
    bar = cv2.applyColorMap(np.linspace(255, 0, bh).astype(np.uint8).reshape(-1, 1), NDVI_LUT)
    img[y0:y0 + bh, x0:x0 + bw] = cv2.resize(bar, (bw, bh), interpolation=cv2.INTER_NEAREST)
    scale = max(0.35, w / 2000)
    for val, y in ((vmax, y0 + 10), (vmin, y0 + bh)):
        t = f"{val:.2f}"
        (tw, _), _ = cv2.getTextSize(t, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
        cv2.putText(img, t, (x0 - tw - 4, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(img, t, (x0 - tw - 4, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 1, cv2.LINE_AA)
    return img


def label(img, text):
    scale = max(0.4, img.shape[1] / 1800)
    cv2.putText(img, text, (8, int(22 * scale / 0.5)), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(img, text, (8, int(22 * scale / 0.5)), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 1, cv2.LINE_AA)
    return img


def message_frame(text, size=(820, 616)):
    img = np.full((size[1], size[0], 3), (41, 37, 33), np.uint8)
    y = size[1] // 2 - 10 * text.count("\n")
    for line in text.split("\n"):
        (tw, _), _ = cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 1)
        cv2.putText(img, line, ((size[0] - tw) // 2, y), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200, 200, 200), 1, cv2.LINE_AA)
        y += 30
    return img


def jpeg(img, quality=80):
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else b""
