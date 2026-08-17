"""Render the QuickCode ghost mark to PNG and ICO rasters.

The canonical mark is ``quickcode/frontend/assets/icon.svg``; this script
re-draws the same geometry with Pillow (a semicircular dome, straight sides, a
four-lobe wavy hem and two eyes) so the rasters stay in step with it. Every
size is drawn natively at 4x and downsampled, which keeps the 16px favicon
crisp instead of mushy.

Pillow is a build-time-only dependency -- install it into your venv, not into
the project's dependencies::

    uv pip install pillow
    .venv/Scripts/python.exe scripts/make_icons.py

Outputs (all committed):
    quickcode/frontend/assets/icon-192.png
    quickcode/frontend/assets/icon-512.png
    quickcode/frontend/assets/favicon.ico      (16/32/48/64)
    packaging/quickcode.ico                    (16/32/48/64/256)
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

# --- Geometry, in the SVG's 128x128 user space -----------------------------

VIEWBOX = 128.0
CENTER_X, DOME_Y, RADIUS = 64.0, 56.0, 42.0
HEM_Y = 104.0
LOBE_DEEP, LOBE_HIGH = 118.0, 90.0  # quadratic control points of the hem

GRADIENT_TOP_Y, GRADIENT_BOTTOM_Y = 14.0, 118.0
COLOR_TOP = (0x7D, 0xB4, 0xFF)
COLOR_BOTTOM = (0x3D, 0x7F, 0xE0)
COLOR_EYE = (0x14, 0x29, 0x4D)

EYES = [(50.0, 58.0), (78.0, 58.0)]
EYE_RX, EYE_RY = 8.5, 10.0

SUPERSAMPLE = 4
ARC_STEPS = 96
CURVE_STEPS = 24

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS = REPO_ROOT / "quickcode" / "frontend" / "assets"
PACKAGING = REPO_ROOT / "packaging"

PNG_SIZES = (192, 512)
FAVICON_SIZES = (16, 32, 48, 64)
APP_ICON_SIZES = (16, 32, 48, 64, 256)


def _quadratic(p0: tuple[float, float], ctrl: tuple[float, float],
               p1: tuple[float, float]) -> list[tuple[float, float]]:
    """Sample a quadratic Bezier, excluding its start point."""
    points = []
    for step in range(1, CURVE_STEPS + 1):
        t = step / CURVE_STEPS
        inv = 1.0 - t
        x = inv * inv * p0[0] + 2 * inv * t * ctrl[0] + t * t * p1[0]
        y = inv * inv * p0[1] + 2 * inv * t * ctrl[1] + t * t * p1[1]
        points.append((x, y))
    return points


def ghost_outline() -> list[tuple[float, float]]:
    """The ghost silhouette as a closed polygon in 128x128 user space."""
    points: list[tuple[float, float]] = []

    # Dome: the upper semicircle, left rim over the top to the right rim.
    for step in range(ARC_STEPS + 1):
        angle = math.pi + math.pi * (step / ARC_STEPS)
        points.append((CENTER_X + RADIUS * math.cos(angle),
                       DOME_Y + RADIUS * math.sin(angle)))

    # Right side down to the hem.
    points.append((CENTER_X + RADIUS, HEM_Y))

    # Wavy hem, right to left: lobes alternate below and above the hem line.
    hem_x = [106.0, 85.0, 64.0, 43.0, 22.0]
    controls = [(95.5, LOBE_DEEP), (74.5, LOBE_HIGH), (53.5, LOBE_DEEP), (32.5, LOBE_HIGH)]
    for index, ctrl in enumerate(controls):
        start = (hem_x[index], HEM_Y)
        end = (hem_x[index + 1], HEM_Y)
        points.extend(_quadratic(start, ctrl, end))

    return points


def _vertical_gradient(size: int) -> Image.Image:
    """The body gradient, painted edge to edge.

    Filling the whole canvas (not just the silhouette) means the pixels under
    the transparent margin still carry ghost blue, so downsampling can't pull a
    dark halo in around the outline.
    """
    gradient = Image.new("RGB", (size, size))
    draw = ImageDraw.Draw(gradient)
    scale = size / VIEWBOX
    top = GRADIENT_TOP_Y * scale
    span = max((GRADIENT_BOTTOM_Y - GRADIENT_TOP_Y) * scale, 1.0)
    for y in range(size):
        t = min(max((y - top) / span, 0.0), 1.0)
        draw.line(
            [(0, y), (size, y)],
            fill=tuple(round(a + (b - a) * t) for a, b in zip(COLOR_TOP, COLOR_BOTTOM, strict=True)),
        )
    return gradient


def render(size: int) -> Image.Image:
    """Render the mark at ``size`` px square, supersampled then downsampled."""
    big = size * SUPERSAMPLE
    scale = big / VIEWBOX

    body_mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(body_mask).polygon(
        [(x * scale, y * scale) for x, y in ghost_outline()], fill=255
    )

    image = _vertical_gradient(big)
    eye_draw = ImageDraw.Draw(image)
    for cx, cy in EYES:
        eye_draw.ellipse(
            [((cx - EYE_RX) * scale, (cy - EYE_RY) * scale),
             ((cx + EYE_RX) * scale, (cy + EYE_RY) * scale)],
            fill=COLOR_EYE,
        )

    image = image.convert("RGBA")
    image.putalpha(body_mask)
    return image.resize((size, size), Image.LANCZOS)


def write_png(size: int, path: Path) -> None:
    render(size).save(path, format="PNG", optimize=True)
    print(f"  {path.relative_to(REPO_ROOT)}  ({size}x{size})")


def write_ico(sizes: tuple[int, ...], path: Path) -> None:
    """Write a multi-size ICO, each frame drawn natively rather than rescaled."""
    frames = [render(size) for size in sorted(sizes)]
    largest = frames[-1]
    largest.save(
        path,
        format="ICO",
        sizes=[(size, size) for size in sorted(sizes)],
        append_images=frames[:-1],
    )
    print(f"  {path.relative_to(REPO_ROOT)}  ({'/'.join(str(s) for s in sorted(sizes))})")


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    PACKAGING.mkdir(parents=True, exist_ok=True)

    print("Rendering the QuickCode ghost:")
    for size in PNG_SIZES:
        write_png(size, ASSETS / f"icon-{size}.png")
    write_ico(FAVICON_SIZES, ASSETS / "favicon.ico")
    write_ico(APP_ICON_SIZES, PACKAGING / "quickcode.ico")


if __name__ == "__main__":
    main()
