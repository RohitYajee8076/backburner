"""Render the README demo GIF: a fake terminal session showing backburner's flow.

Run from the repo root:  python docs/make_demo_gif.py  ->  docs/demo.gif
"""
from PIL import Image, ImageDraw, ImageFont

FONT = ImageFont.truetype("C:/Windows/Fonts/CascadiaMono.ttf", 17)
W, H = 860, 430
BG = (13, 17, 23)        # GitHub dark
BAR = (33, 38, 45)
FG = (201, 209, 217)
GREEN = (63, 185, 80)
CYAN = (86, 182, 194)
YELLOW = (210, 168, 100)
DIM = (110, 118, 129)
LINE_H = 26
PAD_X, PAD_TOP = 22, 56

# (text, color, typed?)  -- typed lines animate char by char
SCRIPT = [
    ("agent> start_task(\"pytest -q\")", GREEN, True),
    ("  { \"task_id\": \"a1b2c3d4\", \"status\": \"working\" }", FG, False),
    ("", FG, False),
    ("  ...the agent keeps working on other things...", DIM, False),
    ("", FG, False),
    ("agent> task_result(\"a1b2c3d4\")        # peek mid-run", GREEN, True),
    ("  { \"status\": \"working\",", FG, False),
    ("    \"output\": \"tests/test_api.py ........ [ 41%]\" }", FG, False),
    ("", FG, False),
    ("agent> task_status(\"a1b2c3d4\")", GREEN, True),
    ("  { \"status\": \"completed\", \"duration_seconds\": 312 }", CYAN, False),
    ("", FG, False),
    ("agent> task_result(\"a1b2c3d4\")", GREEN, True),
    ("  { \"output\": \"418 passed in 311.2s\" }", YELLOW, False),
]


def frame(lines):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 36], fill=BAR)
    for i, cx in enumerate((255, 95, 86), (255, 189, 46), (39, 201, 63)) if False else enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([16 + i * 24, 12, 28 + i * 24, 24], fill=cx)
    d.text((W // 2 - 90, 9), "backburner  -  MCP", font=FONT, fill=DIM)
    for i, (text, color) in enumerate(lines):
        d.text((PAD_X, PAD_TOP + i * LINE_H), text, font=FONT, fill=color)
    return img


frames, durations = [], []
done = []
for text, color, typed in SCRIPT:
    if typed:
        for n in range(0, len(text) + 1, 3):
            frames.append(frame(done + [(text[:n] + "_", color)]))
            durations.append(45)
        done.append((text, color))
        frames.append(frame(done))
        durations.append(500)
    else:
        done.append((text, color))
        if text.strip():
            frames.append(frame(done))
            durations.append(900 if text.strip().startswith(("{", "\"", "...")) or "}" in text else 400)

durations[-1] = 3500  # hold the final result before looping

frames[0].save("docs/demo.gif", save_all=True, append_images=frames[1:],
               duration=durations, loop=0, optimize=True)
print(f"docs/demo.gif: {len(frames)} frames")
