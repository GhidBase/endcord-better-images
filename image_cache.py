# Copyright (C) 2025-2026 SparkLost
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.

"""Async download and disk/memory cache for Discord attachment images."""

import base64
import hashlib
import io
import logging
import os
import threading
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "endcord", "images")
_MAX_PX = 512       # max dimension before downscaling
_mem: dict = {}     # cache_key -> (payload_b64, width_px, height_px)
_anim: dict = {}    # cache_key -> {"frames": [(payload, w, h), ...], "delays": [ms, ...], "index": 0}
_pending: set = set()
_lock = threading.Lock()


def _cache_key(url):
    """Stable key from URL path only — query params are expiring auth tokens."""
    path = urllib.parse.urlparse(url).path
    return hashlib.md5(path.encode()).hexdigest()


def _disk_path(key):
    return os.path.join(_CACHE_DIR, f"{key}.png")


def _to_payload(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode()


def _resize(img, max_px):
    w, h = img.size
    if w > max_px or h > max_px:
        from PIL import Image
        ratio = min(max_px / w, max_px / h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    return img


def _extract_frames(img):
    """Extract all frames and delays from an animated image. Returns (frames, delays) or (None, None)."""
    from PIL import Image
    try:
        n_frames = getattr(img, 'n_frames', 1)
    except Exception:
        n_frames = 1
    if n_frames <= 1:
        return None, None
    frames = []
    delays = []
    for i in range(n_frames):
        try:
            img.seek(i)
        except EOFError:
            break
        delay = img.info.get('duration', 100)
        frame = img.convert("RGBA")
        frame = _resize(frame, _MAX_PX)
        w, h = frame.size
        frames.append((_to_payload(frame), w, h))
        delays.append(max(int(delay), 20))
    return (frames, delays) if len(frames) > 1 else (None, None)


_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:150.0) Gecko/20100101 Firefox/150.0"}


def _open_image(data, url):
    """Open PIL Image from bytes, with MP4→GIF fallback for Tenor/Giphy URLs."""
    from PIL import Image
    try:
        return Image.open(io.BytesIO(data)), data
    except Exception:
        # Tenor/Giphy gifv embeds expose MP4 URLs; try the GIF equivalent instead
        if url.endswith(".mp4"):
            gif_url = url[:-4] + ".gif"
            req = urllib.request.Request(gif_url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                gif_data = resp.read()
            return Image.open(io.BytesIO(gif_data)), gif_data
        raise


def _fetch(url, key, on_ready):
    try:
        from PIL import Image
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        img, data = _open_image(data, url)
        frames, delays = _extract_frames(img)
        os.makedirs(_CACHE_DIR, exist_ok=True)
        if frames:
            # Animated — save first frame to disk as preview, store all frames in memory
            img.seek(0)
            first = _resize(img.convert("RGBA"), _MAX_PX)
            first.save(_disk_path(key), format="PNG")
            with _lock:
                _anim[key] = {"frames": frames, "delays": delays, "index": 0}
                _pending.discard(key)
        else:
            img = _resize(img.convert("RGBA"), _MAX_PX)
            w, h = img.size
            img.save(_disk_path(key), format="PNG")
            payload = _to_payload(img)
            with _lock:
                _mem[key] = (payload, w, h)
                _pending.discard(key)
        if on_ready:
            try:
                on_ready()
            except Exception:
                pass
    except Exception as exc:
        logger.debug("image download failed %s: %s", url, exc)
        with _lock:
            _pending.discard(key)


def preload_local(path, on_ready=None):
    """Load a local file into the cache (used for pending upload previews)."""
    key = _cache_key(path)
    with _lock:
        if key in _mem or key in _anim or key in _pending:
            return
        _pending.add(key)
    threading.Thread(target=_fetch_local, args=(path, key, on_ready), daemon=True).start()


def _fetch_local(path, key, on_ready):
    try:
        from PIL import Image
        img = Image.open(path)
        frames, delays = _extract_frames(img)
        if frames:
            with _lock:
                _anim[key] = {"frames": frames, "delays": delays, "index": 0}
                _pending.discard(key)
        else:
            img = _resize(img.convert("RGBA"), _MAX_PX)
            w, h = img.size
            payload = _to_payload(img)
            with _lock:
                _mem[key] = (payload, w, h)
                _pending.discard(key)
        if on_ready:
            try:
                on_ready()
            except Exception:
                pass
    except Exception as exc:
        logger.debug("local image load failed %s: %s", path, exc)
        with _lock:
            _pending.discard(key)


def get_payload(url, on_ready=None):
    """
    Return (payload_b64, width_px, height_px) for url, or None if not ready.
    For animated GIFs returns the current frame. Starts a background download
    on first miss; calls on_ready() when done.
    """
    key = _cache_key(url)
    with _lock:
        if key in _anim:
            info = _anim[key]
            return info["frames"][info["index"]]
        if key in _mem:
            return _mem[key]

        disk = _disk_path(key)
        if os.path.exists(disk):
            try:
                from PIL import Image
                img = Image.open(disk).convert("RGBA")
                payload = _to_payload(img)
                entry = (payload, img.width, img.height)
                _mem[key] = entry
                return entry
            except Exception:
                pass

        if key not in _pending:
            _pending.add(key)
            threading.Thread(target=_fetch, args=(url, key, on_ready), daemon=True).start()

    return None


def is_animated(url):
    """Return True if url is a cached animated image."""
    key = _cache_key(url)
    with _lock:
        return key in _anim


def get_frame_delay(url):
    """Return the delay in ms for the current frame, or None if not animated."""
    key = _cache_key(url)
    with _lock:
        if key in _anim:
            info = _anim[key]
            return info["delays"][info["index"]]
    return None


def advance_frame(url):
    """Advance to the next frame of an animated image."""
    key = _cache_key(url)
    with _lock:
        if key in _anim:
            info = _anim[key]
            info["index"] = (info["index"] + 1) % len(info["frames"])
