"""
Draw the buZyist symbol (the tomato with the Z, buzyist-handoff
assets/logo/simbolo.svg) and save busyist.ico (16-256 px), busyist.png and
the browser extension's icon.png. Run: python tools/make_icon.py

The handoff's square app icon (Nata background, no corners) is meant for
systems that mask icons themselves; Windows doesn't, so the .ico is the
symbol on a transparent background.
"""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "busyist.ico"
S = 1024  # drawn large, then scaled down for clean edges
K = S / 120  # the symbol's viewBox is 120 x 120

TOMATE = (217, 54, 27, 255)
FOLHA = (30, 107, 67, 255)
NATA = (255, 247, 243, 255)


def _ellipse(img: Image.Image, cx, cy, rx, ry, fill, angle=0.0) -> None:
    """An ellipse in viewBox units, rotated `angle` degrees about its centre (SVG sense)."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).ellipse(((cx - rx) * K, (cy - ry) * K, (cx + rx) * K, (cy + ry) * K), fill=fill)
    if angle:
        # PIL rotates counter-clockwise; SVG's rotate() is clockwise on screen.
        layer = layer.rotate(-angle, resample=Image.BICUBIC, center=(cx * K, cy * K))
    img.alpha_composite(layer)


def symbol() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    _ellipse(img, 60, 68, 47, 42, TOMATE)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((57 * K, 8 * K, 63 * K, 26 * K), radius=3 * K, fill=FOLHA)
    _ellipse(img, 46, 27, 16, 7, FOLHA, -16)
    _ellipse(img, 74, 27, 16, 7, FOLHA, 16)
    # the Z: a polyline with round ends and joins, stroke 11
    d = ImageDraw.Draw(img)
    points = [(42 * K, 53 * K), (78 * K, 53 * K), (42 * K, 87 * K), (78 * K, 87 * K)]
    w = 11 * K
    d.line(points, fill=NATA, width=round(w), joint="curve")
    for x, y in points:
        d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=NATA)
    return img


def main() -> None:
    big = symbol()
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    frames = [big.resize((n, n), Image.LANCZOS) for n in sizes]
    frames[-1].save(OUT, format="ICO", sizes=[(n, n) for n in sizes], append_images=frames[:-1])
    big.resize((512, 512), Image.LANCZOS).save(OUT.with_suffix(".png"))
    big.resize((128, 128), Image.LANCZOS).save(ROOT / "extension" / "icon.png")
    print("wrote", OUT.name, OUT.with_suffix(".png").name, "and extension/icon.png")


if __name__ == "__main__":
    main()
