#!/usr/bin/env python3
"""Brother DCP-L2520D Scanner Web UI — Flask app.

Backend layout:
    scanner.py   USB/brscan4 driver (scan -> PIL image)
    imaging.py   rotate / filters / auto-crop / deskew
    docstore.py  multi-page document, per-page edits, PDF & image export
"""
import threading
import traceback
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory, render_template

import docstore
import imaging
import scanner

BASE = Path(__file__).parent
LEGACY_DIR = BASE / "scans"
LEGACY_DIR.mkdir(exist_ok=True)

app = Flask(__name__)

scan_lock = threading.Lock()
cancel_event = threading.Event()
STATE = {"state": "idle", "progress": 0, "error": None, "page_id": None,
         "blank": False, "stage": "", "last_export": None}


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/settings")
def api_settings():
    return jsonify({
        "filters": [{"id": k, "label": l, "desc": d} for k, l, d in imaging.FILTERS],
        "page_sizes": list(scanner.PAGE_MM.keys()),
        "dpi": [150, 200, 300, 600],
        "color_modes": [
            {"id": "color", "label": "Color"},
            {"id": "gray", "label": "Grayscale"},
            {"id": "bw", "label": "Black & white"},
        ],
        "ocr_langs": [{"id": "eng", "label": "English"},
                      {"id": "vie+eng", "label": "Vietnamese + English"},
                      {"id": "vie", "label": "Vietnamese"}],
    })


@app.route("/api/device")
def api_device():
    return jsonify({"present": scanner.device_present()})


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------

def _scan_worker(resolution, color_mode, page_size, auto_filter, params):
    global STATE
    try:
        def on_progress(lines, total, records):
            STATE["progress"] = min(95, int(lines * 100 / max(total, 1)))
            STATE["stage"] = f"{lines}/{total} lines"

        img, meta = scanner.scan_image(
            resolution=resolution, color_mode=color_mode, page_size=page_size,
            on_progress=on_progress, should_stop=cancel_event.is_set)

        if cancel_event.is_set():
            STATE = {"state": "idle", "progress": 0, "error": None, "page_id": None,
                     "blank": False, "stage": "", "last_export": STATE.get("last_export")}
            return

        STATE["stage"] = "saving page"
        STATE["progress"] = 97
        page = docstore.add_page(img, meta)

        if auto_filter and auto_filter != "none":
            page = docstore.update_page(page["id"], filter=auto_filter, params=params or {})

        blank, ratio = imaging.is_blank(img)
        STATE = {"state": "done", "progress": 100, "error": None, "page_id": page["id"],
                 "blank": blank, "stage": "", "last_export": STATE.get("last_export")}
        log(f"page added {page['id']} ({img.size[0]}x{img.size[1]}) blank={blank}")
    except Exception as e:
        log("SCAN FAILED:\n" + traceback.format_exc())
        STATE = {"state": "error", "progress": 0, "error": str(e), "page_id": None,
                 "blank": False, "stage": "", "last_export": STATE.get("last_export")}


@app.route("/api/scan", methods=["POST"])
def api_scan():
    if scan_lock.locked():
        return jsonify({"status": "busy", "error": "A scan is already running"})

    p = request.json or {}
    resolution = int(p.get("resolution", 300))
    color_mode = p.get("colorMode", "color")
    page_size = p.get("pageSize", "a4")
    auto_filter = p.get("filter", "none")
    params = p.get("params") or {}

    cancel_event.clear()

    def runner():
        with scan_lock:
            _scan_worker(resolution, color_mode, page_size, auto_filter, params)

    global STATE
    STATE = {"state": "scanning", "progress": 0, "error": None, "page_id": None,
             "blank": False, "stage": "starting scanner",
             "last_export": STATE.get("last_export")}
    threading.Thread(target=runner, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/scan/cancel", methods=["POST"])
def api_scan_cancel():
    cancel_event.set()
    return jsonify({"status": "cancelling"})


@app.route("/api/status")
def api_status():
    return jsonify(STATE)


# --------------------------------------------------------------------------
# Document / pages
# --------------------------------------------------------------------------

@app.route("/api/doc")
def api_doc():
    return jsonify(docstore.doc_public())


@app.route("/api/doc/clear", methods=["POST"])
def api_doc_clear():
    docstore.clear_document()
    return jsonify({"ok": True})


@app.route("/api/doc/reorder", methods=["POST"])
def api_doc_reorder():
    order = (request.json or {}).get("order") or []
    docstore.reorder(order)
    return jsonify({"ok": True})


@app.route("/api/doc/name", methods=["POST"])
def api_doc_name():
    doc = docstore.load_document()
    doc["name"] = (request.json or {}).get("name", "").strip()
    docstore.save_document()
    return jsonify({"ok": True, "name": doc["name"]})


@app.route("/api/page/<pid>/rotate", methods=["POST"])
def api_page_rotate(pid):
    p = request.json or {}
    page = docstore.find_page(pid)
    if not page:
        return jsonify({"error": "Page not found"}), 404
    if "fine" in p:
        fine = float(p["fine"])
    else:
        fine = float(page.get("fine_rot", 0.0)) + float(p.get("delta", 90))
        while fine > 180:
            fine -= 360
        while fine < -180:
            fine += 360
    docstore.update_page(pid, fine_rot=fine)
    return jsonify({"ok": True, "page": docstore.doc_public()["pages"]})


@app.route("/api/page/<pid>/filter", methods=["POST"])
def api_page_filter(pid):
    p = request.json or {}
    f = p.get("filter", "none")
    valid = {k for k, _l, _d in imaging.FILTERS}
    if f not in valid:
        return jsonify({"error": "Invalid filter"}), 400
    docstore.update_page(pid, filter=f, params=p.get("params") or {})
    return jsonify({"ok": True})


@app.route("/api/page/<pid>/deskew", methods=["POST"])
def api_page_deskew(pid):
    page = docstore.find_page(pid)
    if not page:
        return jsonify({"error": "Page not found"}), 404
    from PIL import Image
    img = Image.open(docstore._paths(pid)["orig"])
    _, angle = imaging.deskew(img)
    if abs(angle) < 0.05:
        return jsonify({"ok": True, "angle": 0.0})
    docstore.update_page(pid, fine_rot=float(page.get("fine_rot", 0.0)) + angle)
    return jsonify({"ok": True, "angle": angle})


@app.route("/api/page/<pid>/delete", methods=["POST"])
def api_page_delete(pid):
    ok = docstore.delete_page(pid)
    return jsonify({"ok": ok})


@app.route("/api/page/<pid>/duplicate", methods=["POST"])
def api_page_duplicate(pid):
    page = docstore.duplicate_page(pid)
    return jsonify({"ok": bool(page)})


@app.route("/api/page/<pid>/thumb.png")
def api_page_thumb(pid):
    f = docstore._paths(pid)["thumb"]
    if f.exists():
        return send_file(str(f), mimetype="image/png")
    return "Not found", 404


@app.route("/api/page/<pid>/view.png")
def api_page_view(pid):
    f = docstore._paths(pid)["view"]
    if f.exists():
        return send_file(str(f), mimetype="image/png")
    return "Not found", 404


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

@app.route("/api/export", methods=["POST"])
def api_export():
    p = request.json or {}
    fmt = p.get("format", "pdf")          # pdf | png | jpg
    mode = p.get("mode") or None          # color | gray | bw | None
    quality = int(p.get("quality", 85))
    ocr = bool(p.get("ocr", False))
    lang = p.get("lang", "eng")
    target = p.get("target", "auto")

    doc = docstore.load_document()
    if not doc["pages"]:
        return jsonify({"error": "No pages yet. Scan at least one page before exporting."}), 400

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = docstore._safe_name(p.get("name") or doc.get("name"), "scan")
    n = len(doc["pages"])

    try:
        if fmt == "pdf":
            out = docstore.EXPORTS / f"{base}_{stamp}.pdf"
            docstore.export_pdf(out, mode=mode, quality=quality, ocr=ocr, lang=lang,
                                target=target)
        else:
            ext = "jpg" if fmt == "jpg" else "png"
            if n == 1:
                out = docstore.EXPORTS / f"{base}_{stamp}.{ext}"
                docstore.export_images(out, fmt=fmt, mode=mode, quality=quality)
            else:
                out = docstore.EXPORTS / f"{base}_{stamp}_{fmt}.zip"
                docstore.export_images(out, fmt=fmt, mode=mode, quality=quality,
                                       force_zip=True)
    except Exception as e:
        log("EXPORT FAILED:\n" + traceback.format_exc())
        return jsonify({"error": str(e)}), 500

    STATE["last_export"] = out.name
    log(f"exported {out.name} ({out.stat().st_size} bytes, {n} pages)")
    return jsonify({"ok": True, "file": out.name, "url": f"/api/file/{out.name}",
                    "size": out.stat().st_size, "pages": n})


@app.route("/api/exports")
def api_exports():
    return jsonify(docstore.exports_history())


@app.route("/api/file/<path:name>")
def api_file(name):
    f = docstore.EXPORTS / Path(name).name
    if not f.exists():
        return "Not found", 404
    mime = "application/pdf"
    if f.suffix.lower() == ".zip":
        mime = "application/zip"
    elif f.suffix.lower() == ".png":
        mime = "image/png"
    elif f.suffix.lower() in (".jpg", ".jpeg"):
        mime = "image/jpeg"
    return send_file(str(f), mimetype=mime, as_attachment=True)


# --------------------------------------------------------------------------
# Legacy single-shot scan files (kept so old links keep working)
# --------------------------------------------------------------------------

@app.route("/api/files")
def api_files():
    out = []
    for f in sorted(LEGACY_DIR.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True):
        out.append({"name": f.name, "size": f.stat().st_size,
                    "mtime": datetime.fromtimestamp(f.stat().st_mtime).strftime("%d/%m %H:%M")})
    return jsonify(out)


@app.route("/api/preview/<path:filename>")
def api_preview(filename):
    stem = Path(filename).stem
    for cand in (LEGACY_DIR / f"{stem}.png", LEGACY_DIR / f"{stem.replace('scan_', '')}.png"):
        if cand.exists():
            return send_file(str(cand), mimetype="image/png")
    return "Not found", 404


@app.route("/api/download/<path:filename>")
def api_download(filename):
    p = LEGACY_DIR / Path(filename).name
    if p.exists():
        return send_file(str(p), mimetype="application/pdf", as_attachment=True)
    return "Not found", 404


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(BASE / "static", filename)


if __name__ == "__main__":
    log("Scanner Web UI v2 on http://0.0.0.0:8090")
    app.run(host="0.0.0.0", port=8090, debug=False, threaded=True)
