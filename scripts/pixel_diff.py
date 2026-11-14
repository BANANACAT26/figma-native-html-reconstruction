#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pixel_diff.py — 同尺寸逐像素 + 感知 diff 管线。

约定:
  * 输入两张 PNG 必须物理尺寸完全一致；不一致直接报错，禁止缩放对齐。
  * 原始 diff: 逐通道 abs diff、任意通道超阈值即判 mismatch。
  * 阈值显式给出: 主阈值 T=20/255（任一通道），并附带 T=10 / T=40 分位。
  * 感知指标: SSIM（亮度域，11x11 σ=1.5 高斯窗，C1=(0.01*255)^2, C2=(0.03*255)^2）。
  * 分解指标: 用 DOM text-mask 把"文本抗锯齿差异"分离 —— 同时给出全图 raw 指标
    与"文本区外"指标；text mask 不改变原始 diff。
  * 输出: diff.png(放大原始差), heatmap.png(幅值热力), overlay.png(reference 暗底 +
    红色 mismatch 叠印), text-mask.png(若有), metrics.json(全部数字与计算依据),
    连通热点 top-N（bbox/质心/面积/平均幅值）。

用法:
  python3 pixel_diff.py --browser browser.png \
      --ref reference.png --outdir out_diff \
      [--text-mask text-mask.png]
"""
import argparse
import json
import os
import sys

import numpy as np
from PIL import Image
from scipy import ndimage

T_PRIMARY = 20          # 任一通道 abs diff > 20/255 即 mismatch（主阈值）
T_LEVELS = [10, 20, 40] # 同时报告三档
SSIM_WIN_SIGMA = 1.5
SSIM_C1 = (0.01 * 255) ** 2
SSIM_C2 = (0.03 * 255) ** 2
MIN_COMP_AREA = 60      # 小于此像素的连通块忽略为噪点
TOP_N = 15


def load_rgb(path, background=(0, 0, 0)):
    im = Image.open(path)
    arr = np.asarray(im.convert("RGBA")).astype(np.float64)
    a = arr[..., 3:4] / 255.0
    rgb = arr[..., :3] * a + np.array([background], dtype=np.float64) * (1 - a)
    return rgb  # float64 HxWx3, 0..255


def ssim_map(ref, cand):
    """返回逐像素 SSIM map（亮度域，高斯窗）。"""
    g = 0.299 * ref[..., 0] + 0.587 * ref[..., 1] + 0.114 * ref[..., 2]
    c = 0.299 * cand[..., 0] + 0.587 * cand[..., 1] + 0.114 * cand[..., 2]
    trunc = 11 / 2 / SSIM_WIN_SIGMA  # ~3.67 sigma ≈ 11px 窗
    mu_g = ndimage.gaussian_filter(g, SSIM_WIN_SIGMA, truncate=trunc)
    mu_c = ndimage.gaussian_filter(c, SSIM_WIN_SIGMA, truncate=trunc)
    mu_g2, mu_c2, mu_gc = mu_g * mu_g, mu_c * mu_c, mu_g * mu_c
    sg = ndimage.gaussian_filter(g * g, SSIM_WIN_SIGMA, truncate=trunc) - mu_g2
    sc = ndimage.gaussian_filter(c * c, SSIM_WIN_SIGMA, truncate=trunc) - mu_c2
    sgc = ndimage.gaussian_filter(g * c, SSIM_WIN_SIGMA, truncate=trunc) - mu_gc
    ssim = ((2 * mu_gc + SSIM_C1) * (2 * sgc + SSIM_C2)) / \
           ((mu_g2 + mu_c2 + SSIM_C1) * (sg + sc + SSIM_C2))
    return ssim, g, c


def jet_lut(mag):
    """mag: HxW float 0..1 -> RGB uint8（蓝->青->黄->红 热力）。无 matplotlib 依赖。"""
    t = np.clip(mag, 0, 1)
    r = np.clip(1.5 - np.abs(4 * t - 3), 0, 1)
    g = np.clip(1.5 - np.abs(4 * t - 2), 0, 1)
    b = np.clip(1.5 - np.abs(4 * t - 1), 0, 1)
    return (np.stack([r, g, b], -1) * 255).astype(np.uint8)


def region_stats(mask_region, diff_abs, ssim):
    """在指定布尔区域上统计 mismatch 比例 / MAE / SSIM。

    mismatch 口径与全图一致: 逐像素 max(通道|Δ|) > T_PRIMARY。
    """
    n = int(mask_region.sum())
    if n == 0:
        return {"pixels": 0}
    d = diff_abs[mask_region]              # N x 3
    dmax = d.max(axis=1)                   # 每像素最大通道差
    return {
        "pixels": n,
        "mismatch_px": int((dmax > T_PRIMARY).sum()),
        "mismatch_fraction": round(float((dmax > T_PRIMARY).mean()), 6),
        "mae_per_channel_mean": round(float(d.mean()), 3),
        "mae_r": round(float(d[..., 0].mean()), 3),
        "mae_g": round(float(d[..., 1].mean()), 3),
        "mae_b": round(float(d[..., 2].mean()), 3),
        "ssim_mean": round(float(ssim[mask_region].mean()), 6),
    }


def main():
    global T_PRIMARY
    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", required=True)
    ap.add_argument("--reference", "--ref", dest="ref", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--text-mask", default=None)
    ap.add_argument("--threshold", type=float, default=T_PRIMARY)
    ap.add_argument("--background", default="000000", help="RGBA 透明区域合成底色，RRGGBB")
    args = ap.parse_args()
    T_PRIMARY = args.threshold
    bg = tuple(int(args.background[i:i+2], 16) for i in (0, 2, 4))
    if len(bg) != 3:
        raise SystemExit("[diff] --background 必须为 6 位 RRGGBB")

    os.makedirs(args.outdir, exist_ok=True)
    ref = load_rgb(args.ref, bg)
    cand = load_rgb(args.browser, bg)
    if ref.shape != cand.shape:
        raise SystemExit(
            f"[diff] 尺寸不一致，禁止缩放对齐: ref={ref.shape[:2]} browser={cand.shape[:2]}")
    H, W, _ = ref.shape
    print(f"[diff] 同尺寸校验通过: {W}x{H}")

    diff = cand - ref
    diff_abs = np.abs(diff)
    mag = diff_abs.max(axis=2)                 # 每像素最大通道差
    mismatch = mag > T_PRIMARY
    print(f"[diff] raw mismatch(>{T_PRIMARY}) = {mismatch.mean()*100:.3f}%")

    # ---- 文本 mask ----
    if args.text_mask and os.path.exists(args.text_mask):
        tm = np.asarray(Image.open(args.text_mask).convert("L")) > 127
        if tm.shape != (H, W):
            raise SystemExit(f"[diff] text-mask 尺寸 {tm.shape} != 图像 {(H,W)}")
    else:
        tm = np.zeros((H, W), bool)
    outside = ~tm

    # ---- SSIM ----
    ssim, _, _ = ssim_map(ref, cand)

    # ---- 指标: raw 全图 / 文本区外 ----
    metrics = {
        "image_size_px": [W, H],
        "browser": os.path.abspath(args.browser),
        "reference": os.path.abspath(args.ref),
        "thresholds": {
            "primary_mismatch": T_PRIMARY,
            "levels": T_LEVELS,
            "definition": "任一 RGB 通道 |Δ| > T 判为 mismatch 像素",
        },
        "ssim_definition": {
            "domain": "luminance 0.299R+0.587G+0.114B",
            "window": f"gaussian {SSIM_WIN_SIGMA}sigma (~11px)",
            "C1": SSIM_C1, "C2": SSIM_C2,
        },
        "raw": region_stats(np.ones((H, W), bool), diff_abs, ssim),
        "outside_text": region_stats(outside, diff_abs, ssim),
        "text_region": region_stats(tm, diff_abs, ssim),
        "text_mask_coverage": round(float(tm.mean()), 5),
        "mismatch_at_levels": {
            f"T{t}": {"fraction": round(float((diff_abs.max(2) > t).mean()), 6)}
            for t in T_LEVELS
        },
        "global": {
            "max_abs_diff": round(float(diff_abs.max()), 1),
            "mean_signed_bias_R": round(float(diff[..., 0].mean()), 3),
            "mean_signed_bias_G": round(float(diff[..., 1].mean()), 3),
            "mean_signed_bias_B": round(float(diff[..., 2].mean()), 3),
        },
    }

    # ---- 连通热点 ----
    lab, nlab = ndimage.label(mismatch, structure=np.ones((3, 3), int))
    objs = ndimage.find_objects(lab)
    comps = []
    for i, sl in enumerate(objs, start=1):
        area = int((lab[sl] == i).sum())
        if area < MIN_COMP_AREA:
            continue
        ys, xs = np.where(lab == i)
        y0, y1 = sl[0].start, sl[0].stop
        x0, x1 = sl[1].start, sl[1].stop
        comps.append({
            "bbox": [int(x0), int(y0), int(x1), int(y1)],
            "area_px": area,
            "centroid": [round(float(xs.mean()), 1), round(float(ys.mean()), 1)],
            "mean_mag": round(float(mag[ys, xs].mean()), 2),
        })
    comps.sort(key=lambda c: -c["area_px"])
    metrics["hotspots"] = comps[:TOP_N]
    metrics["hotspot_count_above_min_area"] = len(comps)

    # ---- 可视化输出 ----
    # 1) diff.png: 原始 |Δ| 放大 4 倍
    diff_img = np.clip(diff_abs * 4, 0, 255).astype(np.uint8)
    Image.fromarray(diff_img).save(os.path.join(args.outdir, "diff.png"))

    # 2) heatmap.png: 幅值热力（按最大 observed 归一）
    hm = jet_lut(mag / max(mag.max(), 1))
    Image.fromarray(hm).save(os.path.join(args.outdir, "heatmap.png"))

    # 3) overlay.png: reference 压暗 45% + mismatch 红叠印
    ov = np.clip(ref * 0.45, 0, 255).astype(np.uint8).copy()
    ov[mismatch] = np.array([255, 70, 70], np.uint8)
    Image.fromarray(ov).save(os.path.join(args.outdir, "overlay.png"))

    with open(os.path.join(args.outdir, "metrics.json"), "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)

    print(f"[diff] raw ssim={metrics['raw']['ssim_mean']} "
          f"outside-text ssim={metrics['outside_text']['ssim_mean']} "
          f"hotspots={len(comps)}")
    print(f"[diff] 输出目录: {args.outdir}")


if __name__ == "__main__":
    main()
