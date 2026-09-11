#!/usr/bin/env python3
"""Image post-processing for scanned pages: rotate, filters, auto-crop, deskew."""
import numpy as np
from PIL import Image, ImageFilter, ImageOps, ImageChops

# Filters offered in the UI.  id -> (label, description)
FILTERS = [
    ("none",       "Original",           "No processing"),
    ("auto",       "Auto",               "Balance brightness + trim white margins"),
    ("document",   "Document",           "White background, bold text — for printed text"),
    ("bw",         "Black & white",      "Binarize — small files, for text"),
    ("gray",       "Grayscale",          "Convert to grayscale"),
    ("sharpen",    "Sharpen",            "Sharpen text (unsharp mask)"),
    ("brighten",   "Brighten",           "Lift dark areas"),
    ("flat",       "Flatten background", "Remove shadows / uneven lighting"),
    ("despeckle",  "Despeckle",          "Filter out fine grain noise"),
    ("autocrop",   "Crop margins",       "Crop surrounding whitespace"),
    ("scan_clean", "Clean scan",         "Grayscale + white background + sharpen"),
]

FILTER_LABELS = {k: v for k, v, _ in FILTERS}


def _arr(img):
    return np.asarray(img.convert("L") if img.mode in ("1", "L") else img, dtype=np.float32)


def _content_bbox(img, thresh=200, pad=12):
    """Bounding box of non-white content."""
    g = np.asarray(img.convert("L"), dtype=np.uint8)
    mask = g < thresh
    if mask.sum() < 50:
        return None
    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    y0, y1 = int(rows[0]), int(rows[-1])
    x0, x1 = int(cols[0]), int(cols[-1])
    h, w = g.shape
    y0 = max(0, y0 - pad); x0 = max(0, x0 - pad)
    y1 = min(h - 1, y1 + pad); x1 = min(w - 1, x1 + pad)
    if (y1 - y0) < 40 or (x1 - x0) < 40:
        return None
    return (x0, y0, x1 + 1, y1 + 1)


def autocrop(img, keep_ratio=True):
    """Crop white margins. Returns (image, changed, size)."""
    original_size = img.size
    bbox = _content_bbox(img)
    if not bbox:
        return img, False, original_size
    out = img.crop(bbox)
    if out.size == original_size:
        return img, False, original_size
    return out, True, out.size


def flat_field(img, blur=61):
    """Remove uneven lighting / shadows by dividing out a heavily blurred copy."""
    g = np.asarray(img.convert("L"), dtype=np.float32)
    bg = np.asarray(img.convert("L").filter(ImageFilter.GaussianBlur(blur)), dtype=np.float32)
    bg = np.maximum(bg, 1.0)
    out = np.clip(g / bg * 255.0, 0, 255)
    return Image.fromarray(out.astype(np.uint8), "L")


def adaptive_bw(img, block=31, offset=12):
    """Local-mean threshold — the classic 'scanned text' look."""
    g = np.asarray(img.convert("L"), dtype=np.float32)
    try:
        from PIL import ImageFilter as IF
        mean = np.asarray(
            Image.fromarray(g.astype(np.uint8)).filter(IF.BoxBlur(block // 2)), dtype=np.float32)
    except Exception:
        mean = g.mean()
    out = np.where(g > mean - offset, 255, 0).astype(np.uint8)
    return Image.fromarray(out, "L")


def _to_gray(img, params):
    mode = params.get("mode", "gray")
    if mode == "color":
        return img.convert("RGB")
    if mode == "bw":
        return adaptive_bw(img).convert("1")
    return img.convert("L")


def apply_filter(img, name, params=None):
    """Apply a named filter. Returns (image, list_of_notes)."""
    params = params or {}
    notes = []
    if name in (None, "", "none"):
        return img, notes

    if name == "auto":
        img = ImageOps.autocontrast(img.convert("L"), cutoff=1)
        notes.append("brightness balanced")
        cropped, changed, _ = autocrop(img)
        if changed:
            img = cropped
            notes.append("margins cropped")
        return img, notes

    if name == "document":
        img = flat_field(img.convert("L"), params.get("blur", 61))
        img = ImageOps.autocontrast(img, cutoff=1)
        notes.append("white background")
        return img, notes

    if name == "bw":
        base = flat_field(img.convert("L")) if params.get("flat", True) else img.convert("L")
        img = adaptive_bw(base, int(params.get("block", 31)), int(params.get("offset", 12)))
        notes.append("binarized")
        return img, notes

    if name == "gray":
        return img.convert("L"), ["grayscale"]

    if name == "sharpen":
        g = img.convert("L")
        img = g.filter(ImageFilter.UnsharpMask(radius=2, percent=int(params.get("percent", 160)), threshold=3))
        notes.append("sharpened")
        return img, notes

    if name == "brighten":
        g = np.asarray(img.convert("L"), dtype=np.float32)
        gain = float(params.get("gain", 1.25))
        out = np.clip(255.0 - (255.0 - g) * gain, 0, 255)
        return Image.fromarray(out.astype(np.uint8), "L"), ["brightened %.2fx" % gain]

    if name == "flat":
        return flat_field(img.convert("L"), int(params.get("blur", 61))), ["background flattened"]

    if name == "despeckle":
        g = img.convert("L")
        out = g.filter(ImageFilter.MedianFilter(size=int(params.get("size", 3))))
        # keep it from smearing edges too much
        out = Image.blend(g, out, 0.7)
        return out, ["despeckled"]

    if name == "autocrop":
        cropped, changed, _ = autocrop(img)
        if changed:
            notes.append("margins cropped")
        return cropped, notes

    if name == "scan_clean":
        img = flat_field(img.convert("L"))
        img = ImageOps.autocontrast(img, cutoff=1)
        img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=140, threshold=3))
        notes.append("grayscale + white bg + sharpen")
        return img, notes

    return img, notes


def rotate(img, degrees):
    """Rotate by arbitrary degrees, expanding the canvas and filling with white."""
    degrees = float(degrees) % 360.0
    if abs(degrees) < 0.01:
        return img
    if abs(degrees - 180.0) < 0.01:
        return img.rotate(180, expand=True, fillcolor=_fill(img))
    return img.rotate(degrees, expand=True, resample=Image.BICUBIC, fillcolor=_fill(img))


def _fill(img):
    return 255 if img.mode in ("L", "1") else (255, 255, 255)


def deskew(img, max_angle=4.0, step=0.25):
    """Estimate skew from horizontal projection variance and rotate back."""
    g = ImageOps.autocontrast(img.convert("L"))
    if max(g.size) > 1200:                     # work small: plenty accurate, much faster
        scale = 1200.0 / max(g.size)
        g = g.resize((max(1, int(g.size[0] * scale)), max(1, int(g.size[1] * scale))))
    a = np.asarray(g, dtype=np.float32)
    a = (a < 200).astype(np.float32)

    best_angle, best_score = 0.0, -1.0
    angle = -max_angle
    while angle <= max_angle + 1e-9:
        rot = Image.fromarray((a * 255).astype(np.uint8)).rotate(
            angle, resample=Image.BILINEAR, expand=False, fillcolor=0)
        rows = np.asarray(rot, dtype=np.float32).sum(axis=1)
        score = float(((rows[1:] - rows[:-1]) ** 2).sum())    # sharp row edges = aligned text
        if score > best_score:
            best_score, best_angle = score, angle
        angle += step

    if abs(best_angle) < 0.3:
        return img, 0.0
    return rotate(img, -best_angle), -best_angle


def is_blank(img, thresh=200, min_content_ratio=0.002):
    """True if the page looks empty (nothing meaningful on the glass)."""
    g = np.asarray(img.convert("L"), dtype=np.uint8)
    ratio = float((g < thresh).sum()) / g.size
    return ratio < min_content_ratio, ratio


def stats(img):
    g = np.asarray(img.convert("L"), dtype=np.float32)
    return {"mean": round(float(g.mean()), 1), "std": round(float(g.std()), 1)}


def make_thumb(img, max_side=420):
    copy = img.copy()
    copy.thumbnail((max_side, max_side), Image.LANCZOS)
    if copy.mode not in ("RGB", "L"):
        copy = copy.convert("RGB")
    return copy
