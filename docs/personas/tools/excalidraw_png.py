""".excalidraw 파일의 사각형·마름모·화살표·글자를 PIL로 그려 PNG로 저장한다. 손그림 효과는 없다.
사용: python docs/personas/tools/excalidraw_png.py docs/personas/workout-flow.excalidraw docs/personas/workout-flow.png
Pillow와 Windows 맑은 고딕 글꼴이 필요하다. 생성기 실행에는 쓰지 않으므로 requirements.txt에 넣지 않았다."""
import json
import math
import sys

from PIL import Image, ImageDraw, ImageFont

SCALE = 2
PAD = 40
FONT = "C:/Windows/Fonts/malgun.ttf"
FONT_BOLD = "C:/Windows/Fonts/malgunbd.ttf"

doc = json.load(open(sys.argv[1], encoding="utf-8"))
els = [e for e in doc["elements"] if not e.get("isDeleted")]


def bounds(e):
    if e["type"] == "arrow":
        xs = [e["x"] + p[0] for p in e["points"]]
        ys = [e["y"] + p[1] for p in e["points"]]
        return min(xs), min(ys), max(xs), max(ys)
    return e["x"], e["y"], e["x"] + e["width"], e["y"] + e["height"]


bs = [bounds(e) for e in els if not e.get("containerId")]
minx, miny = min(b[0] for b in bs) - PAD, min(b[1] for b in bs) - PAD
maxx, maxy = max(b[2] for b in bs) + PAD, max(b[3] for b in bs) + PAD
img = Image.new("RGB", (int((maxx - minx) * SCALE), int((maxy - miny) * SCALE)), "white")
d = ImageDraw.Draw(img)


def P(x, y):
    return ((x - minx) * SCALE, (y - miny) * SCALE)


def dashed_line(p1, p2, color, width, dash=10 * SCALE, gap=7 * SCALE):
    (x1, y1), (x2, y2) = p1, p2
    length = math.hypot(x2 - x1, y2 - y1)
    if length == 0:
        return
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    pos = 0
    while pos < length:
        end = min(pos + dash, length)
        d.line([(x1 + ux * pos, y1 + uy * pos), (x1 + ux * end, y1 + uy * end)], fill=color, width=width)
        pos += dash + gap


def outline(points, color, dashed, width):
    for a, b in zip(points, points[1:] + points[:1]):
        if dashed:
            dashed_line(a, b, color, width)
        else:
            d.line([a, b], fill=color, width=width)


W = 2 * SCALE
for e in els:
    t = e["type"]
    stroke = e["strokeColor"]
    fill = None if e["backgroundColor"] == "transparent" else e["backgroundColor"]
    dashed = e.get("strokeStyle") == "dashed"
    if t == "rectangle":
        box = [P(e["x"], e["y"]), P(e["x"] + e["width"], e["y"] + e["height"])]
        d.rounded_rectangle(box, radius=12 * SCALE, fill=fill, outline=None if dashed else stroke, width=W)
        if dashed:
            (x1, y1), (x2, y2) = box
            outline([(x1, y1), (x2, y1), (x2, y2), (x1, y2)], stroke, True, W)
    elif t == "ellipse":
        box = [P(e["x"], e["y"]), P(e["x"] + e["width"], e["y"] + e["height"])]
        d.ellipse(box, fill=fill, outline=stroke, width=W)
    elif t == "diamond":
        cx, cy = e["x"] + e["width"] / 2, e["y"] + e["height"] / 2
        pts = [P(cx, e["y"]), P(e["x"] + e["width"], cy), P(cx, e["y"] + e["height"]), P(e["x"], cy)]
        d.polygon(pts, fill=fill)
        outline(pts, stroke, dashed, W)
    elif t == "arrow":
        pts = [P(e["x"] + p[0], e["y"] + p[1]) for p in e["points"]]
        d.line(pts, fill=stroke, width=W, joint="curve")
        (x1, y1), (x2, y2) = pts[-2], pts[-1]
        ang = math.atan2(y2 - y1, x2 - x1)
        size = 12 * SCALE
        head = [(x2, y2),
                (x2 - size * math.cos(ang - 0.45), y2 - size * math.sin(ang - 0.45)),
                (x2 - size * math.cos(ang + 0.45), y2 - size * math.sin(ang + 0.45))]
        d.polygon(head, fill=stroke)

for e in els:
    if e["type"] != "text":
        continue
    size = e["fontSize"]
    font = ImageFont.truetype(FONT_BOLD if size >= 30 else FONT, int(size * 0.82 * SCALE))
    lines = e["text"].split("\n")
    line_h = size * 1.25
    if e.get("containerId"):
        c = next(x for x in els if x["id"] == e["containerId"])
        cx, cy = c["x"] + c["width"] / 2, c["y"] + c["height"] / 2
        top = cy - len(lines) * line_h / 2
        for i, line in enumerate(lines):
            w = d.textlength(line, font=font) / SCALE
            d.text(P(cx - w / 2, top + i * line_h + line_h * 0.12), line, fill=e["strokeColor"], font=font)
    else:
        for i, line in enumerate(lines):
            d.text(P(e["x"], e["y"] + i * line_h), line, fill=e["strokeColor"], font=font)

img.save(sys.argv[2])
print(img.size)
