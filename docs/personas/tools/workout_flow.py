"""운동 기록 세션의 분기 그림을 .excalidraw 파일로 만든다.

사용: python docs/personas/tools/workout_flow.py docs/personas/workout-flow.excalidraw
PNG는 excalidraw_png.py로 만든다."""
import json
import random
import sys

rng = random.Random(1)
elements = []

TEAL = ("#c3fae8", "#0f766e")
YELLOW = ("#fff3bf", "#f59e0b")
PURPLE = ("#d0bfff", "#7c3aed")
BLUE = ("#a5d8ff", "#2563eb")
GREEN = ("#b2f2bb", "#15803d")
ORANGE = ("#ffd8a8", "#c2410c")
RED = ("#ffc9c9", "#dc2626")


def base(kind, id_, x, y, w, h, stroke="#1e1e1e", bg="transparent", dashed=False):
    return {
        "id": id_, "type": kind, "x": x, "y": y, "width": w, "height": h, "angle": 0,
        "strokeColor": stroke, "backgroundColor": bg, "fillStyle": "solid", "strokeWidth": 2,
        "strokeStyle": "dashed" if dashed else "solid", "roughness": 1, "opacity": 100,
        "groupIds": [], "frameId": None, "roundness": None, "seed": rng.randrange(1, 2**31),
        "version": 1, "versionNonce": rng.randrange(1, 2**31), "isDeleted": False,
        "boundElements": [], "updated": 1, "link": None, "locked": False,
    }


def text(id_, x, y, s, size=22, color="#1e1e1e", container=None, w=None, h=None):
    lines = s.split("\n")
    width = w if w is not None else max(sum(size if ord(c) > 0x2000 else size * 0.55 for c in line) for line in lines)
    height = h if h is not None else len(lines) * size * 1.25
    el = base("text", id_, x, y, width, height, stroke=color)
    el.update({
        "text": s, "originalText": s, "fontSize": size, "fontFamily": 5, "lineHeight": 1.25,
        "textAlign": "center" if container else "left", "verticalAlign": "middle" if container else "top",
        "containerId": container, "autoResize": True,
    })
    return el


def shape(kind, id_, x, y, w, h, colors, label, size=22, dashed=False):
    bg, stroke = colors
    el = base(kind, id_, x, y, w, h, stroke=stroke, bg=bg, dashed=dashed)
    if kind == "rectangle":
        el["roundness"] = {"type": 3}
    lines = label.split("\n")
    th = len(lines) * size * 1.25
    tw = w * (0.55 if kind == "diamond" else 0.92)
    t = text(f"{id_}_t", x + (w - tw) / 2, y + (h - th) / 2, label, size, container=id_, w=tw, h=th)
    el["boundElements"].append({"type": "text", "id": t["id"]})
    elements.extend([el, t])
    return el


def arrow(id_, x, y, points, color="#1e1e1e", start=None, end=None, label=None):
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    el = base("arrow", id_, x, y, max(xs) - min(xs), max(ys) - min(ys), stroke=color)
    el.update({
        "points": points, "lastCommittedPoint": None, "startArrowhead": None, "endArrowhead": "arrow",
        "startBinding": {"elementId": start, "focus": 0, "gap": 1} if start else None,
        "endBinding": {"elementId": end, "focus": 0, "gap": 1} if end else None,
        "roundness": None, "elbowed": False,
    })
    elements.append(el)
    for target in (start, end):
        if target:
            next(e for e in elements if e["id"] == target)["boundElements"].append({"type": "arrow", "id": id_})
    if label:
        lx, ly, s, c = label
        elements.append(text(f"{id_}_l", lx, ly, s, 20, c))


GRAY = ("#e9ecef", "#495057")
NW, NH = 400, 50  # 노드 크기
SX = 700 - NW / 2  # 가운데 줄 노드의 x
LX = 250 - NW / 2  # 왼쪽 줄(루틴 회원)
RX = 1150 - NW / 2  # 오른쪽 줄(자유 운동 회원, 갈래)


def node(id_, x, y, action, api, colors, dashed=False):
    return shape("rectangle", id_, x, y, NW, NH, colors, f"{action}\n{api}", 18, dashed)


def cond(id_, cy, question, h=90, w=300):
    return shape("diamond", id_, 700 - w / 2, cy - h / 2, w, h, YELLOW, question, 20)


def down(id_, x, y1, y2, start, end):
    arrow(id_, x, y1, [[0, 0], [0, y2 - y1]], start=start, end=end)


def label(id_, x, y, s, color="#b45309"):
    elements.append(text(id_, x, y, s, 18, color))


elements.append(text("title", 560, -70, "운동 기록 세션", 32))

# 홈 화면과 루틴 목록
node("n1", SX, 0, "홈 진입", "GET /daily-summaries/{date}", TEAL)
node("n2", SX, 80, "홈 진입", "GET /daily-goals/{date}", TEAL)
down("e1", 700, 50, 80, "n1", "n2")
node("n3", SX, 160, "홈 진입", "GET /growth/me", TEAL)
down("e2", 700, 130, 160, "n2", "n3")
node("n4", SX, 240, "운동 탭 진입", "GET /routines", TEAL)
down("e3", 700, 210, 240, "n3", "n4")
cond("d1", 365, "루틴 유무")
down("e4", 700, 290, 320, "n4", "d1")
cond("d2", 490, "루틴 직접 생성\n회원 여부", 100, 300)
down("e5", 700, 410, 440, "d1", "d2")
label("l_e5", 712, 410, "없음")

# 루틴을 만드는 회원의 준비
node("s1", LX, 580, "종목 목록 받기", "[setup] GET /exercises", PURPLE)
arrow("e6", 550, 490, [[0, 0], [-300, 0], [-300, 90]], "#7c3aed", "d2", "s1", (380, 460, "예 (30%)", "#7c3aed"))
node("s2", LX, 660, "주간 목표 설정", "[setup] PUT /weekly-workout-goal/me", PURPLE)
down("e7", 250, 630, 660, "s1", "s2")
node("s3", LX, 740, "루틴 만들기", "[setup] POST /routines", PURPLE)
down("e8", 250, 710, 740, "s2", "s3")

# 루틴 회원의 일정
node("r1", LX, 820, "일정 조회", "GET /workout-schedules", BLUE)
down("e9", 250, 790, 820, "s3", "r1")
arrow("e10", 550, 365, [[0, 0], [-530, 0], [-530, 480], [-500, 480]], "#2563eb", "d1", "r1", (400, 335, "있음", "#2563eb"))
shape("diamond", "dl", 100, 900, 300, 90, YELLOW, "일정 유무", 20)
down("e11", 250, 870, 900, "r1", "dl")
node("r2", LX, 1020, "일정 예약", "POST /workout-schedules", BLUE)
down("e12", 250, 990, 1020, "dl", "r2")
label("l_e12", 262, 990, "없음")

# 자유 운동 회원의 일정
node("f1", RX, 580, "일정 조회", "GET /workout-schedules", GREEN)
arrow("e13", 850, 490, [[0, 0], [300, 0], [300, 90]], "#15803d", "d2", "f1", (900, 460, "아니오 (70%)", "#15803d"))

# 합류
cond("db", 1175, "둘러보기 여부\n(30%)")
arrow("e14", 250, 1070, [[0, 0], [0, 30], [450, 30], [450, 60]], "#2563eb", "r2", "db")
arrow("e15", 400, 945, [[0, 0], [60, 0], [60, 155]], "#2563eb", "dl", None, (410, 915, "있음", "#b45309"))
arrow("e16", 1150, 630, [[0, 0], [0, 470], [-450, 470]], "#15803d", "f1", None)

node("b1", RX, 1150, "종목 둘러보기", "GET /exercises?bodyPart", ORANGE, dashed=True)
arrow("e17", 850, 1175, [[0, 0], [110, 0]], "#c2410c", "db", "b1", (880, 1145, "예", "#c2410c"))
node("ns", SX, 1260, "오늘 기록 조회", "GET /workout-sessions", TEAL)
down("e18", 700, 1220, 1260, "db", "ns")
label("l_e18", 640, 1222, "아니오")
arrow("e19", 1150, 1200, [[0, 0], [0, 40], [-450, 40]], "#c2410c", "b1", None)

cond("dt", 1385, "오늘 기록 유무")
down("e20", 700, 1310, 1340, "ns", "dt")
shape("ellipse", "end1", 1060, 1355, 180, 60, RED, "끝", 20)
arrow("e21", 850, 1385, [[0, 0], [210, 0]], "#dc2626", "dt", "end1", (900, 1355, "있음", "#dc2626"))

cond("dc", 1505, "종목 목록\n사본 유무")
down("e22", 700, 1430, 1460, "dt", "dc")
label("l_e22", 660, 1430, "없음")
node("c1", RX, 1480, "종목 목록 받기", "[setup] GET /exercises", PURPLE)
arrow("e23", 850, 1505, [[0, 0], [110, 0]], "#7c3aed", "dc", "c1", (880, 1475, "없음", "#7c3aed"))
arrow("e24", 1150, 1530, [[0, 0], [0, 35], [-450, 35]], "#7c3aed", "c1", None)

cond("dm", 1625, "검색 실패 여부\n(2%)")
down("e25", 700, 1550, 1580, "dc", "dm")
label("l_e25", 660, 1548, "있음")
node("m1", RX, 1600, "종목 검색", "GET /exercises?keyword", ORANGE, dashed=True)
arrow("e26", 850, 1625, [[0, 0], [110, 0]], "#c2410c", "dm", "m1", (880, 1595, "예", "#c2410c"))
node("m2", RX, 1680, "부위별 탐색", "GET /exercises?bodyPart", ORANGE, dashed=True)
down("e27", 1150, 1650, 1680, "m1", "m2")
arrow("e28", 1150, 1730, [[0, 0], [0, 15], [-450, 15]], "#c2410c", "m2", None)

node("nr", SX, 1760, "이전 세트 불러오기", "GET /workout-sessions/recent-sets", TEAL)
down("e29", 700, 1670, 1760, "dm", "nr")
label("l_e29", 640, 1672, "아니오")
node("np", SX, 1840, "운동 기록 저장", "POST /workout-sessions", TEAL)
down("e30", 700, 1810, 1840, "nr", "np")
node("ng", SX, 1920, "주간 목표 확인", "GET /weekly-workout-goal/me", TEAL)
down("e31", 700, 1890, 1920, "np", "ng")
cond("dg", 2050, "자유 운동 회원의\n목표 미설정 여부", 100, 300)
down("e32", 700, 1970, 2000, "ng", "dg")
node("g1", RX, 2025, "주간 목표 설정", "[setup] PUT /weekly-workout-goal/me", PURPLE)
arrow("e33", 850, 2050, [[0, 0], [110, 0]], "#7c3aed", "dg", "g1", (880, 2020, "예", "#7c3aed"))
shape("ellipse", "end2", 610, 2140, 180, 60, GRAY, "끝", 20)
down("e34", 700, 2100, 2140, "dg", "end2")
label("l_e34", 640, 2104, "아니오")
arrow("e35", 1150, 2075, [[0, 0], [0, 95], [-360, 95]], "#7c3aed", "g1", "end2")

# 범례
elements.append(text("lg", 40, 1300, "범례", 22))
for i, (colors, name, dashed) in enumerate([
    (TEAL, "모든 운동 세션", False), (PURPLE, "준비 [setup]", False), (BLUE, "루틴 회원", False),
    (GREEN, "자유 운동 회원", False), (ORANGE, "일부 세션만 (점선)", True), (YELLOW, "분기", False),
]):
    shape("rectangle", f"lg{i}", 40, 1340 + i * 56, 260, 44, colors, name, 18, dashed)

SY = 1.4
for e in elements:
    if e["type"] == "text" and e.get("containerId"):
        continue
    e["y"] *= SY
    if e["type"] == "text":
        continue
    e["height"] *= SY
    if "points" in e:
        e["points"] = [[px, py * SY] for px, py in e["points"]]
for e in elements:
    if e["type"] == "text" and e.get("containerId"):
        c = next(x for x in elements if x["id"] == e["containerId"])
        e["y"] = c["y"] + (c["height"] - e["height"]) / 2

doc = {
    "type": "excalidraw", "version": 2, "source": "https://excalidraw.com",
    "elements": elements,
    "appState": {"viewBackgroundColor": "#ffffff", "gridSize": None},
    "files": {},
}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(doc, f, ensure_ascii=False, indent=2)
print(len(elements), "elements")
