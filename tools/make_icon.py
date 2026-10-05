"""Draw the tomato and save busyist.ico (16-256 px). Run: python tools/make_icon.py"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "busyist.ico"
S = 1024  # drawn large, then scaled down for clean edges


def tomato() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # body: a slightly squat circle with a soft lower shadow and a highlight
    body = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(body)
    box = (36, 170, S - 36, S - 24)
    d.ellipse(box, fill=(226, 64, 50, 255))
    shade = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shade).ellipse((140, 420, S - 40, S - 30), fill=(160, 30, 24, 105))
    shade = shade.filter(ImageFilter.GaussianBlur(70))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).ellipse(box, fill=255)
    body.alpha_composite(Image.composite(shade, Image.new("RGBA", (S, S)), mask))
    shine = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shine).ellipse((230, 330, 400, 440), fill=(255, 255, 255, 150))
    body.alpha_composite(shine.filter(ImageFilter.GaussianBlur(22)))
    img.alpha_composite(body)

    # leaves: a five-point calyx, then the stem
    d = ImageDraw.Draw(img)
    cx, cy = S // 2, 250
    leaf = (64, 160, 72, 255)
    for points in (
        [(cx, cy - 10), (cx - 250, cy - 60), (cx - 90, cy + 40)],
        [(cx, cy - 10), (cx + 250, cy - 60), (cx + 90, cy + 40)],
        [(cx - 40, cy), (cx - 170, cy + 130), (cx + 10, cy + 60)],
        [(cx + 40, cy), (cx + 170, cy + 130), (cx - 10, cy + 60)],
        [(cx - 60, cy), (cx, cy - 120), (cx + 60, cy)],
    ):
        d.polygon(points, fill=leaf)
    d.ellipse((cx - 70, cy - 50, cx + 70, cy + 60), fill=leaf)
    d.rounded_rectangle((cx - 22, cy - 175, cx + 22, cy - 20), radius=22, fill=(52, 128, 58, 255))
    return img


def main() -> None:
    big = tomato()
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    frames = [big.resize((n, n), Image.LANCZOS) for n in sizes]
    frames[-1].save(OUT, format="ICO", sizes=[(n, n) for n in sizes], append_images=frames[:-1])
    big.resize((512, 512), Image.LANCZOS).save(OUT.with_suffix(".png"))
    print("wrote", OUT, "and", OUT.with_suffix(".png").name)


if __name__ == "__main__":
    main()
