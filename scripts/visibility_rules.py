#!/usr/bin/env python3
"""Design Compiler -- actual visibility rules (pure, deterministic, browser-free).

This module is the single source of truth for "does this node actually paint
visible pixels", shared by the compiler (normalize/analyze), the builder and
the auditor. It implements the visibility contract from
``diagnostics/compiler-contract.json`` (rules DC-1..DC-6) and the iteration-3
visibility diagnosis:

  * DC-1 full node coverage -- ``annotate_visibility_tree`` recurses into
    invisible subtrees; EVERY subtree node becomes an IR row, never silently
    dropped.
  * DC-2 deterministic disposition -- one of the 12 dispositions below, decided
    by field arithmetic, never by "did a data-n string appear in HTML".
  * DC-3 computed paint area -- display/visibility/opacity/clip/mask/size/
    stage intersection/baking-conflict are combined into ``paintsPixels``.
  * DC-4 unknown is not pass -- gate results are pass|fail|unknown;
    ``aggregate_gates.all_pass`` is False unless every gate is pass.
  * DC-5 baking conflicts both directions -- coverage, double exposure,
    hidden child baked, transparent child pixel evidence.
  * DC-6 symmetric opacity threshold (0.01) across exporter/builder/auditor.

The module has no I/O, no browser and no node-specific knowledge: every
function takes plain dicts/lists so the compiler and the audit layer can reuse
it verbatim.
"""

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: DC-6: a node with effective opacity below this threshold paints nothing at
#: ALL three layers (not exported, not emitted as DOM, not counted as rendered).
OPACITY_SKIP_THRESHOLD = 0.01

#: geometric tolerance (px) for ancestor-PNG coverage of baked descendants.
COVERAGE_TOLERANCE_PX = 2.0

#: DC-2 disposition taxonomy.
DISPOSITIONS = frozenset({
    "rendered-pixels-visible",    # IR says it paints; browser confirms pixels
    "rendered-pixels-invisible", # visible chain intact but zero paint area
    "baked-into-ancestor-png",    # pixels live inside an ancestor asset PNG
    "self-invisible",             # self.visible == false
    "ancestor-invisible",         # self visible but an ancestor was hidden
    "transparent",                # effectiveOpacity < OPACITY_SKIP_THRESHOLD
    "zero-size",                  # design box has zero width or height
    "out-of-stage",               # design box does not intersect stage rect
    "fully-clipped",              # clip-chain intersection area == 0
    "mask-cover",                 # a mask ancestor covers the whole box
    "empty-wrapper",              # no own visible fills/strokes (layout only)
    "unresolved",                 # fields missing; must NOT silently pass
})

#: dispositions that must never produce visible DOM pixels.
NON_PAINTED_DISPOSITIONS = frozenset({
    "self-invisible", "ancestor-invisible", "transparent",
    "zero-size", "out-of-stage", "fully-clipped", "mask-cover",
    "empty-wrapper",
})

#: legacy ledger dispositions that mean "this node must not appear in HTML".
LEGACY_SKIPPED_DISPOSITIONS = frozenset({
    "skipped-invisible", "skipped-transparent", "skipped-empty-container",
})

GATE_PASS = "pass"
GATE_FAIL = "fail"
GATE_UNKNOWN = "unknown"

CONFLICT_BAKED_VISIBILITY = "baked-visibility-conflict"
CONFLICT_VERSION = "version_conflict"


# ---------------------------------------------------------------------------
# geometry helpers (pure, axis-aligned rects in artboard space)
# ---------------------------------------------------------------------------

def rect_intersection(a, b):
    """Intersection of two {x,y,width,height} rects; None when disjoint."""
    if not a or not b:
        return None
    ax1, ay1 = a["x"], a["y"]
    ax2, ay2 = ax1 + a["width"], ay1 + a["height"]
    bx1, by1 = b["x"], b["y"]
    bx2, by2 = bx1 + b["width"], by1 + b["height"]
    x1, y1 = max(ax1, bx1), max(ay1, by1)
    x2, y2 = min(ax2, bx2), min(ay2, by2)
    if x2 <= x1 or y2 <= y1:
        return None
    return {"x": x1, "y": y1, "width": x2 - x1, "height": y2 - y1}


def rect_area(r):
    if not r:
        return 0.0
    return float(r["width"]) * float(r["height"])


def rect_contains(outer, inner, tolerance=COVERAGE_TOLERANCE_PX):
    """True iff ``inner`` is inside ``outer`` (within tolerance)."""
    if not outer or not inner:
        return False
    ox1, oy1 = outer["x"], outer["y"]
    ox2, oy2 = ox1 + outer["width"], oy1 + outer["height"]
    ix1, iy1 = inner["x"], inner["y"]
    ix2, iy2 = ix1 + inner["width"], iy1 + inner["height"]
    return (ix1 >= ox1 - tolerance and iy1 >= oy1 - tolerance
            and ix2 <= ox2 + tolerance and iy2 <= oy2 + tolerance)


def get_box(node):
    """Extract an {x,y,width,height} box from a node, tolerating the several
    shapes the serializer/manifest layers use (x/y/width/height keys,
    ``box``/``designBox``/``renderAABB``/``relativeToArtboard`` dicts)."""
    if not isinstance(node, dict):
        return None
    for key in ("designBox", "renderAABB", "box", "relativeToArtboard",
                "exportBox", "pngPlacement"):
        v = node.get(key)
        if isinstance(v, dict) and "width" in v and "height" in v:
            return dict(v)
    if ("width" in node and "height" in node
            and ("x" in node or "y" in node)):
        return {"x": float(node.get("x", 0)), "y": float(node.get("y", 0)),
                "width": float(node["width"]), "height": float(node["height"])}
    return None


# ---------------------------------------------------------------------------
# effective visibility / opacity propagation
# ---------------------------------------------------------------------------

def _as_bool(v, default=True):
    if v is None:
        return default
    return bool(v)


def _as_float(v, default=1.0):
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def compute_effective_visibility(node, ancestors=None):
    """Self + ancestor visible/opacity chain.

    Prefers a pre-stamped ``_eff`` (as ``export_artboard.annotate_tree``
    produces); otherwise recomputes from ``visible``/``opacity`` and the
    optional ancestor chain (list from root -> parent).

    Returns a computed-visibility dict that mirrors the browser's resolved
    style without touching a DOM.
    """
    eff = node.get("_eff") if isinstance(node, dict) else None
    if isinstance(eff, dict) and "effective_visible" in eff:
        self_visible = bool(eff.get("self_visible", True))
        ancestor_visible = bool(eff.get("ancestor_visible", True))
        effective_visible = bool(eff.get("effective_visible", True))
        self_opacity = _as_float(eff.get("self_opacity", 1))
        effective_opacity = _as_float(eff.get("effective_opacity", 1))
    else:
        self_visible = _as_bool(node.get("visible", True))
        effective_opacity = _as_float(node.get("opacity", 1))
        ancestor_visible = True
        for anc in (ancestors or []):
            if not _as_bool(anc.get("visible", True)):
                ancestor_visible = False
            effective_opacity *= _as_float(anc.get("opacity", 1))
        effective_visible = self_visible and ancestor_visible
        self_opacity = _as_float(node.get("opacity", 1))

    computed_opacity = round(effective_opacity, 4)
    return {
        "selfVisible": self_visible,
        "ancestorsVisible": ancestor_visible,
        "effectiveVisible": effective_visible,
        "selfOpacity": self_opacity,
        "effectiveOpacity": effective_opacity,
        "computedDisplay": "none" if not effective_visible else "block",
        "computedVisibility": "visible",  # markers override after classification
        "computedOpacity": computed_opacity,
        "belowOpacityThreshold": effective_opacity < OPACITY_SKIP_THRESHOLD,
    }


def _has_visible_paint(node):
    """G-fills-visible: any own visible fill or stroke (effects ignored --
    they never create geometry)."""
    fills = node.get("fills") if isinstance(node, dict) else None
    if isinstance(fills, list):
        for f in fills:
            if isinstance(f, dict) and _as_bool(f.get("visible", True)):
                return True
            if not isinstance(f, dict):
                return True  # unknown fill shape -> treat as paint
    strokes = node.get("strokes") if isinstance(node, dict) else None
    if isinstance(strokes, list) and len(strokes) > 0:
        return True
    return False


def compute_visual_context(node, ancestors=None, stage_rect=None,
                          clip_rects=None, mask_rect=None):
    """Combine visibility chain with size / stage / clip / mask geometry.

    ``clip_rects`` -- artboard-space rects accumulated from every ancestor
    with clipsContent=True (and the stage rect itself).
    ``mask_rect``  -- artboard-space rect of the nearest isMask ancestor, if any.

    Returns the full computed-layer record contract of the iteration-3 design:
    computedDisplay / computedVisibility / computedOpacity / computedClipPath /
    computedMask / computedSize / computedStageIntersection / computedPaintArea
    plus ``paintsPixels``.
    """
    vis = compute_effective_visibility(node, ancestors)
    box = get_box(node)

    size = {"width": None, "height": None,
            "zeroWidth": None, "zeroHeight": None}
    stage_intersection = {"rect": None, "visibleAreaPx": None,
                          "outsideStage": None}
    clip_path = {"rect": None, "visibleAreaPx": None}
    mask_info = {"rect": mask_rect, "maskedAreaPx": None}

    if box is not None:
        w, h = float(box["width"]), float(box["height"])
        size = {"width": w, "height": h,
                "zeroWidth": w <= 0, "zeroHeight": h <= 0}

        if stage_rect is not None:
            srect = rect_intersection(box, stage_rect)
            stage_intersection = {
                "rect": srect,
                "visibleAreaPx": rect_area(srect),
                "outsideStage": srect is None,
            }

        effective_clip = box
        for cr in (clip_rects or []):
            effective_clip = rect_intersection(effective_clip, cr)
            if effective_clip is None:
                break
        clip_path = {"rect": effective_clip,
                     "visibleAreaPx": rect_area(effective_clip)}

        if mask_rect is not None:
            masked = rect_intersection(box, mask_rect)
            mask_info = {"rect": mask_rect,
                         "maskedAreaPx": rect_area(masked)}

    has_paint = _has_visible_paint(node)
    zero_size = bool(size["zeroWidth"] or size["zeroHeight"])
    out_stage = bool(stage_intersection["outsideStage"])
    clip_area = clip_path["visibleAreaPx"]
    paints = (
        vis["effectiveVisible"]
        and not vis["belowOpacityThreshold"]
        and not zero_size
        and not out_stage
        and (clip_area is not None and clip_area > 0)
        and has_paint
    )

    ctx = dict(vis)
    ctx.update({
        "box": box,
        "computedSize": size,
        "computedStageIntersection": stage_intersection,
        "computedClipPath": clip_path,
        "computedMask": mask_info,
        "hasVisiblePaint": has_paint,
        "paintsPixels": paints,
    })
    return ctx


def classify_disposition(ctx, node, baking_ancestor=None):
    """DC-2: deterministic disposition from a computed visual context.

    Branch order is load-bearing (most specific reason first). Unknown
    geometry -> ``unresolved`` (never a silent pass).
    """
    node_id = node.get("id") if isinstance(node, dict) else None
    if node_id is None:
        return "unresolved"

    if not ctx["ancestorsVisible"]:
        return "ancestor-invisible"
    if not ctx["selfVisible"]:
        return "self-invisible"
    if ctx["belowOpacityThreshold"]:
        return "transparent"

    size = ctx["computedSize"]
    if size["width"] is None:
        return "unresolved"
    if size["zeroWidth"] or size["zeroHeight"]:
        return "zero-size"

    si = ctx["computedStageIntersection"]
    if si["outsideStage"] is not None and si["outsideStage"]:
        return "out-of-stage"

    clip = ctx["computedClipPath"]
    # rect is None exactly when the clip chain zeroes the visible area.
    if clip["visibleAreaPx"] == 0:
        return "fully-clipped"

    mask = ctx["computedMask"]
    if mask["rect"] is not None and mask["maskedAreaPx"] == 0:
        return "mask-cover"

    if not ctx["hasVisiblePaint"]:
        return "empty-wrapper"

    if baking_ancestor:
        return "baked-into-ancestor-png"

    return "rendered-pixels-visible" if ctx["paintsPixels"] \
        else "rendered-pixels-invisible"


# ---------------------------------------------------------------------------
# DC-1: full subtree coverage walk
# ---------------------------------------------------------------------------

def annotate_visibility_tree(root, stage_rect=None):
    """Walk the WHOLE subtree (including invisible ones -- DC-1) and return a
    coverage map: {nodeId: row} plus an ordered row list.

    Each row:
      {nodeId, type, parentId, ctx, disposition, ancestors, clipRectChain,
       isRoot, bakingAncestor}

    ``stage_rect`` defaults to the root's own width/height (artboard bounds).
    Clip rects are taken from ancestor nodes' own boxes when they declare
    ``clipsContent=True``.
    """
    if stage_rect is None:
        stage_rect = {"x": 0, "y": 0,
                      "width": float(root.get("width", 0)),
                      "height": float(root.get("height", 0))}

    rows = {}
    order = []

    def walk(node, parent_id, ancestors, clip_rects, is_root):
        nid = node.get("id")
        own_clip = list(clip_rects)
        if node.get("clipsContent"):
            own_clip.append(get_box(node))
        mask_rect = get_box(node) if node.get("isMask") else None

        ctx = compute_visual_context(node, ancestors, stage_rect,
                                     clip_rects=own_clip, mask_rect=mask_rect)
        disp = classify_disposition(ctx, node)
        row = {
            "nodeId": nid,
            "type": node.get("type"),
            "parentId": parent_id,
            "isRoot": is_root,
            "ctx": ctx,
            "disposition": disp,
            "ancestors": [a.get("id") for a in ancestors],
            "clipRectChain": own_clip,
            "maskRect": mask_rect,
        }
        rows[nid] = row
        order.append(nid)

        child_ancestors = ancestors + [node]
        for child in (node.get("children") or []):
            walk(child, nid, child_ancestors, own_clip, False)

    walk(root, None, [], [stage_rect], True)
    return {"rows": rows, "order": order, "stageRect": stage_rect}


def propagate_baking(coverage, unit_node_ids):
    """Mark descendants of maximal export units as baked-into-ancestor-png.

    A node is baked when it is NOT itself an export unit but has an ancestor
    that IS an export unit (and it paints). Returns the updated coverage and a
    baked map {asset_nodeId: [descendantNodeId, ...]}.
    """
    rows = coverage["rows"]
    baked_map = {u: [] for u in unit_node_ids}

    for nid, row in rows.items():
        if nid in unit_node_ids:
            continue
        if row["disposition"] in NON_PAINTED_DISPOSITIONS | {"unresolved"}:
            continue
        # nearest ancestor that is an export unit
        baking = None
        for anc in reversed(row["ancestors"]):
            if anc in unit_node_ids:
                baking = anc
                break
        if baking:
            row["disposition"] = "baked-into-ancestor-png"
            row["bakingAncestor"] = baking
            baked_map.setdefault(baking, []).append(nid)
    return coverage, baked_map


# ---------------------------------------------------------------------------
# DC-5: baking-conflict checks (both directions)
# ---------------------------------------------------------------------------

def check_double_exposure(asset_node_ids, baked_descendant_ids):
    """BC-2: no node may be both a standalone <img> asset AND a baked
    descendant of another asset's PNG."""
    assets = set(asset_node_ids or [])
    baked = set(baked_descendant_ids or [])
    return sorted(assets & baked)


def check_baked_coverage(descendant_box, ancestor_aabb,
                         tolerance=COVERAGE_TOLERANCE_PX):
    """BC-1: every baked descendant's designBox must be covered by the ancestor
    asset's export AABB (pngPlacement + exportSize)."""
    return rect_contains(ancestor_aabb, descendant_box, tolerance)


def find_hidden_baked_conflicts(skipped_node_ids, baked_map):
    """BC-4: a skipped (hidden/transparent) node must NEVER be listed inside an
    ancestor asset's baked descendants -- that would mean Figma's export did
    not actually skip it (serializer field lied) and hidden pixels are baked
    into a visible parent PNG."""
    skipped = set(skipped_node_ids or [])
    conflicts = []
    for asset_id, desc_ids in (baked_map or {}).items():
        for did in desc_ids:
            if did in skipped:
                conflicts.append({
                    "kind": CONFLICT_BAKED_VISIBILITY,
                    "rule": "BC-4",
                    "nodeId": did,
                    "ancestorAssetId": asset_id,
                    "detail": "hidden/transparent node baked into visible "
                              "parent PNG; cannot be separated from asset",
                })
    return conflicts


def detect_baking_visibility_conflict(skipped_node_ids, baked_map,
                                      descendant_boxes=None, ancestor_aabbs=None):
    """Full baking-conflict scan. Returns a list of conflicts; each conflict
    is strict-blocked (caller must require a finer export, never silently drop
    live content)."""
    conflicts = list(find_hidden_baked_conflicts(skipped_node_ids, baked_map))

    # BC-1: coverage, only when geometry was supplied (else unresolved ->
    # reported to the caller, never silently passed).
    coverage_unknown = []
    if descendant_boxes and ancestor_aabbs:
        for asset_id, desc_ids in (baked_map or {}).items():
            anc = ancestor_aabbs.get(asset_id)
            for did in desc_ids:
                dbox = descendant_boxes.get(did)
                if anc is None or dbox is None:
                    coverage_unknown.append({"assetId": asset_id,
                                             "nodeId": did})
                    continue
                if not check_baked_coverage(dbox, anc):
                    conflicts.append({
                        "kind": CONFLICT_BAKED_VISIBILITY,
                        "rule": "BC-1",
                        "nodeId": did,
                        "ancestorAssetId": asset_id,
                        "detail": "baked descendant designBox not covered by "
                                  "ancestor PNG export AABB",
                    })
    return {"conflicts": conflicts, "coverageUnknown": coverage_unknown}


def detect_version_conflict(live_node, reference_node):
    """Live vs reference visibility disagreement on a node that paints.

    Policy (SOP): when live and reference disagree, mark ``version_conflict``
    and STOP -- never delete/alter live content just because the old reference
    said the node was hidden. The user decides the target version; the
    export must be re-run finer if pixels cannot be separated.
    """
    if not live_node or not reference_node:
        return None
    live_vis = _as_bool(live_node.get("visible", True))
    ref_vis = _as_bool(reference_node.get("visible", True))
    live_op = _as_float(live_node.get("opacity", 1))
    ref_op = _as_float(reference_node.get("opacity", 1))
    if live_vis == ref_vis and abs(live_op - ref_op) < 1e-6:
        return None
    return {
        "kind": CONFLICT_VERSION,
        "nodeId": live_node.get("id"),
        "live": {"visible": live_vis, "opacity": live_op},
        "reference": {"visible": ref_vis, "opacity": ref_op},
        "action": "block-and-request-finer-export",
        "detail": "live/reference visibility disagrees; live content must "
                  "NOT be deleted based on reference",
    }


# ---------------------------------------------------------------------------
# browser audit: measured-vs-IR evaluation (DC-3, DC-4)
# ---------------------------------------------------------------------------

def evaluate_browser_sample(ir_row, measured):
    """Compare one IR row against actual browser measurement.

    ``ir_row``    -- a coverage row (disposition/ctx) from
                     ``annotate_visibility_tree`` or the design-IR.
    ``measured``  -- what the browser actually reports:
                     {display, visibility, opacity, rect, stageRect,
                      naturalWidth, naturalHeight, visiblePixelCount,
                      clipRect}

    Every gate is independently pass/fail/unknown. Missing measurements ->
    unknown (DC-4: unknown is never a silent pass).
    """
    gates = []
    nid = ir_row.get("nodeId") or ir_row.get("nodeId", "?")
    disp = ir_row.get("disposition")
    ctx = ir_row.get("ctx") or {}
    eff_visible = ctx.get("effectiveVisible")
    expected_opacity = ctx.get("computedOpacity")

    def gate(name, status, detail=""):
        gates.append({"nodeId": nid, "gate": name, "status": status,
                      "detail": detail})

    if not measured:
        gate("browser-sample", GATE_UNKNOWN, "no DOM measurement collected")
        return {"nodeId": nid, "disposition": disp, "gates": gates}

    def need(*keys):
        missing = [k for k in keys if measured.get(k) is None]
        return missing

    # G-display: an effective-invisible node must resolve to display:none.
    miss = need("display")
    if miss:
        gate("G-display", GATE_UNKNOWN, "display not measured")
    elif eff_visible is False and measured["display"] != "none":
        gate("G-display", GATE_FAIL,
             f"effectiveVisible=false but display={measured['display']}")
    elif eff_visible is True and measured["display"] == "none":
        gate("G-display", GATE_FAIL,
             f"effectiveVisible=true but display=none")
    else:
        gate("G-display", GATE_PASS)

    # G-visibility: computed visibility inherits ancestor visibility:hidden
    # (CSS visibility is inherited). A node expected to PAINT must resolve to
    # 'visible'; display:none alone does not catch ancestor visibility:hidden.
    # Skipped/empty-wrapper dispositions are expected to be hidden.
    painted_expected = eff_visible and disp not in NON_PAINTED_DISPOSITIONS
    miss = need("visibility")
    if miss:
        gate("G-visibility", GATE_UNKNOWN, "visibility not measured")
    elif painted_expected and measured["visibility"] == "hidden":
        gate("G-visibility", GATE_FAIL,
             "expected painted node but computed visibility=hidden "
             "(self or an ancestor)")
    elif not painted_expected and measured["visibility"] == "hidden":
        gate("G-visibility", GATE_PASS, "expected hidden")
    else:
        gate("G-visibility", GATE_PASS)

    # G-opacity: computed opacity must match the browser (tolerance .02).
    miss = need("opacity")
    if miss or expected_opacity is None:
        gate("G-opacity", GATE_UNKNOWN, "opacity not measured")
    elif abs(float(measured["opacity"]) - float(expected_opacity)) > 0.02:
        gate("G-opacity", GATE_FAIL,
             f"measured {measured['opacity']} vs IR {expected_opacity}")
    else:
        gate("G-opacity", GATE_PASS)

    baked = disp == "baked-into-ancestor-png"
    skipped = disp in NON_PAINTED_DISPOSITIONS

    # G-no-paint-if-invisible: effectiveVisible=false -> zero visible pixels.
    miss = need("visiblePixelCount")
    if miss:
        gate("G-pixel-evidence", GATE_UNKNOWN,
             "per-node pixel sample not collected")
    elif eff_visible is False and measured["visiblePixelCount"] > 0:
        gate("G-no-paint-if-invisible", GATE_FAIL,
             f"invisible node painted {measured['visiblePixelCount']}px")
    elif skipped and measured["visiblePixelCount"] > 0:
        gate("G-no-paint-if-skipped", GATE_FAIL,
             f"{disp} node painted {measured['visiblePixelCount']}px")
    elif (disp == "rendered-pixels-visible" and not baked
          and measured["visiblePixelCount"] == 0):
        gate("G-paint-if-visible", GATE_FAIL,
             "visible node has zero non-transparent pixels")
    else:
        gate("G-pixel-evidence", GATE_PASS)

    # G-stage: measured rect must intersect stage exactly as IR predicts.
    miss = need("rect", "stageRect")
    if miss:
        gate("G-stage", GATE_UNKNOWN, "rect/stage not measured")
    else:
        m_rect = measured["rect"]
        stage = measured["stageRect"]
        inter = rect_intersection(m_rect, stage)
        ir_out = (ctx.get("computedStageIntersection") or {}).get(
            "outsideStage")
        if ir_out and inter is not None:
            gate("G-stage", GATE_FAIL, "IR out-of-stage but browser paints "
                                        "inside stage")
        elif (not ir_out) and inter is None:
            gate("G-stage", GATE_FAIL, "node rendered outside stage")
        else:
            gate("G-stage", GATE_PASS)

    # G-clip: effective clip area after ancestor overflow:hidden.
    miss = need("clipRect")
    if miss:
        gate("G-clip", GATE_UNKNOWN, "clip rect not measured")
    else:
        ir_clip_area = (ctx.get("computedClipPath") or {}).get("visibleAreaPx")
        if ir_clip_area == 0 and rect_area(measured["clipRect"]) > 0:
            gate("G-clip", GATE_FAIL, "IR fully-clipped but browser clip has "
                                      "visible area")
        else:
            gate("G-clip", GATE_PASS)

    # G-mask / G-natural-size for image assets.
    if measured.get("naturalWidth") is not None:
        if measured["naturalWidth"] == 0 or measured["naturalHeight"] == 0:
            gate("G-natural-size", GATE_FAIL, "asset PNG has zero size")
        else:
            gate("G-natural-size", GATE_PASS)

    return {"nodeId": nid, "disposition": disp, "gates": gates}


def aggregate_gates(*gate_lists):
    """Aggregate gate results. DC-4: allPass requires zero fail AND zero
    unknown -- unknown is never a silent pass."""
    flat = []
    for gl in gate_lists:
        flat.extend(gl or [])
    fails = [g for g in flat if g["status"] == GATE_FAIL]
    unknowns = [g for g in flat if g["status"] == GATE_UNKNOWN]
    passes = [g for g in flat if g["status"] == GATE_PASS]
    return {
        "pass": len(passes),
        "fail": len(fails),
        "unknown": len(unknowns),
        "fails": fails,
        "unknowns": unknowns,
        "allPass": len(fails) == 0 and len(unknowns) == 0,
        "strictBlocked": len(fails) > 0,
    }


# ---------------------------------------------------------------------------
# HTML-side helpers shared with audit_content
# ---------------------------------------------------------------------------

def is_marker_fragment(fragment):
    """Heuristic: does this raw HTML opening tag render zero pixels?
    (class 'container-visual marker' or inline visibility:hidden)."""
    if not fragment:
        return False
    return ("container-visual marker" in fragment
            or "visibility:hidden" in fragment)


def should_export_unit(ctx):
    """DC-6 symmetric skip rule shared with the exporter: a node below the
    opacity threshold / fully clipped / zero-size / out-of-stage must NOT be
    exported, emitted or counted at ANY layer."""
    return bool(ctx["paintsPixels"])
