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


def _fetch(url, key, on_ready):
    try:
        from PIL import Image
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:150.0) Gecko/20100101 Firefox/150.0"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        w, h = img.size
        if w > _MAX_PX or h > _MAX_PX:
            ratio = min(_MAX_PX / w, _MAX_PX / h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
            w, h = img.size
        os.makedirs(_CACHE_DIR, exist_ok=True)
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
        if key in _mem or key in _pending:
            return
        _pending.add(key)
    threading.Thread(target=_fetch_local, args=(path, key, on_ready), daemon=True).start()


def _fetch_local(path, key, on_ready):
    try:
        from PIL import Image
        img = Image.open(path).convert("RGBA")
        w, h = img.size
        if w > _MAX_PX or h > _MAX_PX:
            ratio = min(_MAX_PX / w, _MAX_PX / h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
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
    Starts a background download on first miss; calls on_ready() when done.
    """
    key = _cache_key(url)
    with _lock:
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
