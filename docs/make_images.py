#!/usr/bin/env python3
"""Generate the README illustrations (all pixels drawn here, no external assets).

Outputs into ./ (docs/):
  hero.png          — the finished 1440x900 render, framed
  comparison.png    — Figma reference vs 3 build rounds, labeled with real metrics
  pipeline.png      — the Design Compiler pipeline with its hard gates
  gates.png         — A–G gate board for the sample artboard

Run:  python3 docs/make_images.py
"""
import pathlib
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = pathlib.Path(__file__).resolve().parent
BG = (10, 11, 13)
PANEL = (21, 24, 28)
BORDER = (42, 47, 54)
TEXT = (232, 234, 237)
MUTED = (154, 163, 173)
GREEN = (56, 224, 123)
RED = (255, 92, 92)
AMBER = (255, 194, 75)
BLUE = (108, 170, 255)

EN = "/System/Library/Fonts/Supplemental/Arial.ttf"
EN_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
CJK = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"


def font(size, cjk=False, bold=False):
    return ImageFont.truetype(CJK if cjk else (EN_BOLD if bold else EN), size)


def rounded(d, box, r, fill=None, outline=None, width=1):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


def text(d, xy, s, size=24, color=TEXT, cjk=False, bold=False, anchor="la"):
    d.text(xy, s, font=font(size, cjk, bold), fill=color, anchor=anchor)


def arrow(d, x1, y, x2, color=MUTED, w=2):
    d.line([(x1, y), (x2 - 8, y)], fill=color, width=w)
    d.polygon([(x2, y), (x2 - 12, y - 6), (x2 - 12, y + 6)], fill=color)


def frame(src, pad=32):
    im = Image.open(src).convert("RGB")
    return im


def hero(src_shot, out):
    shot = frame(src_shot)
    w, h = shot.size
    pad, cap = 36, 78
    canvas = Image.new("RGB", (w + pad * 2, h + pad * 2 + cap), BG)
    d = ImageDraw.Draw(canvas)
    canvas.paste(shot, (pad, pad))
    d.rectangle([pad - 1, pad - 1, pad + w, pad + h], outline=BORDER)
    text(d, (pad, h + pad + 22), "Layout - 1", 26, TEXT, bold=True)
    text(d, (pad, h + pad + 52),
         "1440 x 900  ·  real DOM text  ·  raw mismatch 1.875%  ·  SSIM 0.9727  ·  MAE 1.66",
         20, MUTED)
    tag = "REBUILT IN HTML"
    tw = d.textlength(tag, font=font(16, bold=True))
    rounded(d, (w + pad - tw - 34, h + pad + 28, w + pad, h + pad + 62), 16,
            fill=(24, 42, 32), outline=(40, 78, 54))
    d.text((w + pad - tw - 17, h + pad + 36), tag, font=font(16, bold=True), fill=GREEN)
    canvas.save(out)
    return canvas.size


def comparison(panels, out):
    """panels: [(path, title, subtitle, color)] rendered in one row."""
    tile_w, gap, pad = 600, 22, 36
    tile_h = int(tile_w * 900 / 1440)
    head = 74
    W = pad * 2 + tile_w * len(panels) + gap * (len(panels) - 1)
    H = pad * 2 + head + tile_h
    canvas = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(canvas)
    for i, (path, title, sub, color) in enumerate(panels):
        x = pad + i * (tile_w + gap)
        im = Image.open(path).convert("RGB").resize((tile_w, tile_h), Image.LANCZOS)
        canvas.paste(im, (x, pad + head))
        d.rectangle([x - 1, pad + head - 1, x + tile_w, pad + head + tile_h],
                    outline=color, width=2)
        text(d, (x, pad + 8), title, 22, color, bold=True)
        text(d, (x, pad + 40), sub, 18, MUTED)
    canvas.save(out)
    return canvas.size


def pipeline(out):
    stages = [
        ("extract", "live Figma → manifest + native PNG", BLUE),
        ("normalize", "compile_design → IR + relations", BLUE),
        ("plan", "layout-plan.json + planHash", BLUE),
        ("P0 gate", "data_gate: contract before build", AMBER),
        ("build", "build_fixed_html: plan only", GREEN),
        ("verify", "capture + pixel_diff + A–G", GREEN),
        ("repair", "one deterministic pass", AMBER),
        ("re-verify", "same base, no regression", GREEN),
    ]
    bw, bh, gap, pad = 300, 108, 44, 40
    W = pad * 2 + bw * len(stages) + gap * (len(stages) - 1)
    H = 286
    canvas = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(canvas)
    text(d, (pad, 34), "Design Compiler pipeline", 34, TEXT, bold=True)
    text(d, (pad, 78), "一条命令串起：抽取 → 归一 → 规划 → 数据门 → 构建 → 验收 → 修复 → 复验",
         20, MUTED, cjk=True)
    y = 150
    for i, (name, sub, color) in enumerate(stages):
        x = pad + i * (bw + gap)
        rounded(d, (x, y, x + bw, y + bh), 16, fill=PANEL, outline=color, width=2)
        rounded(d, (x + 18, y + 16, x + 18 + 8, y + 92), 4, fill=color)
        text(d, (x + 40, y + 22), name, 26, TEXT, bold=True)
        # wrap subtitle to 2 lines by width
        words, line, lines = sub.split(" "), "", []
        for word in words:
            probe = (line + " " + word).strip()
            if d.textlength(probe, font=font(17)) > bw - 60:
                lines.append(line)
                line = word
            else:
                line = probe
        lines.append(line)
        for k, ln in enumerate(lines[:2]):
            text(d, (x + 40, y + 58 + k * 22), ln, 17, MUTED)
        if i < len(stages) - 1:
            arrow(d, x + bw + 6, y + bh // 2, x + bw + gap - 6, MUTED)
    canvas.save(out)
    return canvas.size


def gates(rows, out):
    W, pad, rh, head = 1320, 40, 66, 132
    H = pad * 2 + head + rh * len(rows) + 58
    canvas = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(canvas)
    text(d, (pad, pad), "Gate board — Layout - 1", 34, TEXT, bold=True)
    text(d, (pad, pad + 42),
         "pass / fail / unknown are three distinct states. unknown is never a pass; "
         "strict mode exits non-zero on either.", 19, MUTED)
    y0 = pad + head
    colw = max(int(d.textlength(g, font=font(24, bold=True))) for g, _, _, _ in rows) + 36
    for i, (gate, status, note, color) in enumerate(rows):
        y = y0 + i * rh
        if i % 2 == 0:
            rounded(d, (pad - 12, y, W - pad + 12, y + rh - 8), 10, fill=(16, 18, 21))
        text(d, (pad, y + 16), gate, 24, TEXT, bold=True)
        chip_w = 108
        cx = pad + colw
        rounded(d, (cx, y + 12, cx + chip_w, y + 46), 16,
                fill=tuple(int(c * 0.16) for c in color), outline=color)
        label = status.upper()
        lw = d.textlength(label, font=font(15, bold=True))
        d.text((cx + (chip_w - lw) / 2, y + 20), label,
               font=font(15, bold=True), fill=color)
        text(d, (cx + chip_w + 28, y + 16), note, 19, MUTED)
    text(d, (pad, H - 40), "Sample: Layout - 1 (1:10), 1440 x 900, DPR 1, threshold 20/255",
         18, MUTED)
    canvas.save(out)
    return canvas.size


def main():
    root = pathlib.Path(sys.argv[1]).resolve()
    hero(root / "hero.png", HERE / "hero.png")
    comparison([
        (root / "ref.png", "Figma reference", "the design, exported at 1x", MUTED),
        (root / "r2.png", "Early build",
         "blend mode dropped, blurred layers misplaced — 61.0%", RED),
        (root / "r3.png", "Final",
         "blend mode + effect bounds fixed — 1.875%, SSIM 0.973", GREEN),
    ], HERE / "comparison.png")
    pipeline(HERE / "pipeline.png")
    gates([
        ("P0  data gate", "pass", "rootFills, per-run font sizes, render boxes, asset placement", GREEN),
        ("A  baseline", "pass", "reference locked; 1440x900 / DPR 1 / 20-255 frozen", GREEN),
        ("B  content", "pass", "24/24 layers rendered, nothing hidden over-paints", GREEN),
        ("C  relations", "fail", "22 edges: 16 pass, 1 fail, 5 unknown (rotated box family)", RED),
        ("D  text", "fail", "fontsVerified 3, fontsMissing 0; 2 rotated boxes overflow by contract", RED),
        ("E  connectors", "pass", "connectors.manifest proves connectorCount = 0", GREEN),
        ("F  overview", "unknown", "no overview page for a single-artboard build", AMBER),
        ("G  pixels", "pass", "1.875% raw, SSIM 0.9727, MAE 1.66", GREEN),
    ], HERE / "gates.png")
    print("ok")


if __name__ == "__main__":
    main()
