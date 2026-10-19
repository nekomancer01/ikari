"""Draw the app icon: assets/app.png, app-512.png (Linux), app.ico (Windows) and app.icns (macOS).

Run: python tools/make_icon.py (needs Pillow).
"""
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ASSETS = Path(__file__).resolve().parents[1] / "src" / "subsentiment" / "gui" / "assets"
N = 1024  # draw large, downsample for smooth edges


def gradient(size, c0, c1):
    """Diagonal gradient from top-left c0 to bottom-right c1."""
    ramp = Image.linear_gradient("L").resize((size, size))
    diag = Image.blend(ramp.rotate(90), ramp, 0.5)  # 45 degree ramp
    return Image.composite(Image.new("RGB", (size, size), c1), Image.new("RGB", (size, size), c0), diag)


def rounded_mask(size, box, radius):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle(box, radius=radius, fill=255)
    return m


icon = Image.new("RGBA", (N, N), (0, 0, 0, 0))
tile = gradient(N, (61, 107, 255), (122, 92, 255)).convert("RGBA")
pad = 64
icon.paste(tile, (0, 0), rounded_mask(N, (pad, pad, N - pad, N - pad), 220))

# Soft highlight on the top half: the "glass" sheen.
sheen = Image.new("L", (N, N), 0)
ImageDraw.Draw(sheen).ellipse((-N * 0.3, -N * 0.75, N * 1.3, N * 0.45), fill=70)
sheen = ImageChops.multiply(sheen.filter(ImageFilter.GaussianBlur(40)),
                            rounded_mask(N, (pad, pad, N - pad, N - pad), 220))
icon = Image.composite(Image.new("RGBA", (N, N), (255, 255, 255, 255)), icon, sheen)

# Speech bubble with three bars: posts and their sentiment.
d = ImageDraw.Draw(icon)
bx0, by0, bx1, by1 = 230, 250, 794, 690
d.rounded_rectangle((bx0, by0, bx1, by1), radius=110, fill=(255, 255, 255, 245))
d.polygon([(330, 660), (300, 800), (470, 680)], fill=(255, 255, 255, 245))
colors = [(227, 73, 72), (138, 137, 132), (42, 120, 214)]  # negative, neutral, positive
heights = [150, 210, 300]
bw, gap = 92, 46
x = (bx0 + bx1) // 2 - (3 * bw + 2 * gap) // 2
base = 610
for c, h in zip(colors, heights):
    d.rounded_rectangle((x, base - h, x + bw, base), radius=26, fill=c)
    x += bw + gap

ASSETS.mkdir(parents=True, exist_ok=True)
icon.resize((256, 256), Image.LANCZOS).save(ASSETS / "app.png")
icon.resize((512, 512), Image.LANCZOS).save(ASSETS / "app-512.png")
icon.resize((256, 256), Image.LANCZOS).save(
    ASSETS / "app.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
icon.save(ASSETS / "app.icns")  # Pillow writes every size macOS needs, up to 1024 px
print("wrote", *(ASSETS / n for n in ("app.png", "app-512.png", "app.ico", "app.icns")))
