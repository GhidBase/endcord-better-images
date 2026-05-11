# Copyright (C) 2025-2026 Dylan Simon
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.

"""Inline image and custom emoji rendering via the Kitty terminal graphics protocol."""

import logging
import os
import re
import sys
import time
import unicodedata

EXT_NAME = "Better Images"
EXT_VERSION = "0.1.0"
EXT_ENDCORD_VERSION = "1.4.2"
EXT_DESCRIPTION = "Inline image and custom emoji rendering via the Kitty terminal graphics protocol. Supports Kitty, Ghostty, and WezTerm."
EXT_SOURCE = "https://github.com/ghidbase/endcord-better-images"

logger = logging.getLogger(__name__)

_IMAGE_ROWS = 20    # max rows reserved per inline image (must match cap in _render_overlay)
_IMAGE_MAX_COLS = 40  # max terminal columns an image may occupy


def _detect_kitty_graphics():
    if os.environ.get("KITTY_WINDOW_ID"):
        return True
    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    if term_program in ("ghostty", "kitty", "wezterm"):
        return True
    if "kitty" in os.environ.get("TERM", "").lower():
        return True
    return False


def _get_pixel_size():
    try:
        import fcntl
        import struct
        import termios
        buf = struct.pack("HHHH", 0, 0, 0, 0)
        buf = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, buf)
        _rows, _cols, xpx, ypx = struct.unpack("HHHH", buf)
        if xpx and ypx:
            return xpx, ypx
    except Exception:
        pass
    return None


def _kitty_place_str(term_row, term_col, payload_b64, cols=2, rows=1, px_y=0):
    chunk_size = 4096
    chunks = [payload_b64[i:i + chunk_size] for i in range(0, len(payload_b64), chunk_size)]
    if not chunks:
        return ""
    opts = f"a=T,f=100,c={cols},r={rows},q=2,C=1"
    if px_y:
        opts += f",y={px_y}"
    out = f"\x1b[{term_row + 1};{term_col + 1}H"
    if len(chunks) == 1:
        out += f"\x1b_G{opts},m=0;{chunks[0]}\x1b\\"
    else:
        out += f"\x1b_G{opts},m=1;{chunks[0]}\x1b\\"
        for chunk in chunks[1:-1]:
            out += f"\x1b_Gm=1,q=2;{chunk}\x1b\\"
        out += f"\x1b_Gm=0,q=2;{chunks[-1]}\x1b\\"
    return out


def _is_renderable_embed(e):
    """True if embed e should be rendered as an inline image."""
    if "main_url" not in e and e.get("type", "").startswith("image"):
        return True  # image/gif/etc. attachment
    if e.get("type") == "gifv" and e.get("main_url"):
        return True  # Tenor/Giphy gifv URL-unfurl embed
    return False


def _embed_render_url(e):
    """Return the URL to fetch for rendering embed e."""
    if e.get("type") == "gifv":
        return e["main_url"]
    return e["url"]


def _is_image_line(text):
    """True if a chat line represents an image/gifv embed that should get placeholder rows."""
    return " attachment]: " in text or "[gifv embed]: " in text


class Extension:
    def __init__(self, app):
        self.app = app

        # Add extension dir to path so bundled image_cache/emoji_cache are importable
        _ext_dir = os.path.dirname(os.path.abspath(__file__))
        if _ext_dir not in sys.path:
            sys.path.insert(0, _ext_dir)

        from endcord import terminal_utils as _tu
        import image_cache as _ic
        import emoji_cache as _ec
        self._tu = _tu
        self._ic = _ic
        self._ec = _ec

        if not _detect_kitty_graphics():
            logger.info("Kitty graphics protocol not detected — extension inactive")
            return

        tui = app.tui
        # In the compiled binary, screen_update doesn't call _render_emoji_overlay
        # and none of the image/emoji tui attributes exist — detect this and compensate.
        self._needs_overlay_thread = '_render_emoji_overlay' not in type(tui).__dict__

        tui.use_kitty_emoji = True
        tui._on_image_ready = self._tui_on_image_ready
        tui.on_image_ready = self._tui_on_image_ready
        tui._render_emoji_overlay = self._tui_render_overlay
        tui.set_wide = self._tui_set_wide
        tui._kitty_skip = set()

        # Initialize attributes absent from the compiled binary
        for _attr, _default in [
            ('image_map', []), ('image_positions', []),
            ('emoji_positions', []), ('emoji_map', []),
            ('extra_emoji_positions', []), ('_last_overlay_key', None),
            ('reaction_emoji_positions', []), ('reaction_emoji_map', []),
        ]:
            if not hasattr(tui, _attr):
                setattr(tui, _attr, _default)
        if not hasattr(tui, 'set_images'):
            tui.set_images = lambda m, _t=tui: setattr(_t, 'image_map', m)

        app._IMAGE_ROWS = _IMAGE_ROWS
        app._insert_jumbo_placeholders = self._app_insert_jumbo
        app._insert_image_placeholders = self._app_insert_image
        app._build_image_map = self._app_build_image_map
        app.update_chat = self._app_update_chat
        app.add_pending_message = self._app_add_pending_message
        app._kitty_chat_needs_update = False
        self._anim_urls = set()      # URLs of animated images currently visible
        self._anim_thread_started = False

        logger.info("Kitty graphics extension active")

    # --- TUI methods ---

    def _tui_on_image_ready(self):
        tui = self.app.tui
        tui._last_overlay_key = None
        self.app._kitty_chat_needs_update = True
        tui.need_update.set()

    def _start_anim_thread(self):
        """Start the animation thread that advances GIF frames and triggers redraws."""
        if self._anim_thread_started:
            return
        self._anim_thread_started = True
        import threading
        ic = self._ic
        ext = self

        def _loop():
            next_times = {}  # url -> monotonic time of next frame advance
            while True:
                now = time.monotonic()
                urls = set(ext._anim_urls)

                # Drop timers for URLs no longer visible
                for url in list(next_times):
                    if url not in urls:
                        del next_times[url]

                # Advance frames that are due; schedule URLs we haven't seen before
                advanced = False
                for url in urls:
                    if url not in next_times or now >= next_times[url]:
                        delay_ms = ic.get_frame_delay(url)
                        if delay_ms is not None:
                            ic.advance_frame(url)
                            next_times[url] = now + delay_ms / 1000.0
                            advanced = True

                if advanced:
                    tui = ext.app.tui
                    # Force a full delete-and-rerender so the new frame is visible.
                    tui._last_overlay_key = None
                    tui._overlay_needs_redraw = True
                    tui.need_update.set()

                if next_times:
                    sleep_time = max(0.01, min(next_times.values()) - time.monotonic())
                else:
                    sleep_time = 0.05
                time.sleep(sleep_time)

        threading.Thread(target=_loop, daemon=True).start()

    def on_main_start(self):
        """Wrap common_keybindings; start overlay thread if running in compiled binary."""
        if getattr(self, '_skip_wrap_done', False):
            return
        self._skip_wrap_done = True
        tui = self.app.tui
        _prev = tui.common_keybindings
        ext = self
        def _wrapped(key, mouse=False, switch=False, command=False, forum=False):
            result = _prev(key, mouse=mouse, switch=switch, command=command, forum=forum)
            ext._apply_skip(key)
            return result
        tui.common_keybindings = _wrapped

        if self._needs_overlay_thread:
            self._start_overlay_thread()
        else:
            # Dev build: wrap draw_chat to compute reaction emoji positions after each draw.
            # (Binary build handles this inside _wrapped_draw in _start_overlay_thread.)
            _prev_draw = tui.draw_chat
            def _wrapped_dev_draw(*args, **kwargs):
                result = _prev_draw(*args, **kwargs)
                h = tui.chat_hw[0]
                ext._compute_reaction_positions(h, tui.chat_index, tui.chat_buffer)
                if tui.reaction_emoji_positions:
                    try:
                        for _y, _x, _eid, _nlen in tui.reaction_emoji_positions:
                            tui.win_chat.addstr(_y, _x, ' ' * _nlen)
                        tui.win_chat.noutrefresh()
                    except Exception:
                        pass
                tui.need_update.set()
                return result
            tui.draw_chat = _wrapped_dev_draw

    def _start_overlay_thread(self):
        """For the compiled binary: hook need_update and run our own overlay render thread."""
        import threading
        tui = self.app.tui
        _overlay_event = threading.Event()
        _orig_set = tui.need_update.set

        def _hooked_set():
            _orig_set()
            _overlay_event.set()
        tui.need_update.set = _hooked_set

        # Wrap draw_chat to compute image_positions and emoji_positions after each draw
        import unicodedata
        def _char_w(ch):
            return 2 if unicodedata.east_asian_width(chr(ch)) in ('W', 'F') else 1

        _prev_draw = tui.draw_chat

        def _wrapped_draw(*args, **kwargs):
            result = _prev_draw(*args, **kwargs)
            h = tui.chat_hw[0]

            image_map = tui.image_map
            img_positions = []
            if image_map:
                content_x = getattr(self.app.formatter, 'newline_len', 0)
                best = {}  # url -> (y, content_x, row_offset) keeping smallest row_offset
                for num in range(h):
                    buf_idx = tui.chat_index + num
                    if buf_idx < len(image_map) and image_map[buf_idx]:
                        url, row_offset = image_map[buf_idx]
                        y = h - 1 - num
                        if url not in best or row_offset < best[url][2]:
                            best[url] = (y, content_x, row_offset)
                img_positions = [(y, x, url, ro) for url, (y, x, ro) in best.items()]
            tui.image_positions = img_positions

            emoji_map = tui.emoji_map
            em_positions = []
            if emoji_map:
                for num in range(h):
                    buf_idx = tui.chat_index + num
                    if buf_idx >= len(emoji_map):
                        break
                    emoji_ranges = emoji_map[buf_idx]
                    if not emoji_ranges:
                        continue
                    y = h - 1 - num
                    line = tui.chat_buffer[buf_idx] if buf_idx < len(tui.chat_buffer) else ""
                    screen_x = 0
                    pos = 0
                    for er in sorted(emoji_ranges, key=lambda e: e[0]):
                        while pos < er[0] and pos < len(line):
                            screen_x += _char_w(ord(line[pos]))
                            pos += 1
                        is_jumbo = len(er) > 3 and bool(er[3])
                        em_positions.append((y, screen_x, er[2], er[1] - er[0], is_jumbo))
            if em_positions:
                try:
                    for p in em_positions:
                        tui.win_chat.addstr(p[0], p[1], ' ' * p[3])
                    tui.win_chat.noutrefresh()
                except Exception:
                    pass
            tui.emoji_positions = em_positions

            # Reaction emoji positions
            self._compute_reaction_positions(h, tui.chat_index, tui.chat_buffer)
            if tui.reaction_emoji_positions:
                try:
                    for _y, _x, _eid, _nlen in tui.reaction_emoji_positions:
                        tui.win_chat.addstr(_y, _x, ' ' * _nlen)
                    tui.win_chat.noutrefresh()
                except Exception:
                    pass

            # Signal that images need re-placing after this draw (curses may have
            # overwritten them), without forcing a flash-causing delete-all.
            tui._overlay_needs_redraw = True
            # Always wake the overlay thread — draw_chat doesn't guarantee
            # need_update.set() is called (e.g. on channel open, resize, scroll).
            tui.need_update.set()

            return result
        tui.draw_chat = _wrapped_draw

        # Wrap draw_extra_window to show emoji images in the : autocomplete popup
        _prev_draw_extra = tui.draw_extra_window
        _eid_pat = re.compile(r'<a?:[a-zA-Z0-9_]+:(\d+)>')

        def _wrapped_draw_extra(title, body, **kwargs):
            # Extract emoji IDs from current assist_found results
            emoji_ids = None
            assist_found = getattr(self.app, 'assist_found', None)
            if assist_found and body:
                ids = []
                for item in assist_found:
                    if isinstance(item, (list, tuple)) and len(item) > 1:
                        m = _eid_pat.match(str(item[1]))
                        ids.append(m.group(1) if m else None)
                    else:
                        ids.append(None)
                if any(x is not None for x in ids):
                    emoji_ids = ids

            # Prepend 2-space indent on emoji lines to make room for the image.
            # Strip any existing prefix first so re-navigation doesn't double-indent.
            if emoji_ids:
                body = list(body)
                for i in range(min(len(emoji_ids), len(body))):
                    if emoji_ids[i] is not None:
                        line = body[i]
                        body[i] = "  " + (line[2:] if line.startswith("  ") else line)

            result = _prev_draw_extra(title, body, **kwargs)

            if not emoji_ids:
                tui.extra_emoji_positions = []
                tui.need_update.set()
                return result

            try:
                win_y, win_x = tui.win_extra_window.getbegyx()
                win_h, _ = tui.win_extra_window.getmaxyx()
            except Exception:
                tui.extra_emoji_positions = []
                return result

            positions = []
            idx_start = getattr(tui, 'extra_index', 0)
            for row_offset in range(win_h - 1):  # -1 for title bar
                line_num = idx_start + row_offset
                if line_num >= len(emoji_ids):
                    break
                if emoji_ids[line_num] is not None:
                    positions.append((win_y + row_offset + 1, win_x, emoji_ids[line_num]))

            tui.extra_emoji_positions = positions
            tui.need_update.set()
            return result

        tui.draw_extra_window = _wrapped_draw_extra

        delay = getattr(tui, 'screen_update_delay', 0.01) + 0.005

        def _overlay_loop():
            while True:
                _overlay_event.wait()
                _overlay_event.clear()
                time.sleep(delay)
                self._tui_render_overlay()
        threading.Thread(target=_overlay_loop, daemon=True).start()

    def _compute_reaction_positions(self, h, chat_index, chat_buffer):
        """Compute screen positions for custom emoji in reaction lines."""
        tui = self.app.tui
        reaction_map = tui.reaction_emoji_map
        positions = []
        for num in range(h):
            buf_idx = chat_index + num
            if buf_idx >= len(reaction_map):
                break
            r_emojis = reaction_map[buf_idx]
            if not r_emojis:
                continue
            y = h - 1 - num
            line = chat_buffer[buf_idx] if buf_idx < len(chat_buffer) else ""
            for char_pos, emoji_id, name_len in r_emojis:
                screen_x = sum(
                    2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1
                    for ch in line[:char_pos]
                )
                positions.append((y, screen_x, emoji_id, name_len))
        tui.reaction_emoji_positions = positions

    def _apply_skip(self, key):
        tui = self.app.tui
        skip = tui._kitty_skip
        if not skip:
            return
        if key in tui.KEYBINDINGS_CHAT_UP:
            moved = False
            while tui.chat_selected + 1 < len(tui.chat_buffer) and tui.chat_selected in skip:
                top_line = tui.chat_index + tui.chat_hw[0] - 3
                if top_line + 3 < len(tui.chat_buffer) and tui.chat_selected >= top_line:
                    tui.chat_index += 1
                tui.chat_selected += 1
                moved = True
            if moved:
                tui.draw_chat()
        elif key in tui.KEYBINDINGS_CHAT_DOWN:
            moved = False
            while tui.chat_selected >= tui.dont_hide_chat_selection and tui.chat_selected in skip:
                if tui.chat_index and tui.chat_selected <= tui.chat_index + 2:
                    tui.chat_index -= 1
                tui.chat_selected -= 1
                moved = True
            if moved:
                tui.draw_chat()

    def on_main_loop(self):
        if self.app._kitty_chat_needs_update:
            self.app._kitty_chat_needs_update = False
            self.app.update_chat(keep_selected=True, scroll=False)

    def _tui_render_overlay(self):
        tui = self.app.tui
        # Clear stale extra emoji positions when the assist window has closed
        if tui.extra_emoji_positions and not getattr(tui, 'extra_window_body', None):
            tui.extra_emoji_positions = []
        try:
            chat_begy, chat_begx = tui.win_chat.getbegyx()
            chat_h, chat_w = tui.win_chat.getmaxyx()
        except Exception:
            os.write(sys.stdout.fileno(), "\x1b_Ga=d,d=A,q=2\x1b\\".encode())
            return
        overlay_key = (
            chat_begy, chat_begx, chat_h, chat_w,
            tuple(tui.emoji_positions),
            tuple(tui.image_positions),
            tuple(tui.extra_emoji_positions),
            tuple(tui.reaction_emoji_positions),
        )
        positions_changed = overlay_key != tui._last_overlay_key
        needs_redraw = getattr(tui, '_overlay_needs_redraw', False)
        if not positions_changed and not needs_redraw:
            return
        tui._last_overlay_key = overlay_key
        tui._overlay_needs_redraw = False
        # Only delete all placed images when positions change (scroll, resize, etc.).
        # For same-position redraws triggered by cursor movement, skip the delete so
        # images are re-placed without the flash caused by clearing first.
        parts = ["\x1b_Ga=d,d=A,q=2\x1b\\"] if positions_changed else []
        if not (tui.emoji_positions or tui.image_positions or tui.extra_emoji_positions or tui.reaction_emoji_positions):
            self._anim_urls = set()
        if tui.emoji_positions or tui.image_positions or tui.extra_emoji_positions or tui.reaction_emoji_positions:
            px = _get_pixel_size()
            th, tw = self._tu.get_size()
            if px and th and tw:
                cell_aspect = (px[1] / th) / (px[0] / tw)
            else:
                cell_aspect = 2.0
            jumbo_rows = 2
            jumbo_cols = max(2, round(jumbo_rows * cell_aspect))
            for (chat_y, chat_x, emoji_id, _tw, is_jumbo) in tui.emoji_positions:
                payload = self._ec.get_payload(emoji_id, on_ready=tui.on_image_ready)
                if payload is None:
                    continue
                if is_jumbo and chat_y + 1 < chat_h:
                    parts.append(_kitty_place_str(chat_begy + chat_y, chat_begx + chat_x, payload, cols=jumbo_cols, rows=jumbo_rows))
                else:
                    parts.append(_kitty_place_str(chat_begy + chat_y, chat_begx + chat_x, payload))
            new_anim_urls = set()
            for pos in tui.image_positions:
                if len(pos) == 3:
                    chat_y, chat_x, url = pos
                    row_offset = 0
                else:
                    chat_y, chat_x, url, row_offset = pos
                img_y = chat_y if row_offset else chat_y + 1
                available_rows = chat_h - img_y
                if available_rows < 1:
                    continue
                result = self._ic.get_payload(url, on_ready=tui.on_image_ready)
                if result is None:
                    continue
                if self._ic.is_animated(url):
                    new_anim_urls.add(url)
                    self._start_anim_thread()
                payload, img_w, img_h = result
                cols = min(max(4, (chat_w - chat_x) // 2), _IMAGE_MAX_COLS)
                aspect = (img_w / img_h) if img_h else 1.0
                total_rows = max(2, round(cols / aspect / cell_aspect))
                max_rows = min(_IMAGE_ROWS - 1, chat_h - 1)
                if total_rows > max_rows:
                    total_rows = max_rows
                    cols = min(max(4, round(total_rows * aspect * cell_aspect)), _IMAGE_MAX_COLS)
                rows_visible = total_rows - row_offset
                if rows_visible < 1:
                    continue
                rows = min(rows_visible, available_rows)
                px_y = round(row_offset * img_h / total_rows) if (row_offset and img_h) else 0
                parts.append(_kitty_place_str(chat_begy + img_y, chat_begx + chat_x, payload, cols=cols, rows=rows, px_y=px_y))
            self._anim_urls = new_anim_urls
            for (abs_row, abs_col, emoji_id) in tui.extra_emoji_positions:
                payload = self._ec.get_payload(emoji_id, on_ready=tui.on_image_ready)
                if payload is None:
                    continue
                parts.append(_kitty_place_str(abs_row, abs_col, payload))
            for (chat_y, chat_x, emoji_id, _name_len) in tui.reaction_emoji_positions:
                payload = self._ec.get_payload(emoji_id, on_ready=tui.on_image_ready)
                if payload is None:
                    continue
                parts.append(_kitty_place_str(chat_begy + chat_y, chat_begx + chat_x, payload))
        if not parts:
            return
        out = "\x1b7" + "".join(parts) + "\x1b8"
        os.write(sys.stdout.fileno(), out.encode())

    def _tui_set_wide(self, chat_map):
        """Update wide characters map and emoji overlay map."""
        tui = self.app.tui
        tui.wide_map = []
        tui._kitty_skip = set()
        emoji_map = []
        reaction_emoji_map = []
        for num, line in enumerate(chat_map):
            if line is None:
                tui._kitty_skip.add(num)
                emoji_map.append([])
                reaction_emoji_map.append([])
                continue
            if line[6]:
                tui.wide_map.append(num + 1)
            if len(line) > 5 and line[5] and len(line[5]) > 2:
                lr = line[5]
                emoji_ranges = lr[2]
                is_jumbo = lr[5] if len(lr) > 5 else self._is_jumbo_msg(line[0])
                if is_jumbo:
                    emoji_map.append([[*r, True] for r in emoji_ranges])
                else:
                    emoji_map.append(emoji_ranges)
            else:
                emoji_map.append([])
            # Reaction line: entry[3] is a list of [start, end] spans
            if isinstance(line[3], list):
                msg_idx = line[0]
                r_emojis = []
                if msg_idx is not None and 0 <= msg_idx < len(self.app.messages):
                    msg_reactions = self.app.messages[msg_idx].get("reactions", [])
                    chat_line = self.app.chat[num] if num < len(self.app.chat) else ""
                    for n, span in enumerate(line[3]):
                        if n >= len(msg_reactions):
                            break
                        emoji_id = msg_reactions[n].get("emoji_id")
                        if not emoji_id:
                            continue
                        emoji_name_str = msg_reactions[n].get("emoji", "")
                        name_len = len(emoji_name_str)
                        if not name_len:
                            continue
                        char_pos = chat_line.find(emoji_name_str, span[0])
                        if char_pos < 0:
                            char_pos = span[0]
                        r_emojis.append((char_pos, emoji_id, name_len))
                reaction_emoji_map.append(r_emojis)
            else:
                reaction_emoji_map.append([])
        tui.emoji_map = emoji_map
        tui.reaction_emoji_map = reaction_emoji_map

    # --- App methods ---

    def _is_jumbo_msg(self, msg_num):
        """Return True if the message consists only of custom Discord emoji."""
        if msg_num is None or msg_num < 0 or msg_num >= len(self.app.messages):
            return False
        raw = self.app.messages[msg_num].get("content", "") or ""
        if not raw.strip():
            return False
        return (bool(re.search(r'<a?:[a-zA-Z0-9_]+:\d+>', raw))
                and re.sub(r'<a?:[a-zA-Z0-9_]+:\d+>|\s', '', raw) == "")

    def _app_insert_jumbo(self):
        app = self.app
        if not app.tui.use_kitty_emoji:
            return
        i = 0
        while i < len(app.chat):
            entry = app.chat_map[i] if i < len(app.chat_map) else None
            if entry is not None and entry[5] is not None:
                lr = entry[5]
                is_jumbo = lr[5] if len(lr) > 5 else self._is_jumbo_msg(entry[0])
            else:
                is_jumbo = False
            if is_jumbo:
                app.chat.insert(i, "")
                app.chat_format.insert(i, [[0]])
                app.chat_map.insert(i, None)
                i += 2
                continue
            i += 1

    def _app_insert_image(self):
        app = self.app
        if not app.tui.use_kitty_emoji:
            return

        # Pre-compute image URL list per message (embed order = top-to-bottom display order).
        msg_image_urls = {}
        for msg_idx, msg in enumerate(app.messages):
            urls = [_embed_render_url(e) for e in msg.get("embeds", [])
                    if _is_renderable_embed(e)]
            if urls:
                msg_image_urls[msg_idx] = urls

        # Buffer index 0 = bottom of screen = newest. Iterating upward means we see
        # attachment lines in reverse display order (bottom attachment first = last image first).
        msg_attach_seen = {}  # msg_idx -> count of attachment lines processed so far

        i = 0
        while i < len(app.chat):
            entry = app.chat_map[i] if i < len(app.chat_map) else None
            if entry is not None and entry[0] is not None:
                msg_idx = entry[0]
                if (0 <= msg_idx < len(app.messages)
                        and _is_image_line(app.chat[i])
                        and msg_idx in msg_image_urls
                        and not entry[2]):  # skip reply/interaction preview lines
                    urls = msg_image_urls[msg_idx]
                    seen = msg_attach_seen.get(msg_idx, 0)
                    # seen=0 → bottommost attachment → last image URL; seen=1 → second-to-last; etc.
                    url_idx = len(urls) - 1 - seen
                    img_url = urls[max(0, url_idx)]
                    msg_attach_seen[msg_idx] = seen + 1

                    cached = self._ic.get_payload(img_url, on_ready=app.tui.on_image_ready)
                    if cached:
                        _, img_w, img_h = cached
                        px = _get_pixel_size()
                        th, tw_term = self._tu.get_size()
                        cell_aspect = ((px[1] / th) / (px[0] / tw_term)) if (px and th and tw_term) else 2.0
                        cols = min(max(4, app.chat_dim[1] // 2), _IMAGE_MAX_COLS)
                        aspect = (img_w / img_h) if img_h else 1.0
                        rows = max(2, round(cols / aspect / cell_aspect))
                        if rows > _IMAGE_ROWS - 1:
                            rows = _IMAGE_ROWS - 1
                            cols = min(max(4, round(rows * aspect * cell_aspect)), _IMAGE_MAX_COLS)
                        n = min(rows + 1, _IMAGE_ROWS - 1)
                    else:
                        n = _IMAGE_ROWS - 1
                    app.chat[i:i] = [""] * n
                    app.chat_format[i:i] = [[[0]]] * n
                    app.chat_map[i:i] = [None] * n
                    i += n + 1  # skip the blanks + the attachment line itself
                    continue
            i += 1

    def _app_build_image_map(self):
        app = self.app
        image_map = [None] * len(app.chat)
        msg_to_lines = {}
        for i, entry in enumerate(app.chat_map):
            if entry is not None and entry[0] is not None and i < len(app.chat):
                msg_to_lines.setdefault(entry[0], []).append(i)
        for msg_idx, line_indices in msg_to_lines.items():
            if msg_idx < 0 or msg_idx >= len(app.messages):
                continue
            image_urls = [_embed_render_url(e) for e in app.messages[msg_idx].get("embeds", [])
                          if _is_renderable_embed(e)]
            if not image_urls:
                continue
            attach_lines = [i for i in line_indices
                            if _is_image_line(app.chat[i])
                            and not (app.chat_map[i] and app.chat_map[i][2])]
            # attach_lines is ascending (bottom attachment first = last image in display order).
            # reversed(image_urls) pairs the bottom attachment with the last image URL.
            for line_idx, url in zip(attach_lines, reversed(image_urls)):
                image_map[line_idx] = (url, 0)
                for k in range(1, _IMAGE_ROWS):
                    blank_idx = line_idx - k
                    if blank_idx < 0 or app.chat_map[blank_idx] is not None:
                        break
                    image_map[blank_idx] = (url, k)
        return image_map

    def _app_update_chat(self, keep_selected=True, change_amount=0, select_message_index=None, scroll=True, select_unread=False, change_id=None, change_type=None):
        app = self.app
        if app.messages is None:
            return

        if keep_selected and app.messages:
            selected_line, _ = app.tui.get_chat_selected()
            if selected_line == -1:
                keep_selected = False
            selected_msg, remainder = app.lines_to_msg_with_remainder(selected_line, space=True)
        else:
            selected_line = 0
            selected_msg = 0
            remainder = 0

        if app.gateway.legacy:
            for message in app.messages:
                if message["referenced_message"] and not message["referenced_message"]["id"]:
                    message["referenced_message"] = None

        last_seen_msg = None
        channel_id = app.active_channel["channel_id"]
        channel = app.read_state.get(channel_id)
        if channel:
            last_acked_unreads_line = channel.get("last_acked_unreads_line")
            last_message_id = channel["last_message_id"]
            if last_acked_unreads_line and (not last_message_id or int(last_acked_unreads_line) < int(last_message_id)):
                last_seen_msg = channel["last_acked_unreads_line"]

        chat, chat_format, chat_map = app.formatter.generate_chat(
            app.messages,
            app.current_roles,
            app.current_channels,
            app.chat_dim[1],
            app.current_my_roles,
            app.current_member_roles,
            app.blocked,
            last_seen_msg,
            app.show_blocked_messages,
            change_id=change_id,
            change_type=change_type,
        )
        app.chat = chat[:]
        app.chat_format = chat_format[:]
        app.chat_map = chat_map[:]
        app._insert_jumbo_placeholders()
        app._insert_image_placeholders()
        app.tui.set_wide(app.chat_map)
        app.tui.set_images(app._build_image_map())

        if keep_selected:
            selected_msg = selected_msg + change_amount
            selected_line_new = app.msg_to_lines(selected_msg) - remainder
            change_amount_lines = selected_line_new - selected_line
            app.tui.set_selected(selected_line_new, change_amount=change_amount_lines, scroll=scroll, draw=False)
        elif select_message_index is not None:
            full_message = (app.config["message_spacing"] and select_unread)
            selected_line_new = abs(app.msg_to_lines(select_message_index, full=select_unread)) + 1 + full_message
            app.tui.set_selected(selected_line_new, scroll=scroll, draw=False)
        elif keep_selected is not None:
            app.tui.set_selected(-1, scroll=scroll, draw=False)

        app.tui.update_chat(app.chat, app.chat_format)

    def _app_add_pending_message(self, content, nonce, reply_id=None, attachments=None, stickers=None):
        from endcord import formatter
        app = self.app

        if not app.show_pending_messages:
            return
        if app.get_chat_last_message_id() != app.last_message_id:
            return

        _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif", ".tiff"}
        embeds = []
        if attachments:
            for attachment in attachments:
                ext = os.path.splitext(attachment["name"])[1].lower()
                embed_type = f"image/{ext[1:]}" if ext in _IMAGE_EXTS else "unknown"
                embeds.append({
                    "type": embed_type,
                    "name": attachment["name"],
                    "url": attachment["path"],
                })
                if embed_type != "unknown" and app.tui.use_kitty_emoji:
                    self._ic.preload_local(attachment["path"], on_ready=app.tui.on_image_ready)

        referenced_message = None
        if reply_id:
            for message in app.messages:
                if message["id"] == reply_id:
                    referenced_message = message
                    break
            else:
                referenced_message = {
                    "id": reply_id,
                    "timestamp": formatter.discord_timestamp(time.time()),
                    "content": "Unknown content",
                    "mentions": [],
                    "user_id": "-1000",
                    "username": "unknown",
                    "global_name": None,
                    "nick": None,
                    "embeds": [],
                    "stickers": [],
                }

        if stickers:
            stickers = [{"name": "sticker", "id": s, "format_type": -1} for s in stickers]
        else:
            stickers = []

        if not content and not embeds and not stickers:
            return
        message = {
            "pending": True,
            "id": nonce,
            "channel_id": app.active_channel["channel_id"],
            "guild_id": app.active_channel["guild_id"],
            "timestamp": formatter.discord_timestamp(time.time()),
            "edited": False,
            "content": content,
            "mentions": [],
            "mention_roles": [],
            "mention_everyone": None,
            "user_id": app.my_id,
            "username": app.my_user_data["username"],
            "global_name": app.my_user_data["global_name"],
            "nick": app.my_user_data["nick"],
            "referenced_message": referenced_message,
            "reactions": [],
            "embeds": embeds,
            "stickers": stickers,
            "interaction": None,
        }

        if app.emoji_as_text:
            message = formatter.demojize_message(message)
        app.messages.insert(0, message)
        app.last_message_id = message["id"]
        app.update_chat(change_amount=1, scroll=False, change_id=nonce, change_type=1)
