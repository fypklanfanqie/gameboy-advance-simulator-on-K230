#!/usr/bin/env python3
"""Render the production virtual controls over a generated mock game scene.

This is a layout preview, not a K230 capture or a ROM-derived screenshot.
The grass, path and water are drawn here; control geometry, labels and ARGB
colors come directly from src/gba_ui.py. Pillow approximates the board font.

Run: python tools/preview_gba_controls.py
"""

import argparse
from pathlib import Path
import random
import sys

from PIL import Image, ImageDraw, ImageFont
from PIL.PngImagePlugin import PngInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import gba_ui


def rgba(color):
    """The board's ARGB8888 drawing API takes (A, R, G, B)."""
    if isinstance(color, int):
        return (color, color, color, 255)
    if len(color) == 4:
        alpha, red, green, blue = color
        return (red, green, blue, alpha)
    if len(color) == 3:
        return tuple(color) + (255,)
    raise ValueError("Unsupported drawing color: %r" % (color,))


class BoardImagePreview:
    """Small Pillow adapter for the vendor methods used by draw_controls."""

    def __init__(self, width, height):
        self.image = Image.new("RGBA", (width, height))
        self.painter = ImageDraw.Draw(self.image)

    def clear(self):
        self.painter.rectangle((0, 0, self.image.width, self.image.height),
                               fill=(0, 0, 0, 0))

    def draw_rectangle(self, *args, color=(255, 255, 255), thickness=1,
                       fill=False, **kwargs):
        rect = args[0] if len(args) == 1 else args[:4]
        x, y, width, height = rect
        bounds = (x, y, x + width - 1, y + height - 1)
        self.painter.rectangle(bounds, fill=rgba(color) if fill else None,
                               outline=None if fill else rgba(color),
                               width=max(1, int(thickness)))
        return self

    def draw_circle(self, x, y, radius, color=(255, 255, 255), thickness=1,
                    fill=False, **kwargs):
        bounds = (x - radius, y - radius, x + radius, y + radius)
        self.painter.ellipse(bounds, fill=rgba(color) if fill else None,
                             outline=None if fill else rgba(color),
                             width=max(1, int(thickness)))
        return self

    def draw_string_advanced(self, x, y, char_size, text,
                             color=(255, 255, 255), **kwargs):
        font = None
        for name in ("C:/Windows/Fonts/arialbd.ttf",
                     "C:/Windows/Fonts/seguisb.ttf", "DejaVuSans-Bold.ttf"):
            try:
                font = ImageFont.truetype(name, int(char_size))
                break
            except OSError:
                pass
        if font is None:
            font = ImageFont.load_default(size=int(char_size))
        self.painter.text((x, y), text, font=font, fill=rgba(color), anchor="lt")
        return self


def mock_game(layout):
    """Create a deterministic, low contrast 240 x 160 pixel landscape."""
    scene = Image.new("RGB", (240, 160), (121, 157, 118))
    draw = ImageDraw.Draw(scene)
    rng = random.Random(230)
    for _ in range(1900):
        x, y = rng.randrange(240), rng.randrange(160)
        draw.point((x, y), fill=rng.choice(((114, 150, 111), (137, 165, 125),
                                           (126, 160, 120))))
    # Pond, bank, and occasional muted reflections.
    draw.polygon([(0, 21), (40, 21), (48, 28), (48, 61), (40, 69),
                  (28, 69), (28, 99), (16, 111), (0, 111)],
                 fill=(163, 177, 143))
    draw.polygon([(0, 25), (37, 25), (44, 31), (44, 58), (37, 65),
                  (24, 65), (24, 96), (13, 107), (0, 107)],
                 fill=(98, 145, 159))
    for y in range(30, 104, 12):
        for x in range(4, 38, 13):
            if scene.getpixel((x, y)) == (98, 145, 159):
                draw.line((x, y, x + 5, y), fill=(135, 168, 174))
    # Sandy route and a quiet central square.
    draw.rectangle((80, 0, 98, 160), fill=(183, 178, 142))
    draw.rectangle((80, 73, 239, 91), fill=(183, 178, 142))
    draw.rectangle((79, 71, 99, 93), fill=(192, 184, 150))
    for _ in range(230):
        x, y = rng.randrange(240), rng.randrange(160)
        if scene.getpixel((x, y)) == (183, 178, 142):
            draw.point((x, y), fill=(172, 167, 133))
    # Small building, shrubs, and flowers: all generated, no game assets.
    draw.rectangle((124, 27, 172, 57), fill=(178, 174, 151))
    draw.rectangle((121, 20, 175, 35), fill=(125, 120, 115))
    for y in (23, 27, 31):
        draw.line((122, y, 174, y), fill=(139, 132, 125))
    draw.rectangle((144, 44, 153, 57), fill=(105, 115, 105))
    for x in (130, 159):
        draw.rectangle((x, 40, x + 7, 47), fill=(115, 148, 152))
    for x, y in ((58, 18), (59, 120), (111, 131), (196, 36), (216, 111)):
        draw.rectangle((x + 4, y + 8, x + 7, y + 15), fill=(117, 117, 95))
        draw.ellipse((x, y, x + 12, y + 12), fill=(89, 130, 96))
        draw.line((x + 3, y + 3, x + 7, y + 3), fill=(125, 155, 116))
    for x, y in ((65, 68), (114, 48), (194, 131), (160, 111)):
        for dx, dy in ((0, 0), (5, 3), (2, 7)):
            draw.rectangle((x + dx, y + dy, x + dx + 1, y + dy + 1),
                           fill=(189, 172, 145))
    # A minimal original character identifies the central play area.
    draw.rectangle((113, 78, 118, 87), fill=(110, 116, 125))
    draw.rectangle((113, 74, 118, 79), fill=(178, 167, 145))
    draw.rectangle((112, 73, 119, 75), fill=(145, 123, 118))
    draw.point((114, 88), fill=(91, 101, 97))
    draw.point((117, 88), fill=(91, 101, 97))

    scale, off_x, off_y, screen_x, screen_y = gba_ui.layout_params(layout)
    output = Image.new("RGBA", (gba_ui.LCD_W, gba_ui.LCD_H), (36, 44, 43, 255))
    output.paste(scene.resize((240 * scale, 160 * scale), Image.Resampling.NEAREST),
                 (screen_x + off_x, screen_y + off_y))
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layout", choices=tuple(gba_ui.LAYOUT_SCALE),
                        default=gba_ui.LAYOUT)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "docs" / "virtual-controls-preview.png")
    args = parser.parse_args()
    overlay = BoardImagePreview(gba_ui.LCD_W, gba_ui.LCD_H)
    gba_ui.draw_controls(overlay, args.layout)
    # A transparent central area is essential; do not cover gameplay with a panel.
    assert overlay.image.getpixel((400, 240))[3] == 0
    for name, x, y, width, height in gba_ui.button_regions(args.layout):
        if name in ("A", "B"):
            assert overlay.image.getpixel((x, y))[3] == 0, name
        assert overlay.image.getpixel((x + width // 2, y + height // 2))[3] > 0, name
        # Inside the upper body, away from the border and label, has partial alpha.
        assert 0 < overlay.image.getpixel((x + width // 2, y + 5))[3] < 255, name
    preview = Image.alpha_composite(mock_game(args.layout), overlay.image).convert("RGB")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    metadata = PngInfo()
    metadata.add_text("Description",
                      "Generated layout preview; original mock scenery, no ROM assets "
                      "or hardware capture. Production gba_ui.draw_controls geometry "
                      "and ARGB colors; Pillow approximation of the board font.")
    preview.save(args.output, pnginfo=metadata)
    print("Generated mock layout preview: %s (%s, %s)" %
          (args.output, args.layout, preview.size))


if __name__ == "__main__":
    main()
