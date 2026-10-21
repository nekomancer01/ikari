"""Draw the app icon: assets/app.png, app-512.png (Linux), app.ico (Windows) and app.icns (macOS).

Run: python tools/make_icon.py (needs Pillow).
"""
import sys
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from subsentiment.gui.theme import ASSETS, THEMES

N = 1024  # draw large, downsample for smooth edges

# Flat: solid fills only, no gradients, sheen or shadows. Colours come from the light theme (the bars sit on
# a white bubble, as the charts do there): the tile is the accent, the bars the sentiment colours.
T = {k: ImageColor.getrgb(v) for k, v in THEMES["light"].items() if isinstance(v, str)}
TILE = T["accent"]
WHITE = (255, 255, 255, 255)
COLORS = [T["negative"], T["neutral"], T["positive"]]

icon = Image.new("RGBA", (N, N), (0, 0, 0, 0))
d = ImageDraw.Draw(icon)
pad = 64
d.rounded_rectangle((pad, pad, N - pad, N - pad), radius=220, fill=TILE)

# Speech bubble with three bars: posts and their sentiment.
bx0, by0, bx1, by1 = 230, 250, 794, 690
d.rounded_rectangle((bx0, by0, bx1, by1), radius=110, fill=WHITE)
d.polygon([(330, 660), (300, 800), (470, 680)], fill=WHITE)
heights = [150, 210, 300]
bw, gap = 92, 46
x = (bx0 + bx1) // 2 - (3 * bw + 2 * gap) // 2
base = 610
for c, h in zip(COLORS, heights):
    # Rounded top, square base: the same bar shape as the charts.
    d.rounded_rectangle((x, base - h, x + bw, base), radius=30, fill=c, corners=(True, True, False, False))
    x += bw + gap

# 16 px gets its own drawing on the pixel grid: no tail, wider bars and gaps, so the
# three bars stay separate instead of blurring into one smudge.
u = N // 16
small = Image.new("RGBA", (N, N), (0, 0, 0, 0))
d = ImageDraw.Draw(small)
d.rounded_rectangle((u, u, 15 * u, 15 * u), radius=int(3.5 * u), fill=TILE)
d.rounded_rectangle((3 * u, 3 * u, 13 * u, 13 * u), radius=2 * u, fill=WHITE)
for i, (c, h) in enumerate(zip(COLORS, (3, 5, 7))):
    x = (4 + 3 * i) * u
    d.rounded_rectangle((x, (12 - h) * u, x + 2 * u, 12 * u), radius=u // 2, fill=c, corners=(True, True, False, False))
small = small.resize((16, 16), Image.LANCZOS)

ASSETS.mkdir(parents=True, exist_ok=True)
sizes = (16, 24, 32, 48, 64, 128, 256, 512)
frames = [small] + [icon.resize((n, n), Image.LANCZOS) for n in sizes[1:]]
frames[6].save(ASSETS / "app.png")
frames[7].save(ASSETS / "app-512.png")
frames[6].save(ASSETS / "app.ico", sizes=[(n, n) for n in sizes[:7]], append_images=frames[:6])
icon.save(ASSETS / "app.icns", append_images=frames)  # every size macOS needs, up to 1024 px
print("wrote", *(ASSETS / n for n in ("app.png", "app-512.png", "app.ico", "app.icns")))
