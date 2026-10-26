"""为 Spine 3.8 的阿尔比恩脸图生成「擦眼」软 mask（本地素材工具，产物不入库）。

每张脸算出一块 0..255 的软 mask，运行时按 `a = round(a*(255-m)/255)` 把眼睛擦成透明，
底下的真眼（会眨、会看人）就从洞里透出来。规则来自 2026-10-26 的 v5–v7 试点：

  上限   = 该脸眼槽多边形并集外扩 6px（多边形由 `--geometry` 的骨架反投影给出）
  本体   = 眼白(lum>195,sat<45) ∪ 虹膜(sat>60) → 闭运算 + 填孔 → 每只眼凸包（高光一律擦掉）
  暗色   = 只保留与本体 4 连通的部分（排线、睫毛；不连通的不要）
  眉毛   = b-r>=3 且与本体不连通 → 减掉（紫眉保住）
  竖直   = 上沿 = 本体上缘 -8px（r-b>=22 的暖色眼皮弧并入）；下沿 = 本体下缘 +2px（腮红排线留着）
  收尾   = 膨胀 2px → 高斯羽化 1.5px

用法：
  python pet/tools/spine_face_eye_masks.py
  python pet/tools/spine_face_eye_masks.py --faces 1 2 5
输入：
  模型 zip（atlas + 页面 PNG）        默认 shell/packages/stage-ui/src/assets/spine/models/albion-spine-38.zip
  几何 albion-eye-geometry.json       默认与 zip 同目录；由 pet/tools/spine_face_eye_geometry_dump.py 采
输出：
  <zip 同目录>/albion-eye-masks/manifest.json + mask-<face>.png
  tests/output/spine-eye-masks-20261026/face-<face>.png  （原 vs 擦后 5×，供人眼核对）
"""
from __future__ import annotations

import argparse
import io
import json
import zipfile
from collections import deque
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

REPO = Path(__file__).resolve().parents[2]
DEFAULT_MODELS = REPO / "shell/packages/stage-ui/src/assets/spine/models"
DEFAULT_ZIP = DEFAULT_MODELS / "albion-spine-38.zip"
DEFAULT_SHEETS = REPO / "tests/output/spine-eye-masks-20261026"

# 脸 9（漩涡眼）和 10（一睁一闭）是表情眼，不擦；其余 9 张都是普通睁眼。
ERASABLE = ["1", "2", "3", "4", "5", "6", "7", "8", "11"]
EYE_SLOTS = ["youyan-1", "youyan-3", "youyan-4", "youyan-5", "youyan-6", "youyan-7", "youyan-8", "youyan-9",
             "zuoyan-1", "zuoyan-2", "zuoyan-3", "zuoyan-4", "zuoyan-5", "zuoyan-6", "zuoyan-7", "zuoyan-8"]
POLY_DILATE_PX = 6
UPPER_MARGIN_PX = 8
LOWER_MARGIN_PX = 2
GROW_PX = 2
FEATHER_PX = 1.5


def parse_atlas(text: str) -> dict[str, dict]:
    """atlas 文本 → {region 名: {page, xy, size, rotate}}。"""
    out, page = {}, None
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line and not line[0].isspace() and line.endswith(".png"):
            page = line
        elif line and not line[0].isspace():
            name = line.split(":")[0]
            i += 1
            props = {}
            while i < len(lines) and lines[i][:2] == "  ":
                kv = lines[i].strip()
                if ":" in kv:
                    k, v = kv.split(":", 1)
                    props[k.strip()] = v.strip()
                i += 1
            if "xy" not in props:
                continue
            xy = [int(float(v)) for v in props["xy"].split(",")]
            size = [int(float(v)) for v in props["size"].split(",")]
            out[name] = {"page": page, "xy": xy, "size": size, "rotate": props.get("rotate", "false") == "true"}
            continue
        i += 1
    return out


def bary_uv(face: dict, pt):
    """世界坐标 → 脸图 regionUV（点在网格三角形里做重心插值）。"""
    wv, ruv, tris = face["world"], face["regionUVs"], face["triangles"]
    px, py = pt
    for t in range(0, len(tris), 3):
        i, j, k = tris[t], tris[t + 1], tris[t + 2]
        ax, ay = wv[i * 2], wv[i * 2 + 1]
        bx, by = wv[j * 2], wv[j * 2 + 1]
        cx, cy = wv[k * 2], wv[k * 2 + 1]
        d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(d) < 1e-9:
            continue
        l1 = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / d
        l2 = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / d
        l3 = 1 - l1 - l2
        if l1 < -0.02 or l2 < -0.02 or l3 < -0.02:
            continue
        return [l1 * ruv[i * 2] + l2 * ruv[j * 2] + l3 * ruv[k * 2],
                l1 * ruv[i * 2 + 1] + l2 * ruv[j * 2 + 1] + l3 * ruv[k * 2 + 1]]
    return None


def uv_to_page(atlas: dict, uv):
    """regionUV(0..1) → 页面像素。V 不翻转：这套 atlas 的 xy 就是 region 左上角，
    和拼图里量出来的眼睛位置对得上（2026-10-26 用 v2 的 mask 重叠率校过）。"""
    x, y = atlas["xy"]
    w, h = atlas["size"]
    return x + uv[0] * w, y + uv[1] * h


def dilate(m, W, H, rad, limit=None):
    out = [[False] * W for _ in range(H)]
    r2 = rad * rad
    for y in range(H):
        for x in range(W):
            if not m[y][x]:
                continue
            for dy in range(-rad, rad + 1):
                for dx in range(-rad, rad + 1):
                    if dx * dx + dy * dy > r2:
                        continue
                    bx, by = x + dx, y + dy
                    if 0 <= bx < W and 0 <= by < H and (limit is None or limit[by][bx]):
                        out[by][bx] = True
    return out


def erode(m, W, H, rad):
    inv = [[not m[y][x] for x in range(W)] for y in range(H)]
    return [[not v for v in row] for row in dilate(inv, W, H, rad)]


def fill_holes(m, W, H):
    outside = [[False] * W for _ in range(H)]
    q = deque()
    for x in range(W):
        for y in (0, H - 1):
            if not m[y][x] and not outside[y][x]:
                outside[y][x] = True; q.append((x, y))
    for y in range(H):
        for x in (0, W - 1):
            if not m[y][x] and not outside[y][x]:
                outside[y][x] = True; q.append((x, y))
    while q:
        ax, ay = q.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            bx, by = ax + dx, ay + dy
            if 0 <= bx < W and 0 <= by < H and not m[by][bx] and not outside[by][bx]:
                outside[by][bx] = True; q.append((bx, by))
    return [[m[y][x] or not outside[y][x] for x in range(W)] for y in range(H)]


def convex_hull(points):
    pts = sorted(set(points))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def comps8(m, W, H, min_px=0):
    lab = [[-1] * W for _ in range(H)]
    out = []
    for y in range(H):
        for x in range(W):
            if not m[y][x] or lab[y][x] >= 0:
                continue
            i = len(out); lab[y][x] = i; q = deque([(x, y)]); cells = []
            while q:
                ax, ay = q.popleft(); cells.append((ax, ay))
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        bx, by = ax + dx, ay + dy
                        if 0 <= bx < W and 0 <= by < H and m[by][bx] and lab[by][bx] < 0:
                            lab[by][bx] = i; q.append((bx, by))
            if len(cells) >= min_px:
                out.append(cells)
    return out


def build_mask(page_img: Image.Image, face_name: str, face: dict, atlas: dict, eye_world: dict):
    """返回 (mask Image, rect(x,y,w,h), stats)。rect 是 mask 在页面上的位置。"""
    rx, ry = atlas["xy"]
    rw, rh = atlas["size"]
    polys = []
    for slot in EYE_SLOTS:
        w = eye_world.get(slot) or []
        pts = []
        for i in range(0, len(w) - 1, 2):
            if w[i] is None or w[i + 1] is None:
                continue
            uv = bary_uv(face, (w[i], w[i + 1]))
            if uv:
                pts.append(uv_to_page(atlas, uv))
        if len(pts) >= 3:
            polys.append(pts)
    if not polys:
        raise SystemExit(f"脸 {face_name}: 没有可用的眼槽多边形")

    xs = [p[0] for poly in polys for p in poly]
    ys = [p[1] for poly in polys for p in poly]
    x0 = max(int(rx), int(min(xs)) - POLY_DILATE_PX - 4)
    y0 = max(int(ry), int(min(ys)) - POLY_DILATE_PX - 4)
    x1 = min(int(rx + rw), int(max(xs)) + POLY_DILATE_PX + 4)
    y1 = min(int(ry + rh), int(max(ys)) + POLY_DILATE_PX + 4)
    W, H = x1 - x0, y1 - y0
    win = page_img.crop((x0, y0, x1, y1))
    wp = win.load()

    poly_img = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(poly_img)
    for poly in polys:
        d.polygon([(p[0] - x0, p[1] - y0) for p in poly], fill=255)
    lim = poly_img.filter(ImageFilter.MaxFilter(2 * POLY_DILATE_PX + 1)).load()

    seed = [[False] * W for _ in range(H)]
    ink = [[False] * W for _ in range(H)]
    browc = [[False] * W for _ in range(H)]
    warm = [[False] * W for _ in range(H)]
    for y in range(H):
        for x in range(W):
            r, g, b, a = wp[x, y]
            if a < 40:
                continue
            lum = 0.299 * r + 0.587 * g + 0.114 * b
            sat = max(r, g, b) - min(r, g, b)
            iswhite = lum > 195 and sat < 45
            isiris = sat > 60
            dark = lum < 105
            inlim = lim[x, y] > 127
            seed[y][x] = inlim and (iswhite or isiris)
            ink[y][x] = inlim and (dark or iswhite or isiris)
            browc[y][x] = inlim and (b - r) >= 3 and 50 <= lum <= 210
            warm[y][x] = inlim and (r - b) >= 22 and 150 <= lum <= 240

    bodies = [c for c in comps8(seed, W, H) if len(c) >= 40]
    if not bodies:
        raise SystemExit(f"脸 {face_name}: 找不到眼睛本体（白/虹膜）")
    hull_img = Image.new("L", (W, H), 0)
    hd = ImageDraw.Draw(hull_img)
    spans = []
    for c in bodies:
        hp = convex_hull(c)
        if len(hp) >= 3:
            hd.polygon(hp, fill=255)
            ys2 = [p[1] for p in hp]
            xs2 = [p[0] for p in hp]
            spans.append((min(ys2), max(ys2), min(xs2), max(xs2)))
    hull = hull_img.load()

    body = [[ink[y][x] or hull[x, y] > 127 for x in range(W)] for y in range(H)]
    body = fill_holes(erode(dilate(body, W, H, 1), W, H, 1), W, H)

    allowed = [[(ink[y][x] or warm[y][x] or hull[x, y] > 127) and lim[x, y] > 127 for x in range(W)] for y in range(H)]
    grow = [row[:] for row in body]
    q = deque([(x, y) for y in range(H) for x in range(W) if body[y][x]])
    while q:
        ax, ay = q.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            bx, by = ax + dx, ay + dy
            if 0 <= bx < W and 0 <= by < H and allowed[by][bx] and not grow[by][bx]:
                grow[by][bx] = True; q.append((bx, by))

    brows = [[browc[y][x] and not grow[y][x] for x in range(W)] for y in range(H)]
    merged = [[grow[y][x] and not brows[y][x] for x in range(W)] for y in range(H)]

    for y in range(H):
        for x in range(W):
            if not merged[y][x]:
                continue
            for (top, bot, xmin, xmax) in spans:
                if xmin - 8 <= x <= xmax + 8 and (y < top - UPPER_MARGIN_PX or y > bot + LOWER_MARGIN_PX):
                    merged[y][x] = False
                    break
    for y in range(H):
        for x in range(W):
            if merged[y][x] or not warm[y][x] or lim[x, y] < 128:
                continue
            for (top, bot, xmin, xmax) in spans:
                if xmin - 8 <= x <= xmax + 8 and top - UPPER_MARGIN_PX <= y <= top:
                    merged[y][x] = True
                    break

    limbool = [[lim[x, y] > 127 for x in range(W)] for y in range(H)]
    final = dilate(merged, W, H, GROW_PX, limbool)
    m = Image.new("L", (W, H), 0)
    mp = m.load()
    n = 0
    for y in range(H):
        for x in range(W):
            if final[y][x]:
                mp[x, y] = 255
                n += 1
    m = m.filter(ImageFilter.GaussianBlur(FEATHER_PX))
    stats = {"maskPx": n, "maskPctOfFace": round(100 * n / (rw * rh), 1),
             "browsPx": sum(1 for y in range(H) for x in range(W) if brows[y][x]),
             "bodyPx": sum(1 for y in range(H) for x in range(W) if body[y][x])}
    return m, (x0, y0, W, H), stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", default=str(DEFAULT_ZIP))
    ap.add_argument("--geometry", default=str(DEFAULT_MODELS / "albion-eye-geometry.json"))
    ap.add_argument("--out", default=str(DEFAULT_MODELS / "albion-eye-masks"))
    ap.add_argument("--sheets-dir", default=str(DEFAULT_SHEETS))
    ap.add_argument("--faces", nargs="*", default=ERASABLE)
    args = ap.parse_args()

    zpath = Path(args.zip)
    geom = json.loads(Path(args.geometry).read_text(encoding="utf-8"))
    out_dir = Path(args.out); out_dir.mkdir(parents=True, exist_ok=True)
    sheets = Path(args.sheets_dir); sheets.mkdir(parents=True, exist_ok=True)
    face_by_name = {f["name"]: f for f in geom["faces"]}
    eye_world = {e["slot"]: e["world"] for e in geom["eyes"] if e.get("slot")}

    with zipfile.ZipFile(zpath) as zf:
        atlas = parse_atlas(zf.read("albion.atlas").decode("utf-8"))
        pages = {}
        manifest = {"generated": "2026-10-26", "zip": zpath.name, "faces": {}}
        for name in args.faces:
            face, a = face_by_name.get(name), atlas.get(name)
            if not face or not a:
                print(f"跳过脸 {name}: 缺几何或 atlas 条目")
                continue
            if a["page"] not in pages:
                pages[a["page"]] = Image.open(io.BytesIO(zf.read(a["page"]))).convert("RGBA")
            page_img = pages[a["page"]]
            mask, rect, stats = build_mask(page_img, name, face, a, eye_world)
            fname = f"mask-{name}.png"
            mask.save(out_dir / fname)
            manifest["faces"][name] = {"page": a["page"], "rect": list(rect), "file": fname, **stats}
            mx, my, mw, mh = rect
            orig = page_img.crop((mx, my, mx + mw, my + mh)).copy()
            erased = orig.copy()
            op, ep, mpx = orig.load(), erased.load(), mask.load()
            for y in range(mh):
                for x in range(mw):
                    r, g, b, alpha = ep[x, y]
                    mm = mpx[x, y]
                    if mm and alpha:
                        ep[x, y] = (r, g, b, round(alpha * (255 - mm) / 255))
            sheet = Image.new("RGBA", (mw * 2 + 6, mh), (30, 30, 34, 255))
            sheet.alpha_composite(orig, (0, 0))
            sheet.alpha_composite(erased, (mw + 6, 0))
            sheet.resize((sheet.width * 5, sheet.height * 5), Image.LANCZOS).save(sheets / f"face-{name}.png")
            print(f"脸 {name:>2}: mask {stats['maskPx']:5d}px ({stats['maskPctOfFace']:4.1f}% of face), "
                  f"brows {stats['browsPx']:4d}px, rect {rect}")
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nmanifest → {out_dir / 'manifest.json'}")
    print(f"对照图   → {sheets}")


if __name__ == "__main__":
    main()
