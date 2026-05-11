# endcord-better-images

An [endcord](https://github.com/SparkLost/endcord) extension that renders images and custom emoji inline in the terminal using the [Kitty terminal graphics protocol](https://sw.kovidgoyal.net/kitty/graphics-protocol/).

## Installation

Clone or copy this repository into `~/.config/endcord/Extensions/`:

```
~/.config/endcord/Extensions/endcord_better_images/
├── endcord_better_images.py
├── image_cache.py
└── emoji_cache.py
```

## Requirements

- A terminal that supports the Kitty graphics protocol: **Kitty**, **Ghostty**, or **WezTerm**
- [Pillow](https://python-pillow.org/) (`pip install Pillow`) for image decoding and resizing
- endcord 1.4.2

## Features

### Inline image attachments
Images attached to messages are rendered directly in the chat below the attachment line. Size is computed automatically from the image's aspect ratio to fit within the available width. When an image is partially scrolled off the top of the chat window, the visible portion is still shown rather than disappearing entirely.

### Custom emoji rendering
Discord custom emoji (`:emojiname:`) in messages are rendered as images in-place, replacing the text placeholder. Only the image is shown — the `:name:` text is hidden.

### Jumbo emoji
Messages consisting entirely of custom emoji render at twice the normal height, similar to the large emoji display in Discord clients.

### Emoji picker preview
When typing `:` to open the emoji autocomplete popup, each custom emoji in the list is shown as a small image next to its name.

### Upload preview
When you send a message with an image attachment, the image is shown immediately from the local file while the upload is in progress, before the Discord CDN URL is available.

### Navigation
Blank placeholder rows reserved for images are automatically skipped during normal j/k chat navigation.

## Changelog

### 2026-05-10
- **Fix:** Messages with multiple image attachments no longer overlap — each attachment line now uses its own image's dimensions and URL for blank-row allocation and display mapping.
- **Fix:** Custom emoji images no longer disappear after the first chat redraw (e.g. when the Discord echo of a sent message replaces the pending message). The overlay now always re-renders after any draw cycle.

## License

GPL-3.0
