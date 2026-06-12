"""Render the README banner image.

Run from the repo root:  python docs/make_banner.py  ->  docs/banner.png
"""
from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 320
BG = (13, 17, 23)        # GitHub dark
FG = (230, 237, 243)
DIM = (139, 148, 158)
ORANGE = (255, 140, 60)
EMBER = (255, 95, 60)
GREEN = (63, 185, 80)

TITLE = ImageFont.truetype("C:/Windows/Fonts/CascadiaMono.ttf", 78)
SUB = ImageFont.truetype("C:/Windows/Fonts/CascadiaMono.ttf", 26)
CODE = ImageFont.truetype("C:/Windows/Fonts/CascadiaMono.ttf", 22)

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)

# faint ember glow along the bottom edge
for i in range(70):
    a = 70 - i
    d.line([(0, H - i), (W, H - i)], fill=(13 + a // 3, 17 + a // 8, 23))

# flame mark: three rounded bars rising like heat, left of the title
bars = [(0, 26, EMBER), (1, 12, ORANGE), (2, 34, EMBER)]
bx = 330
for n, rise, color in bars:
    x = bx + n * 26
    d.rounded_rectangle([x, 96 + rise, x + 16, 96 + 78], radius=8, fill=color)

d.text((430, 86), "backburner", font=TITLE, fill=FG)
d.text((434, 188), "background tasks for AI agents", font=SUB, fill=DIM)

line = "start_task  ->  keep working  ->  collect results later"
w = d.textlength(line, font=CODE)
d.text(((W - w) // 2, 258), line, font=CODE, fill=GREEN)

img.save("docs/banner.png")
print("wrote docs/banner.png")
