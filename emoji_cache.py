# Copyright (C) 2025-2026 SparkLost
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.

"""Async download and disk/memory cache for Discord custom emoji images."""

import base64
import io
import logging
import os
import threading
import urllib.request

logger = logging.getLogger(__name__)

_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "endcord", "emoji")
_mem: dict = {}       # emoji_id -> base64 PNG payload string
_pending: set = set()
_lock = threading.Lock()


def _disk_path(emoji_id):
    return os.path.join(_CACHE_DIR, f"{emoji_id}.png")


def _to_payload(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode()


def _fetch(emoji_id, on_ready):
    url = f"https://cdn.discordapp.com/emojis/{emoji_id}.webp?size=64&quality=lossless"
    try:
        from PIL import Image
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:150.0) Gecko/20100101 Firefox/150.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = resp.read()
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        os.makedirs(_CACHE_DIR, exist_ok=True)
        img.save(_disk_path(emoji_id), format="PNG")
        payload = _to_payload(img)
        with _lock:
            _mem[emoji_id] = payload
            _pending.discard(emoji_id)
        if on_ready:
            try:
                on_ready()
            except Exception:
                pass
    except Exception as exc:
        logger.debug("emoji %s download failed: %s", emoji_id, exc)
        with _lock:
            _pending.discard(emoji_id)


def get_payload(emoji_id, on_ready=None):
    """
    Return the cached base64 PNG payload for emoji_id, or None if not ready yet.
    Starts a background download on first miss and calls on_ready() when done.
    """
    with _lock:
        if emoji_id in _mem:
            return _mem[emoji_id]

        disk = _disk_path(emoji_id)
        if os.path.exists(disk):
            try:
                from PIL import Image
                img = Image.open(disk).convert("RGBA")
                payload = _to_payload(img)
                _mem[emoji_id] = payload
                return payload
            except Exception:
                pass

        if emoji_id not in _pending:
            _pending.add(emoji_id)
            threading.Thread(target=_fetch, args=(emoji_id, on_ready), daemon=True).start()

    return None
