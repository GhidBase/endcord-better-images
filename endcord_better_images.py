# Copyright (C) 2025-2026 Dylan Simon
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.

"""Inline image and custom emoji rendering via the Kitty terminal graphics protocol."""

import logging
import os
import sys
import time

EXT_NAME = "Better Images"
EXT_VERSION = "0.1.0"
EXT_ENDCORD_VERSION = "1.4.2"
EXT_DESCRIPTION = "Inline image and custom emoji rendering via the Kitty terminal graphics protocol. Supports Kitty, Ghostty, and WezTerm."
EXT_SOURCE = "https://github.com/ghidbase/endcord-better-images"

logger = logging.getLogger(__name__)

_IMAGE_ROWS = 20    # max rows reserved per inline image (must match cap in _render_overlay)
_IMAGE_MAX_COLS = 40  # max terminal columns an image may occupy


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

        if not _tu.detect_kitty_graphics():
            logger.info("Kitty graphics protocol not detected — extension inactive")
            return

        tui = app.tui
        tui.use_kitty_emoji = True
        tui._on_image_ready = self._tui_on_image_ready
        tui.on_image_ready = self._tui_on_image_ready
        tui._render_emoji_overlay = self._tui_render_overlay
        tui.set_wide = self._tui_set_wide
        tui._kitty_skip = set()

        app._IMAGE_ROWS = _IMAGE_ROWS
        app._insert_jumbo_placeholders = self._app_insert_jumbo
        app._insert_image_placeholders = self._app_insert_image
        app._build_image_map = self._app_build_image_map
        app.update_chat = self._app_update_chat
        app.add_pending_message = self._app_add_pending_message
        app._kitty_chat_needs_update = False

        logger.info("Kitty graphics extension active")

    # --- TUI methods ---

    def _tui_on_image_ready(self):
        tui = self.app.tui
        tui._last_overlay_key = None
        self.app._kitty_chat_needs_update = True
        tui.need_update.set()

    def on_main_start(self):
        """Wrap common_keybindings once after all extensions are loaded to apply skip."""
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
        )
        if overlay_key == tui._last_overlay_key:
            return
        tui._last_overlay_key = overlay_key
        parts = ["\x1b_Ga=d,d=A,q=2\x1b\\"]
        if tui.emoji_positions or tui.image_positions or tui.extra_emoji_positions:
            px = self._tu.get_pixel_size()
            th, tw = self._tu.get_size()
            if px and th and tw:
                cell_aspect = (px[1] / th) / (px[0] / tw)
            else:
                cell_aspect = 2.0
            jumbo_rows = 2
            jumbo_cols = max(2, round(jumbo_rows * cell_aspect))
            for (chat_y, chat_x, emoji_id, is_jumbo) in tui.emoji_positions:
                payload = self._ec.get_payload(emoji_id, on_ready=tui.on_image_ready)
                if payload is None:
                    continue
                if is_jumbo and chat_y + 1 < chat_h:
                    parts.append(self._tu.kitty_place_str(chat_begy + chat_y, chat_begx + chat_x, payload, cols=jumbo_cols, rows=jumbo_rows))
                else:
                    parts.append(self._tu.kitty_place_str(chat_begy + chat_y, chat_begx + chat_x, payload))
            for (chat_y, chat_x, url) in tui.image_positions:
                img_y = chat_y + 1
                available_rows = chat_h - img_y
                if available_rows < 2:
                    continue
                result = self._ic.get_payload(url, on_ready=tui.on_image_ready)
                if result is None:
                    continue
                payload, img_w, img_h = result
                cols = min(max(4, chat_w // 2), _IMAGE_MAX_COLS)
                aspect = (img_w / img_h) if img_h else 1.0
                rows = max(2, round(cols / aspect / cell_aspect))
                rows = min(rows, _IMAGE_ROWS - 1, available_rows)
                parts.append(self._tu.kitty_place_str(chat_begy + img_y, chat_begx + chat_x, payload, cols=cols, rows=rows))
            for (abs_row, abs_col, emoji_id) in tui.extra_emoji_positions:
                payload = self._ec.get_payload(emoji_id, on_ready=tui.on_image_ready)
                if payload is None:
                    continue
                parts.append(self._tu.kitty_place_str(abs_row, abs_col, payload))
        out = "\x1b7" + "".join(parts) + "\x1b8"
        os.write(sys.stdout.fileno(), out.encode())

    def _tui_set_wide(self, chat_map):
        """Update wide characters map and emoji overlay map."""
        tui = self.app.tui
        tui.wide_map = []
        tui._kitty_skip = set()
        emoji_map = []
        for num, line in enumerate(chat_map):
            if line is None:
                tui._kitty_skip.add(num)
                emoji_map.append([])
                continue
            if line[6]:
                tui.wide_map.append(num + 1)
            if len(line) > 5 and line[5] and len(line[5]) > 2:
                is_jumbo = len(line[5]) > 5 and bool(line[5][5])
                if is_jumbo:
                    emoji_map.append([[*r, True] for r in line[5][2]])
                else:
                    emoji_map.append(line[5][2])
            else:
                emoji_map.append([])
        tui.emoji_map = emoji_map

    # --- App methods ---

    def _app_insert_jumbo(self):
        app = self.app
        if not app.tui.use_kitty_emoji:
            return
        i = 0
        while i < len(app.chat):
            entry = app.chat_map[i] if i < len(app.chat_map) else None
            if (entry is not None
                    and entry[5] is not None
                    and len(entry[5]) > 5
                    and entry[5][5]):
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
        i = 0
        while i < len(app.chat):
            entry = app.chat_map[i] if i < len(app.chat_map) else None
            if entry is not None and entry[0] is not None:
                msg_idx = entry[0]
                if (0 <= msg_idx < len(app.messages)
                        and " attachment]: " in app.chat[i]
                        and any("main_url" not in e and e.get("type", "").startswith("image")
                                for e in app.messages[msg_idx].get("embeds", []))):
                    img_url = next((
                        e["url"] for e in app.messages[msg_idx].get("embeds", [])
                        if "main_url" not in e and e.get("type", "").startswith("image")
                    ), None)
                    cached = self._ic.get_payload(img_url, on_ready=app.tui.on_image_ready) if img_url else None
                    if cached:
                        _, img_w, img_h = cached
                        px = self._tu.get_pixel_size()
                        th, tw_term = self._tu.get_size()
                        cell_aspect = ((px[1] / th) / (px[0] / tw_term)) if (px and th and tw_term) else 2.0
                        cols = min(max(4, app.chat_dim[1] // 2), _IMAGE_MAX_COLS)
                        aspect = (img_w / img_h) if img_h else 1.0
                        n = min(max(2, round(cols / aspect / cell_aspect)) + 1, _IMAGE_ROWS - 1)
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
            image_urls = [
                embed["url"] for embed in app.messages[msg_idx].get("embeds", [])
                if "main_url" not in embed and embed.get("type", "").startswith("image")
            ]
            if not image_urls:
                continue
            attach_lines = [i for i in line_indices if " attachment]: " in app.chat[i]]
            for line_idx, url in zip(attach_lines, image_urls):
                image_map[line_idx] = url
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
