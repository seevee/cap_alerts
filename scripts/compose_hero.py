#!/usr/bin/env python3
"""Compose the README hero from the panels capture_hero.js wrote.

Usage: .venv/bin/python scripts/compose_hero.py <panel dir>

Left column: the entry's Sensors card over the six triggers, scaled to the
card's width. Right: the "Alert issued" sidebar. Writes docs/img/hero-light.webp,
hero-dark.webp (lossless, the README's click-through target) and
hero-adaptive.svg, which embeds both and switches on prefers-color-scheme so
GitHub and HACS render it without an external request.

Needs Pillow with WebP, which the test venv already has through Home Assistant.
"""

from __future__ import annotations

import base64
import io
import sys
from pathlib import Path

from PIL import Image, ImageChops

GAP, PAD = 48, 16
DOCS_IMG = Path(__file__).resolve().parent.parent / "docs" / "img"


def _trim(img: Image.Image, bg: tuple[int, ...]) -> Image.Image:
    """Crop to content, first painting a differing corner colour as ``bg``."""
    corner = img.getpixel((1, 1))
    if corner != bg:
        px = img.load()
        for y in range(img.height):
            for x in range(img.width):
                if px[x, y] == corner:
                    px[x, y] = bg
    box = ImageChops.difference(img, Image.new("RGB", img.size, bg)).getbbox()
    return img.crop(box) if box else img


def compose(panels: Path, theme: str) -> Image.Image:
    sensors = Image.open(panels / f"sensors-{theme}.png").convert("RGB")
    bg = sensors.getpixel((2, 2))
    sensors = _trim(sensors, bg)
    sidebar = _trim(Image.open(panels / f"sidebar-{theme}.png").convert("RGB"), bg)
    triggers = _trim(Image.open(panels / f"triggers-{theme}.png").convert("RGB"), bg)
    triggers = triggers.resize(
        (sensors.width, round(triggers.height * sensors.width / triggers.width)),
        Image.Resampling.LANCZOS,
    )
    left_h = sensors.height + GAP // 2 + triggers.height
    width = PAD + sensors.width + GAP + sidebar.width + PAD
    height = PAD + max(left_h, sidebar.height) + PAD
    out = Image.new("RGB", (width, height), bg)
    out.paste(sensors, (PAD, PAD))
    out.paste(triggers, (PAD, PAD + sensors.height + GAP // 2))
    out.paste(sidebar, (PAD + sensors.width + GAP, PAD))
    return out


def _webp(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "WEBP", lossless=True, method=6)
    return buf.getvalue()


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    panels = Path(sys.argv[1])
    images = {theme: compose(panels, theme) for theme in ("light", "dark")}
    # Captured at 2x, so the SVG's logical size is half the pixel size.
    w, h = images["light"].width // 2, images["light"].height // 2
    parts = []
    for theme, img in images.items():
        data = _webp(img)
        (DOCS_IMG / f"hero-{theme}.webp").write_bytes(data)
        b64 = base64.b64encode(data).decode()
        parts.append(
            f'  <image class="{theme}" href="data:image/webp;base64,{b64}"'
            f' width="{w}" height="{h}"/>'
        )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="100%">\n'
        "  <style>\n"
        "    .light { display: block; }\n"
        "    .dark { display: none; }\n"
        "    @media (prefers-color-scheme: dark) {\n"
        "      .light { display: none; }\n"
        "      .dark { display: block; }\n"
        "    }\n"
        "  </style>\n" + "\n".join(parts) + "\n</svg>\n"
    )
    (DOCS_IMG / "hero-adaptive.svg").write_text(svg, encoding="utf-8")
    for name in ("hero-light.webp", "hero-dark.webp", "hero-adaptive.svg"):
        size = (DOCS_IMG / name).stat().st_size
        print(f"{name}: {size // 1024} KB")
    print(f"logical size {w}x{h}")


if __name__ == "__main__":
    main()
