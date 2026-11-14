#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic Design Compiler core (iteration-3).

Pipeline:  extract -> normalize -> analyze -> plan

This module turns an export directory produced by export_artboard.py
(manifest.json + text-manifest.json + optional connectors.manifest.json) into a
deterministic, machine-readable layout intent that the builder / auditor
consume instead of hand-written specs:

  design-ir.json          every node (incl. hidden/transparent/skipped) with a
                          normalized disposition and unified box / matrix /
                          clip / zorder / autoLayout / text / asset / connector
                          record.
  component-relations.json  a graph of parentChildInset / siblingGap /
                          siblingOrder / alignmentAxis / baseline /
                          iconHostCenter / textLineInset / connectorEdge
                          edges covering every node, never hand-picked.
  layout-plan.json        flat render plan (layers + textPlans + connectorPlans
                          + relations) with a canonical semantic hash.
  layout-plan.json.sha256 sidecar mirroring run.json.sha256.

Hard rules (from compiler-contract.json + the four iteration-3 diagnoses):
  * Every subtree node has an IR row with a disposition -- coverage is the
    content ledger, never "did a data-n string land in HTML".
  * Missing fields are ``unknown`` -- never fabricated as zero or guessed.
  * A rotated node's unrotated designBox is NOT comparable to its rotated
    renderAABB/pngPlacement; cross-family numeric deltas are unknown.
  * Parent-child insets are computed in the immediate parent's LOCAL frame
    (child.local is already pre-rotation), which cancels -180 / flipped
    ancestors deterministically.
  * Connector source/target/edge are derived from the manifest only. Anything
    ambiguous or absent -> status=unverifiable, never a visual guess.
  * No wall-clock, no dict-order dependence, floats rounded to 3dp before
    hashing; repeated runs on the same IR are byte-identical.
  * No project node ids / colors / pixel compensations are baked in.

The module is a pure library plus a thin CLI:

  python3 scripts/compile_design.py --export-dir <dir> [--out <dir>]

Importable surface (used by tests):
  extract_from_data(manifest, text_manifest, connectors_manifest) -> RawBundle
  normalize(raw) -> IrDict
  analyze(ir) -> Relations
  plan(ir, relations) -> LayoutPlan
  compile_bundle(raw_bundle) -> (ir, relations, layout_plan, plan_hash)
  write_outputs(out_dir, ir, relations, layout_plan, plan_hash) -> dict
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from typing import Any, Dict, List, Optional, Tuple

import visibility_rules as vr  # single source of truth for disposition

RULE_VERSION = "design-compiler/1.0.0"
RELATIONS_VERSION = "design-compiler/1.0.0"

EPS = 1e-6
# Default geometry tolerance (px) for centering / gap clustering. Not a
# project constant -- an operator may override; the default is generic.
DEFAULT_TOL = 2.0
OPACITY_SKIP = 0.01

# Dispositions that produce an own DOM layer in the layout plan.
_RENDERS_LAYER = (
    "rendered-asset",
    "rendered-text",
    "rendered-container",
)


# ---------------------------------------------------------------------------
# small deterministic helpers
# ---------------------------------------------------------------------------
def _r3(v: Any) -> Any:
    """Round a float to 3dp; pass through non-floats / None."""
    if isinstance(v, bool):
        return v
    if isinstance(v, float):
        return round(v, 3)
    return v


def _canonical(obj: Any) -> Any:
    """Recursively round floats to 3dp and produce a hash-ready structure.

    Dicts are NOT sorted here -- the canonical hash uses json.dumps(
    sort_keys=True). This pass only normalizes float precision.
    """
    if isinstance(obj, float):
        return round(obj, 3)
    if isinstance(obj, dict):
        return {k: _canonical(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_canonical(v) for v in obj]
    return obj


def _sha256_json(obj: Any) -> str:
    payload = json.dumps(
        _canonical(obj), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _edge_id(kind: str, source: str, target: str) -> str:
    # MUST match audit_relations._edge_id: pipe-joined triple, first 12 hex.
    canon = "|".join([kind, source, target])
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:12]


def _box(d: Any) -> Optional[Dict[str, float]]:
    """Normalize a box-like dict to {x,y,w,h}; accept width/height aliases."""
    if not isinstance(d, dict):
        return None
    x = d.get("x", d.get("left"))
    y = d.get("y", d.get("top"))
    w = d.get("width", d.get("w"))
    h = d.get("height", d.get("h"))
    if x is None or y is None or w is None or h is None:
        return None
    try:
        return {"x": float(x), "y": float(y), "w": float(w), "h": float(h)}
    except (TypeError, ValueError):
        return None


def _mat(m: Any) -> List[float]:
    """Normalize an affine matrix to [a,b,c,d,tx,ty]; identity when absent."""
    if isinstance(m, (list, tuple)) and len(m) == 6:
        try:
            return [float(v) for v in m]
        except (TypeError, ValueError):
            pass
    return [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]


def _is_rotated(mat: List[float], eps: float = EPS) -> bool:
    a, b, c, d = mat[0], mat[1], mat[2], mat[3]
    return abs(a - 1.0) > eps or abs(d - 1.0) > eps \
        or abs(b) > eps or abs(c) > eps


def _is_180_flip(mat: List[float], eps: float = EPS) -> bool:
    a, b, c, d = mat[0], mat[1], mat[2], mat[3]
    return abs(a + 1.0) < eps and abs(d + 1.0) < eps \
        and abs(b) < eps and abs(c) < eps


def _reproject_render_aabb(local_box: Optional[Dict[str, float]],
                          mat: List[float]) -> Optional[Dict[str, float]]:
    """True axis-aligned render box = AABB of the LOCAL box corners transformed
    by the FULL affine [a,b,c,d,tx,ty] (artboard space).

    When a node is rotated/flipped, the export's ``renderAABB`` may still equal
    its unrotated ``designBox`` (the serializer copied the layout box). The only
    deterministic render box is the corner reprojection; never trust the equal
    box as a rotated one.
    """
    if not local_box or not mat or len(mat) != 6:
        return None
    a, b, c, d, tx, ty = mat
    x, y, w, h = (local_box["x"], local_box["y"],
                  local_box["w"], local_box["h"])
    corners = [(x, y), (x + w, y), (x, y + h), (x + w, y + h)]
    xs = [a * px + c * py + tx for px, py in corners]
    ys = [b * px + d * py + ty for px, py in corners]
    return {"x": round(min(xs), 4), "y": round(min(ys), 4),
            "w": round(max(xs) - min(xs), 4),
            "h": round(max(ys) - min(ys), 4)}


def _center(box: Dict[str, float]) -> Tuple[float, float]:
    return (box["x"] + box["w"] / 2.0, box["y"] + box["h"] / 2.0)


def _intersection_area(a: Dict[str, float], b: Dict[str, float]) -> float:
    x0 = max(a["x"], b["x"])
    y0 = max(a["y"], b["y"])
    x1 = min(a["x"] + a["w"], b["x"] + b["w"])
    y1 = min(a["y"] + a["h"], b["y"] + b["h"])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return (x1 - x0) * (y1 - y0)


def _clip_intersection(a, b):
    a = a or {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}
    b = b or a
    x0, y0 = max(a["x"], b["x"]), max(a["y"], b["y"])
    x1 = min(a["x"] + a["w"], b["x"] + b["w"])
    y1 = min(a["y"] + a["h"], b["y"] + b["h"])
    return {"x": round(x0, 3), "y": round(y0, 3),
            "w": round(max(0.0, x1 - x0), 3),
            "h": round(max(0.0, y1 - y0), 3)}


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------
def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def extract(export_dir: str) -> Dict[str, Any]:
    """Read manifest.json / text-manifest.json / connectors.manifest.json."""
    manifest = _load_json(os.path.join(export_dir, "manifest.json"))
    tm_path = os.path.join(export_dir, "text-manifest.json")
    text_manifest = _load_json(tm_path) if os.path.exists(tm_path) \
        else {"artboardId": manifest.get("artboardId"), "texts": []}
    cm_path = os.path.join(export_dir, "connectors.manifest.json")
    if not os.path.exists(cm_path):
        # some exports nest the connector manifest under connectors/
        alt = os.path.join(export_dir, "connectors",
                           "connectors.manifest.json")
        if os.path.exists(alt):
            cm_path = alt
    connectors_manifest = _load_json(cm_path) if os.path.exists(cm_path) \
        else {"exported": [], "refused": []}
    raw = extract_from_data(manifest, text_manifest, connectors_manifest)
    calibration_path = os.path.join(export_dir, "text-calibration.json")
    if os.path.exists(calibration_path):
        raw["text_calibration"] = _load_json(calibration_path)
    return raw


def extract_from_data(
    manifest: Dict[str, Any],
    text_manifest: Dict[str, Any],
    connectors_manifest: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Bundle already-decoded manifest dicts (test-friendly)."""
    return {
        "manifest": manifest or {},
        "text_manifest": text_manifest or {"texts": []},
        "connectors_manifest": connectors_manifest or {"exported": []},
    }


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------
def _fills_kind(fills: Any) -> str:
    if not isinstance(fills, list) or not fills:
        return "unknown"
    kinds = set()
    for f in fills:
        if not isinstance(f, dict):
            continue
        if f.get("visible") is False:
            continue
        t = f.get("type", "")
        if t == "SOLID":
            kinds.add("solid")
        elif t == "IMAGE":
            kinds.add("image")
        elif t:
            kinds.add("gradient")
    if not kinds:
        return "unknown"
    if len(kinds) == 1:
        return next(iter(kinds))
    return "mixed"


def _auto_layout(src: Dict[str, Any]) -> Dict[str, Any]:
    """Read auto-layout fields when the serializer exposed them.

    Absent fields are ``unknown`` (derivation="unknown"), never zero.
    """
    mode = src.get("layoutMode") or src.get("layout")
    if not mode:
        return {"present": False, "derivation": "unknown",
                "layoutMode": None, "primaryAxis": None, "counterAxis": None,
                "paddingL": None, "paddingR": None, "paddingT": None,
                "paddingB": None, "itemSpacing": None, "layoutWrap": None}
    return {
        "present": True,
        "derivation": "autoLayout",
        "layoutMode": mode,
        "primaryAxis": src.get("primaryAxisAlignItems"),
        "counterAxis": src.get("counterAxisAlignItems"),
        "paddingL": src.get("paddingLeft"),
        "paddingR": src.get("paddingRight"),
        "paddingT": src.get("paddingTop"),
        "paddingB": src.get("paddingBottom"),
        "itemSpacing": src.get("itemSpacing"),
        "layoutWrap": src.get("layoutWrap"),
    }


def _text_plan(t: Dict[str, Any]) -> Dict[str, Any]:
    """Derive the deterministic CSS intent for a text node (text.json S2)."""
    local = _box(t.get("local")) or {}
    ar = t.get("textAutoResize") or "NONE"
    chars = t.get("characters") or ""
    if ar == "WIDTH_AND_HEIGHT":
        white_space = "nowrap"
        outer_width = "auto"
        mode = "content-width"
    elif local.get("w") is not None and local["w"] > 0:
        white_space = "pre-wrap"   # preserves embedded \n AND wraps at box width
        outer_width = _r3(local["w"])
        mode = "fixed-box"
    else:
        white_space = "pre"
        outer_width = "auto"
        mode = "unwrapped-auto"

    lh = t.get("lineHeight") or {}
    resolved_px = None
    lh_source = "unknown"
    if isinstance(lh, dict):
        unit = lh.get("unit")
        value = lh.get("value")
        if unit == "PIXELS" and value is not None:
            resolved_px = _r3(float(value))
            lh_source = "authoritative"
        elif unit == "PERCENT" and value is not None:
            fs = t.get("fontSize")
            resolved_px = _r3(float(fs) * float(value) / 100.0) if fs else None
            lh_source = "percent"
        elif unit == "AUTO":
            # AUTO is a legitimate live value. resolvedPx is only trusted when the
            # export actually measured it. Never fabricate from box height / line
            # count: unknown stays None and the builder may emit `normal`.
            rp = lh.get("resolvedPx")
            if rp is not None:
                resolved_px = _r3(float(rp))
                lh_source = "live-auto"
            # else: resolved_px stays None, lh_source stays "unknown"
    ls = t.get("letterSpacing") or {}
    ls_px = None
    if isinstance(ls, dict):
        unit = ls.get("unit", "PIXELS")
        v = ls.get("value", 0)
        try:
            if unit == "PERCENT":
                fs = t.get("fontSize")
                ls_px = _r3(float(fs) * float(v) / 100.0) if isinstance(fs, (int, float)) else None
            else:
                ls_px = _r3(float(v))
        except (TypeError, ValueError):
            ls_px = None

    # Per-run style: carry segments verbatim. fontSize may be "mixed" here; the
    # builder hard-fails unless every run carries a numeric fontSize. We never
    # invent a default size at compile time.
    runs = []
    for s in (t.get("segments") or []):
        if not isinstance(s, dict):
            continue
        runs.append({
            "characters": s.get("characters", ""),
            "fontSize": s.get("fontSize"),
            "fontName": s.get("fontName"),
            "fills": s.get("fills"),
            "lineHeight": s.get("lineHeight"),
            "letterSpacing": s.get("letterSpacing"),
            "start": s.get("start"),
            "end": s.get("end"),
        })

    return {
        "nodeId": t.get("nodeId"),
        "characters": chars,
        "textAutoResize": ar,  # verbatim: NONE / WIDTH_HEIGHT / WIDTH_AND_HEIGHT
        "mode": mode,
        "whiteSpace": white_space,
        "outerWidth": outer_width,
        "lineHeightPx": resolved_px,
        "lineHeightSource": lh_source,
        "letterSpacingPx": ls_px,
        "textAlignHorizontal": t.get("textAlignHorizontal") or "LEFT",
        "textAlignVertical": t.get("textAlignVertical") or "TOP",
        "fontSize": t.get("fontSize"),
        "fontName": t.get("fontName"),
        "fills": t.get("fills"),
        "runs": runs,
    }


def _unify_dispositions(nodes: Dict[str, Dict[str, Any]],
                        stage_box: Optional[Dict[str, float]]) -> None:
    """DC-1..DC-6: route every node through visibility_rules so disposition is
    the single 12-value taxonomy.

    The export ledger already knows whether a node renders / is baked / is
    skipped; here we combine that with the deterministic geometric signals
    visibility_rules computes (zero-size / out-of-stage / fully-clipped /
    below-opacity-threshold) so the taxonomy is decided by field arithmetic,
    never by 'did a data-n string land in HTML'.
    """
    stage_rect = None
    if stage_box and stage_box["w"] > 0:
        stage_rect = {"x": 0.0, "y": 0.0,
                      "width": stage_box["w"], "height": stage_box["h"]}

    for nid, rec in nodes.items():
        old = rec.get("disposition") or "unresolved"
        reason = rec.get("dispositionReason") or ""
        eff_op = rec["sourceFields"].get("effectiveOpacity", 1.0)
        eff_vis = rec["sourceFields"].get("effectiveVisible", True)

        # geometric signals from visibility_rules helpers
        db = rec.get("designBox")
        zero_size = bool(db and (db["w"] == 0 or db["h"] == 0))
        outside = False
        if db and stage_rect:
            outside = vr.rect_intersection(
                {"x": db["x"], "y": db["y"], "width": db["w"],
                 "height": db["h"]}, stage_rect) is None
        clip_area = None
        if db and rec.get("effectiveClipRect"):
            cr = rec["effectiveClipRect"]
            clip_area = vr.rect_area(vr.rect_intersection(
                {"x": db["x"], "y": db["y"], "width": db["w"],
                 "height": db["h"]},
                {"x": cr["x"], "y": cr["y"], "width": cr["w"], "height": cr["h"]}))
        fully_clipped = clip_area == 0
        below_opacity = eff_op < vr.OPACITY_SKIP_THRESHOLD

        # branch order mirrors visibility_rules.classify_disposition
        if not eff_vis and "ancestor" in str(reason):
            disp = "ancestor-invisible"
        elif not eff_vis or old == "skipped-invisible":
            disp = "self-invisible"
        elif below_opacity:
            disp = "transparent"
        elif zero_size:
            disp = "zero-size"
        elif outside:
            disp = "out-of-stage"
        elif fully_clipped:
            disp = "fully-clipped"
        elif old == "rendered-by-ancestor" or rec.get("bakingAncestor"):
            disp = "baked-into-ancestor-png"
        elif old == "skipped-empty-container":
            disp = "empty-wrapper"
        else:
            # DC-2: decide by ACTUAL resolvable paint, not the ledger's
            # optimistic "rendered-container" label. A transparent layout wrapper
            # with no own paint paints zero pixels; an IMAGE fill with no
            # imageRef and no exported PNG cannot be resolved.
            v = rec.get("visual") or {}
            fills = v.get("fills") or []
            def _visible_solid(f):
                return (isinstance(f, dict) and f.get("type") == "SOLID"
                        and f.get("visible", True))
            def _visible_image_ref(f):
                return (isinstance(f, dict) and f.get("type") == "IMAGE"
                        and f.get("visible", True) and f.get("imageRef"))
            own_solid = any(_visible_solid(f) for f in fills)
            own_image_ref = any(_visible_image_ref(f) for f in fills)
            has_image_fill = any(isinstance(f, dict)
                                 and f.get("type") == "IMAGE" for f in fills)
            asset_file = (rec.get("asset") or {}).get("file")
            is_text = rec.get("type") == "TEXT"

            if is_text or old == "rendered-stage":
                disp = "rendered-pixels-visible"   # tlayer / stage background
            elif asset_file:
                disp = "rendered-pixels-visible"   # exported asset PNG/IMG
            elif own_solid or own_image_ref:
                disp = "rendered-pixels-visible"
            elif has_image_fill:
                # IMAGE paint but no imageRef and no exported PNG -> cannot
                # resolve; must NOT claim rendered-pixels-visible.
                disp = "unresolved"
                rec["dispositionReason"] = (
                    "skipped-no-image-ref: IMAGE fill without imageRef and no "
                    "exported asset; strict block")
            else:
                # transparent layout wrapper with no own paint -> empty wrapper
                disp = "empty-wrapper"

        rec["disposition"] = disp
        rec["visibilityCtx"] = {
            "effectiveVisible": bool(eff_vis),
            "computedOpacity": round(float(eff_op), 4),
            "zeroSize": zero_size,
            "outsideStage": outside,
            "fullyClipped": fully_clipped,
            "belowOpacityThreshold": below_opacity,
            "paintsPixels": disp == "rendered-pixels-visible",
        }


def normalize(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Merge ledger coverage + geometry + text + connectors into one IR."""
    manifest = raw["manifest"]
    text_manifest = raw["text_manifest"]
    connectors_manifest = raw.get("connectors_manifest") or {}

    artboard_size = manifest.get("artboardSize") or {}
    stage_w = float(artboard_size.get("width") or 0.0)
    stage_h = float(artboard_size.get("height") or 0.0)
    stage_box = {"x": 0.0, "y": 0.0, "w": stage_w, "h": stage_h} \
        if stage_w and stage_h else None

    # index sources by nodeId
    assets = {a.get("nodeId"): a for a in manifest.get("assets", []) or []}
    containers = {c.get("nodeId"): c
                  for c in manifest.get("containerVisuals", []) or []}
    texts = {t.get("nodeId"): t for t in text_manifest.get("texts", []) or []}
    connector_recs = {c.get("nodeId"): c
                      for c in connectors_manifest.get("exported", []) or []}

    ledger = manifest.get("contentLedger", []) or []
    nodes: Dict[str, Dict[str, Any]] = {}

    def _geo_for(nid: str) -> Dict[str, Any]:
        """Pull geometry from whichever source carried this node."""
        src = (assets.get(nid) or containers.get(nid)
               or texts.get(nid) or connector_recs.get(nid) or {})
        # Box families, strictly by coordinate system (all artboard-space):
        #   designBox       = unrotated layout rect (ancestor matrix only)
        #   renderAABB      = export's own render truth: intrinsicRenderBounds
        #                     first, then the clamped relativeToArtboard.
        # Never derive renderAABB by corner-reprojecting parent-relative local.
        design_box = (_box(src.get("designBox"))
                      or _box(src.get("relativeToArtboard")))
        render_aabb = (_box(src.get("renderAABB"))
                       or _box(src.get("intrinsicRenderBounds"))
                       or _box(src.get("relativeToArtboard")))
        png = _box(src.get("pngPlacement"))
        clip = _box(src.get("effectiveClipRect"))
        local = _box(src.get("local"))
        mat = _mat(src.get("absoluteTransform"))
        export_size = src.get("exportSize")
        if isinstance(export_size, dict):
            export_size = {"w": float(export_size.get("width") or 0.0),
                           "h": float(export_size.get("height") or 0.0)}
        return {
            "src": src,
            "designBox": design_box,
            "renderAABB": render_aabb or design_box,
            "pngPlacement": png,
            "effectiveClipRect": clip,
            "local": local,
            "effectiveMatrix": mat,
            "exportSize": export_size,
            "rotation": src.get("rotation", 0),
        }

    coverage_ids = set()
    for row in ledger:
        nid = row.get("nodeId")
        if not nid:
            continue
        coverage_ids.add(nid)
        geo = _geo_for(nid)
        src = geo["src"]
        mat = geo["effectiveMatrix"]
        rotated = _is_rotated(mat)
        disp = row.get("disposition") or "unresolved"
        eff_op = float(row.get("effectiveOpacity", 1.0))
        eff_vis = bool(row.get("effectiveVisible", True))

        # derived visibility checks (deterministic, from geometry only)
        db = geo["designBox"]
        zero_size = bool(db and (db["w"] == 0 or db["h"] == 0))
        outside = False
        if db and stage_box:
            outside = not (db["x"] + db["w"] > 0 and db["y"] + db["h"] > 0
                           and db["x"] < stage_w and db["y"] < stage_h)
        clip_zero = False
        if db and geo["effectiveClipRect"]:
            clip_zero = _intersection_area(db, geo["effectiveClipRect"]) <= 0.0
        transparent = eff_op < OPACITY_SKIP

        ntype = row.get("type") or src.get("type") or "unknown"
        is_text = ntype == "TEXT" or nid in texts
        is_connector = nid in connector_recs

        rec: Dict[str, Any] = {
            "nodeId": nid,
            "parentId": row.get("parentId"),
            "children": [],
            "name": row.get("name") or src.get("name"),
            "type": ntype,
            "disposition": disp,
            "dispositionReason": row.get("reason"),
            "sourceFields": {
                "visible": row.get("selfVisible", True),
                "effectiveVisible": eff_vis,
                "effectiveOpacity": _r3(eff_op),
                "isMask": src.get("isMask", False),
                "placementAnchor": src.get("placementAnchor"),
                "residual": src.get("residual"),
            },
            "visibilityChain": [],  # filled after pass 2
            "effectiveVisibility": eff_vis and not transparent,
            "designBox": geo["designBox"],
            "renderAABB": geo["renderAABB"],
            "absoluteRenderBounds": geo["renderAABB"],
            "exportSize": geo["exportSize"],
            "pngPlacement": geo["pngPlacement"],
            "effectiveClipRect": geo["effectiveClipRect"],
            "effectiveMatrix": mat,
            "zIndexChain": list(row.get("zIndexChain") or []),
            "autoLayout": _auto_layout(src),
            "fillsKind": _fills_kind(src.get("fills")),
            # Full visual properties preserved verbatim for the builder --
            # dropping these is what turned real painted nodes into
            # visibility:hidden zero-visual markers. Additive passthrough only.
            "visual": {
                "fills": src.get("fills"),
                "fillKinds": src.get("fillKinds"),
                "strokes": src.get("strokes"),
                "strokeWeight": src.get("strokeWeight"),
                "effects": src.get("effects"),
                "cornerRadius": src.get("cornerRadius"),
                "opacity": src.get("opacity"),
                "effectiveOpacity": row.get("effectiveOpacity", eff_op),
                "isMask": src.get("isMask", False),
                "clipsContent": src.get("clipsContent"),
                "rotation": src.get("rotation", 0),
                # Figma layer blend mode: a node-level composite property that
                # PNG export cannot bake (exportAsync rasterizes the layer in
                # isolation). Must reach the plan so the builder can emit
                # mix-blend-mode; dropping it silently washed out COLOR_BURN
                # backgrounds in a real run.
                "blendMode": src.get("blendMode", "NORMAL"),
            },
            "boxComparability": {
                "rotated": rotated,
                "flipped180": _is_180_flip(mat),
                "note": ("rotated designBox not comparable to renderAABB"
                         if rotated else "designBox comparable"),
            },
            "derivedChecks": {
                "zeroSize": zero_size,
                "outOfStage": outside,
                "fullyClipped": clip_zero,
                "transparent": transparent,
            },
            "text": _text_plan(texts[nid]) if is_text else None,
            "asset": {
                "file": src.get("asset"),
                "kind": "image" if src.get("asset") else None,
            } if nid in assets else None,
            "connector": None,
            "bakingAncestor": row.get("ancestorNodeId"),
        }
        nodes[nid] = rec

    # connector records (may reference nodes already in IR, or be standalone)
    def _svg_path(crec):
        saved = crec.get("saved")
        if isinstance(saved, dict):
            arr = saved.get("saved") or []
        elif isinstance(saved, list):
            arr = saved
        else:
            arr = []
        for s in arr:
            if isinstance(s, dict) and s.get("path"):
                return s["path"]
        return None

    for nid, crec in connector_recs.items():
        con_base = {
            "sourceNodeId": crec.get("sourceNodeId") or crec.get("fromNodeId")
                            or crec.get("from"),
            "targetNodeId": crec.get("targetNodeId") or crec.get("toNodeId")
                            or crec.get("to"),
            "arrowheadAt": crec.get("arrowheadAt") or crec.get("arrowDirection")
                           or "to",
            "svgPath": _svg_path(crec),
            "assetKind": crec.get("assetKind", "svg"),
            "tipResolution": crec.get("tipResolution"),
        }
        if nid in nodes:
            nodes[nid]["connector"] = dict(con_base)
            nodes[nid]["type"] = nodes[nid]["type"] or "CONNECTOR"
        else:
            geo = _geo_for(nid)
            nodes[nid] = {
                "nodeId": nid, "parentId": None, "children": [],
                "name": crec.get("name"), "type": "CONNECTOR",
                "disposition": "rendered-asset",
                "dispositionReason": "connector svg export",
                "sourceFields": {"visible": True, "effectiveVisible": True,
                                 "effectiveOpacity": 1.0, "isMask": False},
                "visibilityChain": [], "effectiveVisibility": True,
                "designBox": geo["designBox"], "renderAABB": geo["renderAABB"],
                "absoluteRenderBounds": geo["renderAABB"],
                "exportSize": geo["exportSize"], "pngPlacement": geo["pngPlacement"],
                "effectiveClipRect": geo["effectiveClipRect"],
                "effectiveMatrix": geo["effectiveMatrix"],
                "zIndexChain": list(crec.get("zIndexChain") or []),
                "autoLayout": _auto_layout(crec),
                "fillsKind": "unknown",
                "boxComparability": {"rotated": _is_rotated(geo["effectiveMatrix"]),
                                     "flipped180": False, "note": "connector"},
                "derivedChecks": {"zeroSize": False, "outOfStage": False,
                                  "fullyClipped": False, "transparent": False},
                "text": None,
                "asset": {"file": _svg_path(crec), "kind": "svg"},
                "connector": dict(con_base),
            }

    # build child lists + visibility chain (parent -> child)
    for rec in nodes.values():
        pid = rec["parentId"]
        if pid and pid in nodes:
            nodes[pid]["children"].append(rec["nodeId"])

    # Effective clipping is inherited, not a property of the clipping frame's own
    # box. Older exports omitted effectiveClipRect on TEXT; derive it from the
    # live ancestor clipsContent chain so calibrated line plans cannot leak past
    # their Figma frame. Existing export clip rects are intersected, never reset.
    def _propagate_clip(rec, inherited_clip):
        own_clip = _clip_intersection(rec.get("effectiveClipRect") or stage_box,
                                      inherited_clip or stage_box)
        rec["effectiveClipRect"] = own_clip
        child_clip = own_clip
        if (rec.get("visual") or {}).get("clipsContent") is True:
            child_clip = _clip_intersection(child_clip, rec["renderAABB"])
        if rec.get("designBox"):
            rec["derivedChecks"]["fullyClipped"] = \
                _intersection_area(rec["designBox"], child_clip) <= 0.0
        for cid in rec["children"]:
            _propagate_clip(nodes[cid], child_clip)

    for rec in nodes.values():
        if not rec["parentId"] or rec["parentId"] not in nodes:
            _propagate_clip(rec, stage_box)

    for rec in nodes.values():
        chain = []
        pid = rec["parentId"]
        while pid and pid in nodes:
            chain.append({"nodeId": pid,
                          "disposition": nodes[pid]["disposition"],
                          "effectiveVisibility": nodes[pid]["effectiveVisibility"]})
            pid = nodes[pid]["parentId"]
        rec["visibilityChain"] = chain

    # sort children deterministically by (zIndexChain, nodeId)
    def _zkey(nid: str) -> Tuple:
        return (tuple(nodes[nid]["zIndexChain"]), nid)
    for rec in nodes.values():
        rec["children"].sort(key=_zkey)

    _unify_dispositions(nodes, stage_box)

    # Stage background contract: rootFills is the ONLY carrier of the artboard
    # background. It MUST be propagated verbatim through source -> layout-plan
    # -> builder. Missing key == unknown (caller must block); empty array ==
    # explicit no fill (transparent + disclosure). Never fabricate here.
    root_fills = manifest.get("rootFills")
    return {
        "ruleVersion": RULE_VERSION,
        "source": {
            "artboardId": manifest.get("artboardId"),
            "artboardName": manifest.get("artboardName"),
            "artboardSize": {"w": _r3(stage_w), "h": _r3(stage_h)},
            "ledgerTotal": len(ledger),
            # Presence classification (build-side gates on these):
            #   rootFills is None      -> unknown -> block build
            #   rootFills == []        -> explicit no fill
            #   rootFills == [...]     -> composite via build_background_css
            "rootFills": root_fills,
            "rootBackground": manifest.get("rootBackground"),
            "rootBackgroundAsset": manifest.get("rootBackgroundAsset"),
            "rootImageAssets": manifest.get("rootImageAssets"),
        },
        "nodes": nodes,
        "unresolved": [],
    }


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------
def _render_box(rec: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """The box the DOM actually places: renderAABB, falling back to designBox.

    Matches audit_relations.plan_node_box so compiler and auditor use the SAME
    box family for every expected delta.
    """
    return rec.get("renderAABB") or rec.get("designBox")


def _paints(nodes: Dict[str, Dict[str, Any]], nid: str) -> bool:
    rec = nodes[nid]
    if not rec.get("effectiveVisibility", True):
        return False
    return rec.get("disposition") not in vr.NON_PAINTED_DISPOSITIONS \
        and rec.get("disposition") != "unresolved"


def analyze(ir: Dict[str, Any], tol: float = DEFAULT_TOL) -> Dict[str, Any]:
    nodes: Dict[str, Dict[str, Any]] = ir["nodes"]
    edges: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []

    def _add_edge(kind: str, source: str, target: str, expected: Dict[str, Any],
                  derivation: str, status: str = "derived",
                  unknown_reason: Optional[str] = None,
                  extra: Optional[Dict[str, Any]] = None) -> None:
        eid = _edge_id(kind, source, target)
        edge = {
            "edgeId": eid,
            "kind": kind,
            "source": source,
            "target": target,
            "expected": expected,
            "derivation": derivation,
            "status": status,
            "tolerance": tol,
            "ruleVersion": RELATIONS_VERSION,
        }
        if unknown_reason:
            edge["unknownReason"] = unknown_reason
        if extra:
            edge.update(extra)
        edges.append(edge)

    def _same_family(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
        return a["boxComparability"]["rotated"] == b["boxComparability"]["rotated"]

    def _insets_art(pb, cb):
        return {
            "left": _r3(cb["x"] - pb["x"]),
            "right": _r3((pb["x"] + pb["w"]) - (cb["x"] + cb["w"])),
            "top": _r3(cb["y"] - pb["y"]),
            "bottom": _r3((pb["y"] + pb["h"]) - (cb["y"] + cb["h"])),
        }

    # ---- parentChildInset + textLineInset (artboard space, render family) ---
    for nid, rec in nodes.items():
        pid = rec["parentId"]
        if not pid or pid not in nodes:
            continue
        parent = nodes[pid]
        pb = _render_box(parent)
        cb = _render_box(rec)
        if not pb or not cb:
            _add_edge("parentChildInset", pid, nid, None, "manifest-tree",
                      status="unknown", unknown_reason="missing render box")
            continue
        if not _same_family(rec, parent):
            _add_edge("parentChildInset", pid, nid, None, "manifest-tree",
                      status="unknown",
                      unknown_reason="cross box-family (rotated vs unrotated)")
            continue
        # only paint-participating nodes take geometry edges; hidden rows are
        # reconciled by the visibility gate, not a geometry delta.
        if not (_paints(nodes, pid) and _paints(nodes, nid)):
            continue
        derivation = "transform-corrected" if parent["boxComparability"]["flipped180"] \
            else ("autoLayout" if parent["autoLayout"]["present"] else "geometry-inferred")
        _add_edge("parentChildInset", pid, nid, _insets_art(pb, cb), derivation)

        if rec.get("text"):
            lh = rec["text"].get("lineHeightPx")
            lb = dict(cb)
            if isinstance(lh, (int, float)):
                lb["h"] = float(lh)
            _add_edge("textLineInset", pid, nid, _insets_art(pb, lb),
                      "geometry-inferred")

    # ---- per-parent sibling / alignment / order / baseline / icon -----------
    def _zkey(nid):
        return (tuple(nodes[nid]["zIndexChain"]), nid)

    for nid, rec in nodes.items():
        children = [c for c in rec["children"]
                    if c in nodes and _paints(nodes, c)]
        if not children:
            continue
        ordered = sorted(children, key=_zkey)

        # siblingOrder: ONE edge per parent -> __order__ (audit shape)
        _add_edge("siblingOrder", nid, "__order__",
                  {"order": ordered}, "manifest-tree")

        boxed = [c for c in ordered if _render_box(nodes[c])]
        if len(boxed) < 2:
            continue

        def _rb(c):
            return _render_box(nodes[c])

        # siblingGap: consecutive in z-order; axis by center displacement
        for a, b in zip(boxed, boxed[1:]):
            ba, bb = _rb(a), _rb(b)
            cax, cay = _center(ba)
            cbx, cby = _center(bb)
            axis = "vertical" if abs(cby - cay) >= abs(cbx - cax) else "horizontal"
            if axis == "horizontal":
                gap = bb["x"] - (ba["x"] + ba["w"])
            else:
                gap = bb["y"] - (ba["y"] + ba["h"])
            _add_edge("siblingGap", a, b,
                      {"axis": axis, "gap": _r3(gap)}, "manifest-tree")

        # alignmentAxis: cluster by shared center-x / center-y
        cx = {c: _center(_rb(c))[0] for c in boxed}
        cy = {c: _center(_rb(c))[1] for c in boxed}
        # centerX clusters (vertical alignment)
        by_x = sorted(boxed, key=lambda c: cx[c])
        clusters: List[List[str]] = []
        for c in by_x:
            if clusters and abs(cx[c] - cx[clusters[-1][0]]) <= tol:
                clusters[-1].append(c)
            else:
                clusters.append([c])
        for cl in clusters:
            if len(cl) >= 2:
                anchor = cl[0]
                for other in cl[1:]:
                    _add_edge("alignmentAxis", anchor, other,
                              {"axis": "centerX",
                               "delta": _r3(cx[other] - cx[anchor])},
                              "geometry-inferred")
        # centerY clusters (horizontal alignment)
        by_y = sorted(boxed, key=lambda c: cy[c])
        yclusters: List[List[str]] = []
        for c in by_y:
            if yclusters and abs(cy[c] - cy[yclusters[-1][0]]) <= tol:
                yclusters[-1].append(c)
            else:
                yclusters.append([c])
        for cl in yclusters:
            if len(cl) >= 2:
                anchor = cl[0]
                for other in cl[1:]:
                    _add_edge("alignmentAxis", anchor, other,
                              {"axis": "centerY",
                               "delta": _r3(cy[other] - cy[anchor])},
                              "geometry-inferred")

        # baseline: text siblings whose bottom edges align
        texts_in = [c for c in boxed if nodes[c].get("text")]
        for i in range(len(texts_in)):
            for j in range(i + 1, len(texts_in)):
                a, b = texts_in[i], texts_in[j]
                ba, bb = _rb(a), _rb(b)
                if abs((ba["y"] + ba["h"]) - (bb["y"] + bb["h"])) <= tol:
                    _add_edge("baseline", a, b,
                              {"axis": "bottom",
                               "delta": _r3((bb["y"] + bb["h"]) -
                                            (ba["y"] + ba["h"]))},
                              "geometry-inferred")

        # iconHostCenter: small contained node paired with this host
        host_box = _render_box(rec)
        if host_box:
            for c in boxed:
                cb = _rb(c)
                small = cb["w"] <= host_box["w"] * 0.6 and \
                    cb["h"] <= host_box["h"] * 0.6
                contained = (cb["x"] >= host_box["x"] - tol and
                             cb["y"] >= host_box["y"] - tol and
                             cb["x"] + cb["w"] <= host_box["x"] + host_box["w"] + tol and
                             cb["y"] + cb["h"] <= host_box["y"] + host_box["h"] + tol)
                if small and contained:
                    hc = _center(host_box)
                    ic = _center(cb)
                    _add_edge("iconHostCenter", nid, c,
                              {"axis": "centerX",
                               "delta": _r3(ic[0] - hc[0])},
                              "geometry-inferred")

    # ---- connectorEdge ----------------------------------------------------
    for nid, rec in nodes.items():
        con = rec.get("connector")
        if not con:
            continue
        src = con.get("sourceNodeId")
        tgt = con.get("targetNodeId")
        if not src or not tgt or src == tgt or \
                src not in nodes or tgt not in nodes:
            _add_edge("connectorEdge", src or nid, tgt or "", None,
                      "manifest-tree", status="unverifiable",
                      unknown_reason="connector source/target undetermined",
                      extra={"uncertain": True})
            unresolved.append({"nodeId": nid, "kind": "connectorEdge",
                               "reason": "source/target undetermined"})
            continue
        sbox, tbox = _render_box(nodes[src]), _render_box(nodes[tgt])
        if not sbox or not tbox:
            _add_edge("connectorEdge", src, tgt, None, "manifest-tree",
                      status="unverifiable", unknown_reason="endpoint box absent",
                      extra={"uncertain": True})
            continue
        scx, scy = _center(sbox)
        tcx, tcy = _center(tbox)
        dx, dy = tcx - scx, tcy - scy
        if abs(abs(dx) - abs(dy)) <= tol:
            _add_edge("connectorEdge", src, tgt, None, "geometry-inferred",
                      status="unverifiable",
                      unknown_reason="relative position ambiguous (diagonal)",
                      extra={"uncertain": True})
            continue
        if abs(dx) > abs(dy):
            s_edge = "right" if dx > 0 else "left"
            t_edge = "left" if dx > 0 else "right"
        else:
            s_edge = "bottom" if dy > 0 else "top"
            t_edge = "top" if dy > 0 else "bottom"
        # remember derived edges on the connector record for the plan
        con["sourceEdge"] = s_edge
        con["targetEdge"] = t_edge
        con["uncertain"] = False
        _add_edge("connectorEdge", src, tgt,
                  {"sourceEdge": s_edge, "targetEdge": t_edge, "gap": 0.0},
                  "geometry-inferred", extra={"uncertain": False})

    edges.sort(key=lambda e: (e["edgeId"], e["kind"], e["source"], e["target"]))

    derived = sum(1 for e in edges if e["status"] == "derived")
    unknown = sum(1 for e in edges if e["status"] != "derived")
    by_kind: Dict[str, int] = {}
    for e in edges:
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1

    return {
        "ruleVersion": RELATIONS_VERSION,
        "nodes": [_compact_node(nodes[n]) for n in sorted(
            nodes, key=lambda x: (tuple(nodes[x]["zIndexChain"]), x))],
        "edges": edges,
        "unresolved": unresolved,
        "summary": {
            "nodeCount": len(nodes),
            "edgeCount": len(edges),
            "derivedEdges": derived,
            "unknownEdges": unknown,
            "byKind": by_kind,
        },
    }


def _compact_node(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "nodeId": rec["nodeId"],
        "parentId": rec["parentId"],
        "type": rec["type"],
        "disposition": rec["disposition"],
        "effectiveVisibility": rec["effectiveVisibility"],
        "designBox": rec["designBox"],
        "renderAABB": rec["renderAABB"],
        "boxComparability": rec["boxComparability"],
        "zIndexChain": rec["zIndexChain"],
    }


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------
def plan(ir: Dict[str, Any], relations: Dict[str, Any]) -> Dict[str, Any]:
    nodes: Dict[str, Dict[str, Any]] = ir["nodes"]

    def _zkey(nid: str) -> Tuple:
        return (tuple(nodes[nid]["zIndexChain"]), nid)

    layers = []
    text_plans = []
    connector_plans = []
    asset_units = []
    for nid in sorted(nodes, key=_zkey):
        rec = nodes[nid]
        renders = rec["disposition"] == "rendered-pixels-visible"
        layer = {
            "nodeId": nid,
            "parentId": rec["parentId"],
            "type": rec["type"],
            "selector": '[data-n="%s"]' % nid,
            "zIndexChain": rec["zIndexChain"],
            "disposition": rec["disposition"],
            "effectiveVisibility": rec["effectiveVisibility"],
            "renderLayer": renders,
            "designBox": rec["designBox"],
            "renderAABB": rec["renderAABB"],
            "effectiveMatrix": rec["effectiveMatrix"],
            "cssMatrix": rec["effectiveMatrix"],
            "pngPlacement": rec["pngPlacement"],
            "exportSize": rec.get("exportSize"),
            "asset": (rec.get("asset") or {}).get("file"),
            "clipRect": rec["effectiveClipRect"],
            "boxFamily": "render" if rec["boxComparability"]["rotated"]
                         else "design",
            "visual": rec.get("visual") or {},
        }
        # D1: the artboard ROOT row has no source geometry (it is the stage), so
        # its designBox/renderAABB are null. Give it the stage box so the geometry
        # contract (and data_gate G3) is consistent without special-casing nodes.
        if nid == (ir.get("source") or {}).get("artboardId"):
            aw = (ir.get("source") or {}).get("artboardSize", {}).get("w", 0.0)
            ah = (ir.get("source") or {}).get("artboardSize", {}).get("h", 0.0)
            root_box = {"x": 0.0, "y": 0.0, "w": float(aw or 0.0),
                        "h": float(ah or 0.0)}
            layer["designBox"] = layer["designBox"] or root_box
            layer["renderAABB"] = layer["renderAABB"] or root_box
        # Flat visual keys the builder reads directly (build_background_css /
        # container_layer expect c.get('fills') on the layer, not nested).
        v = rec.get("visual") or {}
        layer.update({
            "fills": v.get("fills"),
            "strokes": v.get("strokes"),
            "strokeWeight": v.get("strokeWeight"),
            "effects": v.get("effects"),
            "cornerRadius": v.get("cornerRadius"),
            "opacity": v.get("opacity"),
            "effectiveOpacity": v.get("effectiveOpacity"),
            "blendMode": v.get("blendMode", "NORMAL"),
            "absoluteTransform": rec["effectiveMatrix"],
            "local": rec.get("_local_box"),
        })
        if rec.get("text"):
            layer["lineHeightPx"] = rec["text"].get("lineHeightPx")
        layers.append(layer)
        if rec.get("text"):
            # I1: textPlans[] must be SELF-CONTAINED for the builder (it reads
            # tp directly, not a join back through nodes[]). Merge geometry onto
            # the same record.
            tp = dict(rec["text"])
            tp.update({
                "nodeId": nid,
                "parentId": rec["parentId"],
                "type": rec["type"],
                "disposition": rec["disposition"],
                "effectiveVisibility": rec["effectiveVisibility"],
                "designBox": rec["designBox"],
                "renderAABB": rec["renderAABB"],
                "effectiveMatrix": rec["effectiveMatrix"],
                "cssMatrix": rec["effectiveMatrix"],
                "lineHeightPx": rec["text"].get("lineHeightPx"),
                "clipRect": rec["effectiveClipRect"],
            })
            text_plans.append(tp)
        if rec.get("connector"):
            con = dict(rec["connector"])
            src = con.get("sourceNodeId")
            tgt = con.get("targetNodeId")
            con.update({
                "nodeId": nid,
                "designBox": rec["designBox"],
                "renderAABB": rec["renderAABB"],
                "assetKind": rec.get("asset", {}).get("kind", "svg"),
            })
            # I5: never drop a connector plan because source/target are absent.
            # Emit a complete record; leave endpoints null + uncertain, and let
            # repair resolve the SVG tip via path.getPointAtLength. Do NOT guess.
            if not src or not tgt:
                con["uncertain"] = True
                con["unknownReason"] = (
                    "connector source/target not declared in export; "
                    "resolve tip from SVG path.getPointAtLength, do not infer "
                    "from bounding box")
            connector_plans.append(con)
        if rec["disposition"] == "rendered-pixels-visible" and rec.get("asset"):
            asset_units.append(nid)

    out = {
        "ruleVersion": RULE_VERSION,
        "artboard": ir["source"],
        "nodes": layers,
        "assets": [dict(l, kind="asset") for l in layers
                   if l["nodeId"] in set(asset_units)],
        "textPlans": sorted(text_plans, key=lambda t: t["nodeId"] or ""),
        "connectorPlans": sorted(connector_plans, key=lambda c: c["nodeId"]),
        "relations": relations["edges"],
        "exportUnits": asset_units,
        "unresolved": ir.get("unresolved", []) + relations.get("unresolved", []),
    }
    out["planHash"] = _sha256_json({k: v for k, v in out.items()
                                    if k != "planHash"})
    return out


def _apply_text_calibrations(layout_plan: Dict[str, Any],
                             calibration: Optional[Dict[str, Any]]) -> None:
    """Attach independently measured Figma PDF line plans to matching text nodes.

    Calibration is data, not an Agent-authored layout override: the concatenated
    native PDF lines must exactly reproduce the live characters. A measured
    baseline step may resolve AUTO line-height; no box-height division is used.
    """
    nodes = (calibration or {}).get("nodes") or {}
    for tp in layout_plan.get("textPlans", []):
        cal = nodes.get(tp.get("nodeId"))
        if not cal:
            continue
        lines = cal.get("lines") or []
        if not lines or not all(isinstance(x, str) for x in lines):
            raise ValueError(f"invalid calibrated lines for {tp.get('nodeId')}")
        if "".join(lines) != (tp.get("characters") or ""):
            raise ValueError(
                f"calibrated text does not match live characters for {tp.get('nodeId')}")
        tp["linePlan"] = {
            "lines": lines,
            "source": cal.get("source") or "figma-pdf-text-lines",
            "calibrationNodeId": cal.get("calibrationNodeId"),
            "pdfSha256": cal.get("pdfSha256"),
        }
        lh = cal.get("lineHeightPx")
        if isinstance(lh, (int, float)) and lh > 0:
            tp["lineHeightPx"] = float(lh)
            tp["lineHeightSource"] = "figma-pdf-baseline-step"
    layout_plan["planHash"] = _sha256_json(
        {k: v for k, v in layout_plan.items() if k != "planHash"})


# ---------------------------------------------------------------------------
# orchestration + IO
# ---------------------------------------------------------------------------
def _local_boxes_from_raw(raw: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
    """Recover per-node local boxes (parent frame) from all geometry sources."""
    out: Dict[str, Dict[str, float]] = {}
    for src in (raw["manifest"].get("assets", []) or []):
        lb = _box(src.get("local"))
        if lb:
            out[src["nodeId"]] = lb
    for src in (raw["manifest"].get("containerVisuals", []) or []):
        lb = _box(src.get("local"))
        if lb:
            out[src["nodeId"]] = lb
    for src in (raw["text_manifest"].get("texts", []) or []):
        lb = _box(src.get("local"))
        if lb:
            out[src["nodeId"]] = lb
    return out


def compile_bundle(raw: Dict[str, Any], tol: float = DEFAULT_TOL):
    ir = normalize(raw)
    ir["_local_by_id"] = _local_boxes_from_raw(raw)
    # Attach local boxes for analyze/audit. DO NOT reproject renderAABB here:
    # the export manifest already carries the artboard-space render truth
    # (relativeToArtboard / intrinsicRenderBounds), which _geo_for picked up.
    # The previous I6 override fed PARENT-relative local corners into the
    # artboard-absolute matrix -> double translation (e.g. fish-1 landed at
    # x=1337,y=-3049 instead of 3749,40). Render truth is never recomputed
    # downstream; if a node lacks it, rec.renderAABB stays None and the plan
    # gate treats it as missing (unknown), not as a guessed box.
    for nid, rec in ir["nodes"].items():
        rec["_local_box"] = ir["_local_by_id"].get(nid)
    relations = analyze(ir, tol=tol)
    layout_plan = plan(ir, relations)
    if raw.get("text_calibration"):
        _apply_text_calibrations(layout_plan, raw["text_calibration"])
        ir["textCalibration"] = raw["text_calibration"]
    # strip private keys before output
    ir_out = {k: v for k, v in ir.items() if not k.startswith("_")}
    for rec in ir_out["nodes"].values():
        rec.pop("_local_box", None)
    return ir_out, relations, layout_plan, layout_plan["planHash"]


def write_outputs(out_dir: str, ir: Dict[str, Any],
                  relations: Dict[str, Any], layout_plan: Dict[str, Any],
                  plan_hash: str) -> Dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)

    def _dump(name: str, obj: Any) -> str:
        path = os.path.join(out_dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2,
                      sort_keys=True)
        return path

    paths = {
        "design-ir.json": _dump("design-ir.json", ir),
        "component-relations.json": _dump("component-relations.json", relations),
        "layout-plan.json": _dump("layout-plan.json", layout_plan),
    }
    sidecar = os.path.join(out_dir, "layout-plan.json.sha256")
    with open(sidecar, "w", encoding="utf-8") as fh:
        fh.write(plan_hash + "  layout-plan.json\n")
    paths["layout-plan.json.sha256"] = sidecar
    return paths


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Deterministic Design Compiler")
    ap.add_argument("--export-dir", required=True,
                    help="dir containing manifest.json + text-manifest.json")
    ap.add_argument("--out", default=None,
                    help="output dir (default: same as --export-dir)")
    ap.add_argument("--tol", type=float, default=DEFAULT_TOL)
    args = ap.parse_args(argv)

    raw = extract(args.export_dir)
    ir, relations, layout_plan, plan_hash = compile_bundle(raw, tol=args.tol)
    out_dir = args.out or args.export_dir
    paths = write_outputs(out_dir, ir, relations, layout_plan, plan_hash)
    print(json.dumps({
        "wrote": paths,
        "nodes": relations["summary"]["nodeCount"],
        "edges": relations["summary"]["edgeCount"],
        "planHash": plan_hash,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
