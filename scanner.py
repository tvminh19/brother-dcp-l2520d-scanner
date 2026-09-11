#!/usr/bin/env python3
"""Brother DCP-L2520D USB scanner driver (brscan4 protocol, no SANE needed).

Protocol reference: brscan (libsane-brother), brother_mfccmd.h + brother_scanner.c
  - Open : ctrl_transfer(0xC0, 0x01, 0x0002, 0, 5)
  - Close: ctrl_transfer(0xC0, 0x02, 0x0002, 0, 5)
  - Cancel: write b"\x1bR"
  - Scan start: "\x1bX\n" + params + "\x80"
  - Plane header: (hdr & 0x1C) -> 0x04/0x08/0x0C color (emitted as YCbCr), 0x00 mono
                  (hdr & 0x03) -> 2 = PackBits compressed

Performance notes (measured on a Raspberry Pi 5, USB 2.0):
  - The scan head is the bottleneck: ~1.2 MB/s at 300 dpi grey, ~2.2 MB/s at
    600 dpi colour. Raw transfer time is fixed by the hardware.
  - What the driver *can* avoid is dead time around the transfer: a slow
    stale-byte drain before the scan, and a long "is it finished?" wait after
    the last raster line. The device ends a page with an abrupt, permanent
    stream of zero-length USB packets; we treat a short data gap as EOF instead
    of burning several seconds every scan.
  - Planes are handed to NumPy as zero-copy ``memoryview`` slices so a 100 MB
    colour page is not duplicated in memory just to be decoded.
"""
import time
from datetime import datetime

import usb.core
import usb.util
from PIL import Image
import numpy as np

VID, PID = 0x04F9, 0x0324
EP_OUT, EP_IN = 0x04, 0x85
IFACE = 1

PAGE_MM = {"a4": (210, 297), "letter": (216, 279), "legal": (216, 356), "a5": (148, 210)}
PAGE_SIZE_CHOICES = tuple(PAGE_MM.keys())

COLOR_CMD = {
    "color": b"M=CGRAY\n",     # 24-bit colour
    "gray": b"M=GRAY256\n",    # 8-bit grey
    "bw": b"M=ERRDIF\n",       # 1-bit mono
}

# --- read-loop tuning -------------------------------------------------------
READ_CHUNK = 256 * 1024   # bytes per usb read; big -> fewer syscalls on 100 MB pages
FIRST_BYTE_TIMEOUT = 15.0  # give up waiting for the first byte (then retry once)
IDLE_EOF = 2.5            # no data for this long (after data started) => page done
MAX_SECONDS = 900         # absolute ceiling for one page
DRAIN_ROUNDS = 8          # stale-byte reads before a scan (early-exit when clean)


class ScannerError(RuntimeError):
    pass


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def device_present():
    try:
        return usb.core.find(idVendor=VID, idProduct=PID) is not None
    except Exception:
        return False


def open_scanner():
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        raise ScannerError("Scanner not found (check the USB cable and that the printer is powered on)")

    for i in (0, 1):
        try:
            if dev.is_kernel_driver_active(i):
                dev.detach_kernel_driver(i)
        except Exception:
            pass

    dev.set_configuration(1)
    usb.util.claim_interface(dev, IFACE)
    return dev


def scanner_open_session(dev):
    """Send the 'open' control message; recover from a stale/stuck scanner."""

    def _check(resp):
        # resp[2] bit 0x80 => BCOMMAND_RETURN (device needs a power cycle)
        if len(resp) >= 3 and (resp[2] & 0x80):
            raise ScannerError(
                "The printer is in an error state (BCOMMAND_RETURN). "
                "Turn it OFF and ON again, wait ~10 seconds, then retry."
            )

    try:
        _check(bytes(dev.ctrl_transfer(0xC0, 0x01, 0x0002, 0, 5, timeout=5000)))
        return dev
    except ScannerError:
        raise
    except usb.core.USBError as e:
        log(f"open control failed ({e}); resetting USB and retrying")
        try:
            usb.util.release_interface(dev, IFACE)
        except Exception:
            pass
        try:
            dev.reset()
        except Exception:
            pass
        time.sleep(2.0)
        dev = open_scanner()
        _check(bytes(dev.ctrl_transfer(0xC0, 0x01, 0x0002, 0, 5, timeout=5000)))
        return dev


def build_scan_command(resolution, color_mode, w, h):
    cmd = b"\x1bX\n"
    cmd += ("R=%d,%d\n" % (resolution, resolution)).encode()
    cmd += COLOR_CMD[color_mode]
    cmd += b"C=NONE\n"        # uncompressed: simple + fast to parse
    cmd += b"B=50\nN=50\n"    # brightness / contrast neutral
    cmd += b"U=OFF\nP=OFF\n"  # business mode off, photo mode off
    cmd += ("A=0,0,%d,%d\n" % (w, h)).encode()
    cmd += b"D=SIN\n"
    cmd += b"\x80"
    return cmd


def cancel_scan(dev):
    try:
        dev.write(EP_OUT, b"\x1bR", timeout=2000)
    except Exception:
        pass
    try:
        dev.ctrl_transfer(0xC0, 0x02, 0x0002, 0, 5, timeout=3000)
    except Exception:
        pass


def close_scanner(dev):
    if dev is None:
        return
    cancel_scan(dev)
    try:
        usb.util.release_interface(dev, IFACE)
    except Exception:
        pass
    try:
        usb.util.dispose_resources(dev)
    except Exception:
        pass


def _drain_stale(dev):
    """Discard leftover bytes from a previous aborted scan. Returns quickly when
    the pipe is already clean (the device answers with a zero-length packet)."""
    dropped = 0
    for _ in range(DRAIN_ROUNDS):
        try:
            stale = dev.read(EP_IN, 65536, timeout=150)
        except usb.core.USBError:
            break
        if not stale:
            break
        dropped += len(stale)
    if dropped:
        log(f"drained {dropped} stale bytes")


PLANE_OF = {0x00: "mono", 0x04: "R", 0x08: "G", 0x0C: "B", 0x10: "RGB", 0x14: "BGR", 0x1C: "256"}


def _rows_seen(planes, color_mode, w):
    if color_mode == "color":
        n = len(planes["RGB"]) // 3 or len(planes["G"])
        return n // max(w, 1)
    if color_mode == "bw":
        return len(planes["mono"]) // max((w + 7) // 8, 1)
    return len(planes["mono"]) // max(w, 1)


def read_scan_data(dev, expected_h, w, color_mode="gray", on_progress=None, should_stop=None):
    """Read raw records until the device goes idle. Returns (planes, raw_len, records).

    The page is "done" when no bytes arrive for ``IDLE_EOF`` seconds after the
    stream started -- the DCP-L2520D switches abruptly to an unbroken run of
    zero-length packets when it finishes, with no distinct end marker to catch.
    While that ZLP run is going we sleep briefly between polls so we don't spin
    a CPU core at ~8000 empty reads/second.
    """
    planes = {k: bytearray() for k in ("mono", "R", "G", "B", "RGB", "256")}
    buf = bytearray()
    raw_len = records = 0
    last_report = 0.0
    t0 = last_data = time.time()

    while time.time() - t0 < MAX_SECONDS:
        if should_stop and should_stop():
            log("scan cancelled by user")
            break

        try:
            data = bytes(dev.read(EP_IN, READ_CHUNK, timeout=8000))
        except usb.core.USBError as e:
            msg = str(e).lower()
            if e.errno in (110, 60) or "timeout" in msg or "timed out" in msg:
                if records > 0:
                    log(f"read done by timeout ({records} records)")
                    break
                continue
            raise

        now = time.time()
        if not data:
            if records == 0:
                if now - t0 > FIRST_BYTE_TIMEOUT:
                    log(f"read done: no data at all after {FIRST_BYTE_TIMEOUT:.0f}s")
                    break
                if now - t0 > 1.0:          # past warm-up: don't spin while waiting
                    time.sleep(0.02)
                continue
            idle = now - last_data
            if idle > IDLE_EOF:
                log(f"read done by idle ({records} records, {raw_len} bytes)")
                break
            if idle > 0.25:                 # ZLP storm -> stop hammering the bus
                time.sleep(0.02)
            continue

        last_data = now
        raw_len += len(data)
        buf.extend(data)

        pos, n = 0, len(buf)
        while pos < n:
            hdr = buf[pos]
            if hdr >= 0x80:            # marker / padding, skip one byte
                pos += 1
                continue
            if pos + 3 > n:
                break
            wrapper_len = buf[pos + 1] | (buf[pos + 2] << 8)
            lp = pos + 3 + wrapper_len
            if lp + 2 > n:
                break
            data_len = buf[lp] | (buf[lp + 1] << 8)
            rec_end = lp + 2 + data_len
            if data_len == 0 or rec_end > n:
                break
            kind = PLANE_OF.get(hdr & 0x1C)
            if kind and (hdr & 0x03) != 2:     # skip PackBits (we ask for C=NONE)
                planes[kind].extend(buf[lp + 2:rec_end])
            records += 1
            pos = rec_end
        del buf[:pos]

        if on_progress and now - last_report > 0.7:
            on_progress(_rows_seen(planes, color_mode, w), expected_h, records)
            last_report = now

    return planes, raw_len, records


def decode_image(planes, color_mode, w, h):
    """Turn raw planes into a PIL image (zero-copy plane -> NumPy)."""
    if color_mode == "color":
        if planes["RGB"]:
            packed = planes["RGB"]
            rows = min(len(packed) // (w * 3), h)
            if rows < 2:
                raise ScannerError("No image data received from the scanner")
            arr = np.frombuffer(memoryview(packed)[:rows * w * 3], dtype=np.uint8)
            return Image.fromarray(arr.reshape(rows, w, 3), "RGB")

        r, g, b = planes["R"], planes["G"], planes["B"]
        rows = min(len(r), len(g), len(b)) // w
        if rows < 2:
            raise ScannerError(
                "No data received from the scanner. Check that the paper is on the glass, "
                "the lid is closed, and the printer is not in an error state."
            )
        n = rows * w
        # Headers say R/G/B but the device actually emits YCbCr:
        # plane 0x08 = luma, 0x04 = Cr, 0x0C = Cb. Treating them as RGB
        # renders a white page as flat green.
        Y = np.frombuffer(memoryview(g)[:n], dtype=np.uint8).reshape(rows, w).astype(np.float32)
        Cr = np.frombuffer(memoryview(r)[:n], dtype=np.uint8).reshape(rows, w).astype(np.float32) - 128.0
        Cb = np.frombuffer(memoryview(b)[:n], dtype=np.uint8).reshape(rows, w).astype(np.float32) - 128.0
        R = np.clip(Y + 1.40200 * Cr, 0, 255)
        G = np.clip(Y - 0.34414 * Cb - 0.71414 * Cr, 0, 255)
        B = np.clip(Y + 1.77200 * Cb, 0, 255)
        return Image.fromarray(np.stack([R, G, B], axis=2).astype(np.uint8), "RGB")

    d = planes["mono"]
    if color_mode == "gray":
        rows = min(len(d) // w, h)
        if rows < 2:
            raise ScannerError("Incomplete grayscale image data (%d bytes)" % len(d))
        arr = np.frombuffer(memoryview(d)[:rows * w], dtype=np.uint8).reshape(rows, w)
        return Image.fromarray(arr, "L")

    row_bytes = (w + 7) // 8
    rows = min(len(d) // row_bytes, h)
    if rows < 2:
        raise ScannerError("Incomplete black & white image data (%d bytes)" % len(d))
    arr = np.frombuffer(memoryview(d)[:rows * row_bytes], dtype=np.uint8).reshape(rows, row_bytes)
    bits = (1 - np.unpackbits(arr, axis=1)[:, :w]) * 255   # device stores 1 = black
    return Image.fromarray(bits.astype(np.uint8), "L")


def _scan_once(dev, resolution, color_mode, page_size, w, h, on_progress, should_stop):
    dev = scanner_open_session(dev)
    _drain_stale(dev)
    dev.write(EP_OUT, build_scan_command(resolution, color_mode, w, h), timeout=3000)
    time.sleep(0.8)

    t_read = time.time()
    planes, raw_len, records = read_scan_data(
        dev, h, w, color_mode=color_mode,
        on_progress=on_progress, should_stop=should_stop)
    dt = time.time() - t_read
    rate = raw_len / dt / 1e6 if dt else 0.0
    log(f"raw: {raw_len} bytes, {records} records in {dt:.1f}s ({rate:.1f} MB/s)")
    return planes, raw_len, records


def scan_image(resolution=300, color_mode="color", page_size="a4",
               on_progress=None, should_stop=None):
    """Full one-page scan. Returns (PIL.Image, meta dict).

    Retries once if the device accepts the command but streams nothing -- the
    DCP-L2520D sometimes needs a few seconds to settle between back-to-back
    scans and answers the first request with silence.
    """
    wm, hm = PAGE_MM.get(page_size, PAGE_MM["a4"])
    w = (int(wm * resolution / 25.4) // 16) * 16
    h = int(hm * resolution / 25.4)

    log(f"scan start: {resolution}dpi {color_mode} {page_size} -> {w}x{h}")
    for attempt in (1, 2):
        dev = open_scanner()
        try:
            planes, raw_len, records = _scan_once(
                dev, resolution, color_mode, page_size, w, h, on_progress, should_stop)
        finally:
            close_scanner(dev)

        if records > 0 or (should_stop and should_stop()):
            break
        if attempt == 1:
            log("no data from device; settling for 4s and retrying once")
            time.sleep(4.0)

    img = decode_image(planes, color_mode, w, h)
    meta = {"resolution": resolution, "color_mode": color_mode,
            "page_size": page_size, "raw_bytes": raw_len, "records": records,
            "width": img.size[0], "height": img.size[1]}
    return img, meta
