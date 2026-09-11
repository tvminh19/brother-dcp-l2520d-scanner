#!/usr/bin/env python3
"""Document store: multi-page scan sessions, per-page edits, PDF/image export."""
import json
import shutil
import subprocess
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from PIL import Image

import imaging

BASE = Path(__file__).parent
DATA = BASE / "data"
PAGES = DATA / "pages"
EXPORTS = DATA / "exports"
TMP = DATA / "tmp"
# Exact paper sizes in PDF points (1 pt = 1/72 inch)
PAPER_PT = {"a4": (595.28, 841.89), "letter": (612.0, 792.0), "legal": (612.0, 1008.0)}
for d in (DATA, PAGES, EXPORTS, TMP):
    d.mkdir(parents=True, exist_ok=True)

DOC_FILE = DATA / "current.json"
_lock_state = {"document": None}

# On-screen preview only. The full-resolution, lossless copy lives in *_orig.png
# and every export re-renders from that, so the viewer never needs 4960 px.
# Capping it here turns the post-scan "saving page" stall from ~6 s to ~2 s on a
# 600 dpi colour page (three big PNG encodes -> one big + two small).
VIEW_MAX_PX = 2600
FAST_PNG = 1          # compress_level for the archival original: ~3x faster encode


# --------------------------------------------------------------------------
# Document state
# --------------------------------------------------------------------------

def _new_document():
    return {"id": uuid.uuid4().hex[:8], "name": "", "created": datetime.now().isoformat(),
            "pages": []}


def load_document():
    if _lock_state["document"] is not None:
        return _lock_state["document"]
    if DOC_FILE.exists():
        try:
            _lock_state["document"] = json.loads(DOC_FILE.read_text())
            return _lock_state["document"]
        except Exception:
            pass
    _lock_state["document"] = _new_document()
    return _lock_state["document"]


def save_document():
    doc = load_document()
    DOC_FILE.write_text(json.dumps(doc, indent=2))
    return doc


def doc_public():
    """Document as the UI needs it."""
    doc = load_document()
    pages = []
    for p in doc["pages"]:
        pages.append({
            "id": p["id"],
            "n": len(pages) + 1,
            "thumb": f"/api/page/{p['id']}/thumb.png?v={p['rev']}",
            "w": p.get("view_w", p["src_w"]),
            "h": p.get("view_h", p["src_h"]),
            "src_w": p["src_w"], "src_h": p["src_h"],
            "rot": p.get("rot", 0),
            "fine_rot": p.get("fine_rot", 0.0),
            "filter": p.get("filter", "none"),
            "filter_label": imaging.FILTER_LABELS.get(p.get("filter", "none"), "-"),
            "blank": p.get("blank", False),
            "content_ratio": p.get("content_ratio"),
            "meta": p.get("meta", {}),
            "ts": p.get("ts", ""),
        })
    return {"id": doc["id"], "name": doc["name"], "created": doc["created"],
            "pages": pages, "count": len(pages)}


# --------------------------------------------------------------------------
# Page CRUD
# --------------------------------------------------------------------------

def _paths(pid):
    return {
        "orig": PAGES / f"{pid}_orig.png",
        "view": PAGES / f"{pid}_view.png",
        "thumb": PAGES / f"{pid}_thumb.png",
        "meta": PAGES / f"{pid}.json",
    }


def render_page(page, save=True):
    """Apply filter + rotation to the stored original and return the PIL image."""
    paths = _paths(page["id"])
    src = Image.open(paths["orig"])
    if src.mode not in ("RGB", "L"):
        src = src.convert("RGB")

    img, notes = imaging.apply_filter(src, page.get("filter", "none"), page.get("params") or {})
    page["filter_notes"] = notes

    angle = float(page.get("rot", 0)) + float(page.get("fine_rot", 0.0))
    if abs(angle) % 360 > 0.01:
        img = imaging.rotate(img, angle)

    if save:
        view = img
        if max(view.size) > VIEW_MAX_PX:
            view = view.copy()
            view.thumbnail((VIEW_MAX_PX, VIEW_MAX_PX), Image.LANCZOS)
        view.save(paths["view"], "PNG")
        imaging.make_thumb(img).save(paths["thumb"], "PNG")
        page["view_w"], page["view_h"] = img.size    # report the true rendered size
    return img


def add_page(img, meta):
    doc = load_document()
    pid = uuid.uuid4().hex[:10]
    paths = _paths(pid)

    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.save(paths["orig"], "PNG", compress_level=FAST_PNG)

    blank, ratio = imaging.is_blank(img)
    page = {
        "id": pid,
        "ts": datetime.now().strftime("%d/%m %H:%M:%S"),
        "created": datetime.now().isoformat(),
        "src_w": img.size[0], "src_h": img.size[1],
        "rot": 0, "fine_rot": 0.0,
        "filter": "none", "params": {},
        "blank": blank, "content_ratio": round(ratio, 5),
        "meta": meta or {}, "rev": 1,
    }
    doc["pages"].append(page)
    save_document()
    render_page(page, save=True)
    return page


def find_page(pid):
    doc = load_document()
    for p in doc["pages"]:
        if p["id"] == pid:
            return p
    return None


def update_page(pid, **changes):
    page = find_page(pid)
    if not page:
        return None
    page.update(changes)
    page["rev"] = page.get("rev", 1) + 1
    save_document()
    render_page(page, save=True)
    save_document()
    return page


def delete_page(pid):
    doc = load_document()
    page = find_page(pid)
    if not page:
        return False
    doc["pages"] = [p for p in doc["pages"] if p["id"] != pid]
    save_document()
    for f in _paths(pid).values():
        try:
            f.unlink()
        except Exception:
            pass
    return True


def duplicate_page(pid):
    src = find_page(pid)
    if not src:
        return None
    img = Image.open(_paths(pid)["orig"])
    copy = dict(src)
    copy.pop("id", None)
    copy.pop("rev", None)
    return add_page(img, src.get("meta", {}))


def reorder(order):
    doc = load_document()
    index = {p["id"]: p for p in doc["pages"]}
    new = [index[i] for i in order if i in index]
    for p in doc["pages"]:
        if p not in new:
            new.append(p)
    doc["pages"] = new
    save_document()
    return True


def clear_document():
    doc = load_document()
    for p in doc["pages"]:
        for f in _paths(p["id"]).values():
            try:
                f.unlink()
            except Exception:
                pass
    _lock_state["document"] = _new_document()
    save_document()
    return True


def render_all(mode=None, quality=85):
    """Yield (page, PIL image, dpi) for export. mode: color|gray|bw|None(keep)."""
    doc = load_document()
    if not doc["pages"]:
        raise RuntimeError("No pages to export")
    for page in doc["pages"]:
        img = render_page(page, save=False)
        if mode == "color" and img.mode != "RGB":
            img = img.convert("RGB")
        elif mode == "gray" and img.mode != "L":
            img = img.convert("L")
        elif mode == "bw":
            img = imaging.adaptive_bw(img).convert("1")
        dpi = float((page.get("meta") or {}).get("resolution", 300) or 300)
        yield page, img, dpi


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

def _safe_name(name, fallback):
    import re
    name = (name or "").strip() or fallback
    keep = "-_.() abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    cleaned = "".join(c if c in keep else "_" for c in name).strip().strip(".")
    cleaned = re.sub(r"[\s_]+", "_", cleaned).strip("_")
    return cleaned[:80] or fallback


def export_pdf(path, mode=None, quality=85, ocr=False, lang="eng", target="auto"):
    """Write a multi-page PDF, optionally with a searchable text layer.

    target: "auto" keeps each page at its scanned pixel size; "a4"/"letter"/"legal"
    fits every page onto that exact paper size (better when printing).
    """
    import fitz

    pages = list(render_all(mode=mode, quality=quality))
    if not pages:
        raise RuntimeError("No pages to export")

    doc_pdf = fitz.open()
    try:
        for i, (page, img, dpi) in enumerate(pages, 1):
            if ocr:
                pdf_bytes = _ocr_page_pdf(img, lang)
                if pdf_bytes:
                    src = fitz.open("pdf", pdf_bytes)
                    doc_pdf.insert_pdf(src)
                    src.close()
                    continue

            buf = _encode_image(img, quality)
            if target in PAPER_PT:
                w_pt, h_pt = PAPER_PT[target]
                # landscape scans stay landscape on the chosen paper
                if img.size[0] > img.size[1] and w_pt < h_pt:
                    w_pt, h_pt = h_pt, w_pt
            else:
                w_pt = img.size[0] * 72.0 / dpi
                h_pt = img.size[1] * 72.0 / dpi
            pg = doc_pdf.new_page(width=w_pt, height=h_pt)
            pg.insert_image(pg.rect, stream=buf)
        doc_pdf.save(str(path), deflate=True, garbage=3)
    finally:
        doc_pdf.close()
    return path


def _encode_image(img, quality=85):
    import io
    out = io.BytesIO()
    if img.mode in ("1", "L"):
        img.save(out, "PNG", optimize=True)          # 1-bit -> CCITT, tiny
    elif img.mode == "P":
        img.convert("RGB").save(out, "JPEG", quality=quality, optimize=True)
    else:
        img.save(out, "JPEG", quality=quality, optimize=True, subsampling=0)
    return out.getvalue()


def _ocr_page_pdf(img, lang="eng"):
    """Run tesseract on one page; returns searchable PDF bytes or None."""
    tmp_png = TMP / f"{uuid.uuid4().hex}.png"
    try:
        img.convert("RGB").save(tmp_png, "PNG")
        r = subprocess.run(
            ["tesseract", str(tmp_png), "stdout", "-l", lang, "pdf"],
            capture_output=True, timeout=180)
        if r.returncode == 0 and r.stdout[:4] == b"%PDF":
            return r.stdout
        return None
    except Exception:
        return None
    finally:
        try:
            tmp_png.unlink()
        except Exception:
            pass


def export_images(path, fmt="png", mode=None, quality=85, force_zip=False):
    """One image for a single page, or a ZIP of images for several pages."""
    pages = list(render_all(mode=mode, quality=quality))
    ext = "jpg" if fmt in ("jpg", "jpeg") else "png"

    if len(pages) == 1 and not force_zip:
        _, img, _ = pages[0]
        return _save_image(img, path, ext, quality)

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for i, (page, img, _dpi) in enumerate(pages, 1):
            buf = TMP / f"{uuid.uuid4().hex}.{ext}"
            _save_image(img, buf, ext, quality)
            z.write(buf, f"trang_{i:03d}.{ext}")
            buf.unlink(missing_ok=True)
    return path


def _save_image(img, path, ext, quality=85):
    if ext == "jpg":
        img.convert("RGB").save(path, "JPEG", quality=quality, optimize=True)
    else:
        img.save(path, "PNG", optimize=True)
    return Path(path)


def exports_history():
    out = []
    for f in sorted(EXPORTS.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.is_file():
            out.append({
                "name": f.name,
                "size": f.stat().st_size,
                "mtime": datetime.fromtimestamp(f.stat().st_mtime).strftime("%d/%m %H:%M"),
                "url": f"/api/file/{f.name}",
            })
    return out
