#!/usr/bin/env python3
"""
Native Figma export pipeline (capability-validated for Figwright MCP 0.6.0).

Usage:
    python3 export_artboard.py <artboardId> [<artboardId> ...]
        [--out-root DIR] [--scale 1] [--label sample]

What it does per artboard:
  1. Reads the full subtree via get_node.
  2. Accumulates affine transforms down the tree (local x/y/w/h + rotation
     about center) to derive real render bounds relative to the artboard,
     handling rotation. (Strokes/effects expansion is recorded from the
     serializer when present; the PNG itself is rendered natively by Figma.)
  3. Export units = maximal TEXT-free subtrees:
       - a non-TEXT node whose subtree contains no TEXT is exported whole
         (masks / opacity / overlap / blend are composited natively by
         exportAsync);
       - a text-bearing group is recursed into, and each TEXT-free child
         subtree is exported instead — so no Figma text pixel ever lands in
         an asset. Text is rebuilt later by a separate DOM task.
     The artboard itself is NEVER exported; only its root fills are recorded.
  4. Writes 1x PNGs via save_screenshots (plugin exportAsync) and two JSON
     manifests: assets (geometry + provenance) and text (typography).

Dev-mode note: clone/hide-text writes are refused by the plugin in Dev Mode
(editor=dev). The recursive TEXT-free descent below achieves the same
text-free-assets goal without any writes.
"""
import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from figwright_client import FW  # noqa: E402


# ---------------------------------------------------------------------------
# affine transform (column-order like Figma relativeTransform: [a,b,c,d,tx,ty])
# ---------------------------------------------------------------------------
def node_matrix(x, y, w, h, rotation_deg):
    """Figma serialization convention: local-to-parent matrix M=[cos,-sin,sin,cos,x,y].

    The rect's top-left sits at (x,y); rotation is about the top-left origin
    (0,0), NOT about the center. Generic: no node-specific offset."""
    a = math.radians(-(rotation_deg or 0.0))
    c, s = math.cos(a), math.sin(a)
    # Figma relativeTransform rotation matrix [cos, sin, -sin, cos, tx, ty]
    return [c, s, -s, c, x, y]


def mat_mul(m1, m2):
    """Compose m1 ∘ m2 (apply m2 first, then m1)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return [
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    ]


def apply(m, px, py):
    return (m[0] * px + m[2] * py + m[4], m[1] * px + m[3] * py + m[5])


def render_bounds(m, w, h):
    """Axis-aligned bounds of rect (0,0,w,h) transformed by m."""
    corners = [apply(m, 0, 0), apply(m, w, 0), apply(m, w, h), apply(m, 0, h)]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return {"x": min(xs), "y": min(ys),
            "width": max(xs) - min(xs), "height": max(ys) - min(ys)}


# ---------------------------------------------------------------------------
# tree walk
# ---------------------------------------------------------------------------
def has_text(n):
    if n.get("type") == "TEXT":
        return True
    return any(has_text(c) for c in n.get("children", []) or [])


def annotate_tree(node, parent_eff=None, parent_clip_chain=None, parent_zchain=None):
    """Single pre-pass that stamps every node with its *effective* visibility,
    accumulated opacity, clip chain and z-index chain.

    Effective rules (generic, no node-specific compensation):
      * effective_visible = self.visible AND all ancestors visible.
      * effective_opacity = product of self.opacity and all ancestor opacities.
      * clip chain accumulates every ancestor with clipsContent=true (artboard
        space rects are computed later in the affine walk; here we just record
        the path).
      * zchain is the child-index path from the artboard root, used as the
        generic paint order key (Figma paints children in array order, later
        = on top).

    Every node gets ``_eff`` so collectors and the ledger can read one source
    of truth instead of recomputing visibility independently.
    """
    if parent_eff is None:
        eff = {
            "self_visible": bool(node.get("visible", True)),
            "ancestor_visible": True,
            "effective_visible": bool(node.get("visible", True)),
            "self_opacity": float(node.get("opacity", 1) or 1),
            "ancestor_opacity": 1.0,
            "effective_opacity": float(node.get("opacity", 1) or 1),
        }
        clip_chain = []
        zchain = []
    else:
        self_vis = bool(node.get("visible", True))
        self_op = float(node.get("opacity", 1) or 1)
        eff = {
            "self_visible": self_vis,
            "ancestor_visible": parent_eff["effective_visible"],
            "effective_visible": bool(self_vis and parent_eff["effective_visible"]),
            "self_opacity": self_op,
            "ancestor_opacity": parent_eff["effective_opacity"],
            "effective_opacity": parent_eff["effective_opacity"] * self_op,
        }
        clip_chain = list(parent_clip_chain or [])
        if node.get("clipsContent"):
            clip_chain.append(node.get("id"))
        zchain = list(parent_zchain or [])
    node["_eff"] = eff
    node["_zchain"] = zchain
    node["_clippath"] = clip_chain
    for i, child in enumerate(node.get("children", []) or []):
        annotate_tree(child, eff, clip_chain, zchain + [i])


def collect_export_units(node):
    """Return list of maximal text-free subtrees that are *effectively visible*.

    A node is a renderable unit iff:
      * it is not TEXT,
      * its subtree contains no TEXT,
      * it is effectively visible (self + all ancestors visible).
    Effectively-invisible subtrees are NOT recursed: their children cannot
    become visible by being exported separately. The ledger records why.
    """
    units = []
    for child in node.get("children", []) or []:
        eff = child.get("_eff", {})
        if child.get("type") == "TEXT":
            continue
        if not eff.get("effective_visible", True):
            child["_disposition"] = "skipped-invisible"
            continue
        if has_text(child):
            units.extend(collect_export_units(child))
        else:
            child["_disposition"] = "rendered-asset"
            units.append(child)
    return units


def collect_texts(node):
    """Collect effectively-visible TEXT nodes. Invisible text (self or ancestor)
    is NOT collected into the render manifest; it is recorded in the ledger as
    skipped rather than over-rendered."""
    out = []
    for child in node.get("children", []) or []:
        eff = child.get("_eff", {})
        if child.get("type") == "TEXT":
            if not eff.get("effective_visible", True):
                child["_disposition"] = "skipped-invisible"
            else:
                child["_disposition"] = "rendered-text"
                out.append(child)
        out.extend(collect_texts(child))
    return out


def slim_fills(fills):
    if not isinstance(fills, list):
        return fills
    out = []
    for f in fills:
        out.append(dict(f) if isinstance(f, dict) else f)
    return out


def autolayout_fields(n):
    """Pass through the live auto-layout (flexbox) fields verbatim.

    The compiler uses these to mark edges derivation='autoLayout' instead of
    geometry-inferred. When the node is NOT an auto-layout container the dict
    carries ``present=False`` so the compiler can disclose unknown rather than
    fabricate a zero gap. Additive only: missing keys stay absent.
    """
    if not isinstance(n, dict):
        return {"present": False}
    mode = n.get("layoutMode")
    out = {"present": bool(mode and mode != "NONE"), "layoutMode": mode}
    for k in ("primaryAxisAlignItems", "counterAxisAlignItems", "layoutWrap",
              "layoutSizingHorizontal", "layoutSizingVertical",
              "layoutGrow", "layoutAlign", "itemSpacing",
              "paddingLeft", "paddingRight", "paddingTop", "paddingBottom",
              "primaryAxisSizingMode", "counterAxisSizingMode",
              "counterAxisSpacing"):
        if k in n:
            out[k] = n[k]
    return out


def text_layout_fields(n):
    """Pass through live text layout fields verbatim (additive)."""
    if not isinstance(n, dict):
        return {}
    out = {}
    for k in ("textAutoResize", "textAlignHorizontal", "textAlignVertical",
              "layoutAlign", "layoutGrow",
              "paragraphIndent", "paragraphSpacing", "hyphenation",
              "characterStyleOverrides", "styleOverrideTable",
              "lineTypes", "lineIndentations", "runs"):
        if k in n:
            out[k] = n[k]
    return out


def png_size(path):
    """Read PNG width/height from the 8-byte IHDR header without PIL."""
    import struct
    with open(path, "rb") as fh:
        head = fh.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    w, h = struct.unpack(">II", head[16:24])
    return (w, h)


def image_fill_spec(f):
    """Generic identity of a Figma IMAGE paint.

    Figma carries the embedded-image key under two wire names depending on the
    source: the REST API uses ``imageRef``; the plugin API (paints from
    ``node.fills``) uses ``imageHash``. A robust parser must accept both, never
    assume one name, and never invent a key when neither is present.

    Returns ``(key, kind)`` where kind is ``"imageRef"`` or ``"imageHash"``, or
    ``(None, None)`` for a non-IMAGE paint or an IMAGE paint with no key.
    """
    if not isinstance(f, dict) or f.get("type") != "IMAGE":
        return None, None
    for k in ("imageRef", "imageHash"):
        v = f.get(k)
        if v:
            return v, k
    return None, None


# Contract tool names used to fetch raw image bytes by key. The plugin API maps
# an image hash to an exportable Image object; we ask the MCP bridge to do that
# for us. Both names are tried because the bridge version may expose either.
_IMAGE_EXPORT_TOOLS = ("export_image_by_hash", "get_image_bytes")


def export_image_by_key(fw, key, kind, dest_path):
    """Best-effort export of the raw image bytes for an IMAGE fill key.

    Generic: no node id, size or color is baked in. Any failure (tool absent,
    call error, decode error) returns ``False`` and leaves ``dest_path`` absent;
    the caller then records the fill as unresolved and discloses it. It never
    whitewashes. Returns True only when a real PNG bytes file was written.
    """
    import base64, hashlib
    # Deterministic, process-stable filename from the key itself (PYTHONHASHSEED
    # would make builtin hash() non-reproducible across runs).
    digest = hashlib.sha256(f"{kind}:{key}".encode("utf-8")).hexdigest()[:12]
    for tool in _IMAGE_EXPORT_TOOLS:
        try:
            res = fw.call(tool, **{kind: key, "outPath": dest_path})
        except Exception:
            continue
        # The bridge may write the file itself, or return base64 bytes.
        if isinstance(res, dict):
            b64 = res.get("data") or res.get("bytes64")
            if b64:
                try:
                    raw = base64.b64decode(b64)
                    with open(dest_path, "wb") as fh:
                        fh.write(raw)
                except Exception:
                    return False
        if os.path.exists(dest_path) and png_size(dest_path):
            return True
    return False


# ---------------------------------------------------------------------------
def export_artboard(fw, artboard_id, out_root, label, scale=1, bg_asset=None):
    tree = fw.get_node(artboard_id)
    if not tree:
        raise RuntimeError(f"artboard {artboard_id} not found")

    out_dir = os.path.join(out_root, label)
    os.makedirs(out_dir, exist_ok=True)

    # --- pre-pass: stamp effective visibility / opacity / zchain / clip path ---
    annotate_tree(tree)

    # --- walk with accumulated matrices ---
    units = collect_export_units(tree)
    texts = collect_texts(tree)

    def walk(n, parent_abs, clip_chain, is_root=False):
        local = node_matrix(n.get("x", 0), n.get("y", 0),
                            n.get("width", 0), n.get("height", 0), n.get("rotation", 0))
        # Root artboard: we work in its own local frame, so its page offset is the
        # reference origin and must NOT propagate to children.
        abs_m = parent_abs if is_root else mat_mul(parent_abs, local)
        n["_abs"] = abs_m
        n["_parent_abs"] = parent_abs
        n["_clipchain"] = clip_chain
        # Figma quirk: GROUP children live in the group's PARENT space, not in
        # the group's local space -> children inherit parent_abs directly.
        child_parent = parent_abs if n.get("type") == "GROUP" else abs_m
        # A clipsContent=true frame becomes an additional clip rect (axis-aligned
        # box in artboard space) for everything beneath it.
        child_chain = clip_chain
        if n.get("clipsContent"):
            rb = render_bounds(abs_m, n.get("width", 0), n.get("height", 0))
            child_chain = clip_chain + [{"nodeId": n.get("id"), "name": n.get("name"),
                                         "rect": {k: round(v, 3) for k, v in rb.items()}}]
        for c in n.get("children", []) or []:
            c["_parent_id"] = n.get("id")
            walk(c, child_parent, child_chain)
    # reference frame = artboard's own local space. Serializer child x/y are
    # artboard-relative, so the root matrix is identity.
    walk(tree, [1, 0, 0, 1, 0, 0], [], is_root=True)

    ART = {"w": tree.get("width", 0), "h": tree.get("height", 0),
           "clips": tree.get("clipsContent", True)}

    def subtree_render_union(n):
        """Union of axis-aligned render boxes over the whole subtree.
        GROUP boxes are skipped: group children already live in parent space."""
        if n.get("type") == "GROUP" and n.get("children"):
            box = None
        else:
            rb = render_bounds(n["_abs"], n.get("width", 0), n.get("height", 0))
            box = [rb["x"], rb["y"], rb["x"] + rb["width"], rb["y"] + rb["height"]]
        for c in n.get("children", []) or []:
            cb = subtree_render_union(c)
            if box is None:
                box = cb
            else:
                box[0] = min(box[0], cb[0]); box[1] = min(box[1], cb[1])
                box[2] = max(box[2], cb[2]); box[3] = max(box[3], cb[3])
        return box if box is not None else [0, 0, 0, 0]

    def clip_to_artboard(box):
        if not ART["clips"]:
            return box
        clipped = [max(box[0], 0), max(box[1], 0),
                   min(box[2], ART["w"]), min(box[3], ART["h"])]
        if clipped[2] <= clipped[0] or clipped[3] <= clipped[1]:
            return box  # fully outside the artboard -> no clip
        return clipped

    def box_to_bounds(box):
        return {"x": box[0], "y": box[1],
                "width": box[2] - box[0], "height": box[3] - box[1]}

    unit_ids = [u["id"] for u in units]
    print(f"[{label}] artboard {artboard_id}: {len(units)} export units, "
          f"{len(texts)} text nodes")

    # --- export all units in one plugin call ---
    saved = fw.save_screenshots(unit_ids, out_dir, fmt="PNG", scale=scale).get("saved", [])
    saved_by_id = {s["nodeId"]: s for s in saved}

    assets = []
    for u in units:
        s = saved_by_id.get(u["id"], {})
        path = s.get("path")
        dims = png_size(path) if path and os.path.exists(path) else None
        # Placement geometry is type-specific (validated against PNG pixels):
        #  - INSTANCE: rendered content overflows the instance box -> use the
        #    union of its children's render boxes.
        #  - GROUP / leaf nodes: the node's own render box (Figma export frame).
        # The artboard clips the result (clipsContent) when it overflows.
        intrinsic = box_to_bounds(subtree_render_union(u))
        residual = None
        png_w, png_h = (dims if dims else (u.get("width", 0), u.get("height", 0)))
        # Generic placement rule: the rasterized PNG covers the node's ACTUAL
        # rendered content. For an INSTANCE whose arrow/path children overflow
        # the reported instance box, that content bounds == subtree_render_union,
        # NOT a center-anchor on the (possibly tiny) instance frame. We anchor by
        # whichever box the PNG pixels actually match -- geometric, no fixed
        # compensation.
        own = render_bounds(u["_abs"], u.get("width", 0), u.get("height", 0))
        own_box = [own["x"], own["y"], own["x"] + own["width"], own["y"] + own["height"]]
        union_box = [intrinsic["x"], intrinsic["y"],
                     intrinsic["x"] + intrinsic["width"],
                     intrinsic["y"] + intrinsic["height"]]
        tol = 6.0
        match_union = (u.get("type") == "INSTANCE" and dims and
                       abs(intrinsic["width"] - png_w) < tol and
                       abs(intrinsic["height"] - png_h) < tol)
        if match_union:
            fr = union_box
            anchor = "subtree-render-union"
        else:
            fr = own_box
            anchor = "own-render-box"
        if u.get("type") == "INSTANCE" and not match_union:
            residual = ("instance-overflow: PNG size does not match own box or "
                        "subtree union; placement anchored by own-render-box; "
                        "arrow tips need per-edge audit verification")
        rb = box_to_bounds(clip_to_artboard(fr))
        # DESIGN / CSS layout box = the node's unrotated local rect mapped to
        # artboard coords (ancestor matrix only, NOT the node's own rotation).
        # This is what CSS left/top/width/height use. The PNG (exportSize) is the
        # rotation-baked pixel content; it is centered on the designBox center.
        design_local = node_matrix(u.get("x", 0), u.get("y", 0),
                                   u.get("width", 0), u.get("height", 0), 0)
        design = render_bounds(mat_mul(u["_parent_abs"], design_local),
                               u.get("width", 0), u.get("height", 0))
        # designBox = TRUE unclipped layout box; clipping is separate (effectiveClipRect)
        designBox = box_to_bounds(
            [design["x"], design["y"], design["x"] + design["width"],
             design["y"] + design["height"]])
        # PNG placement = render AABB, but origin clamped to the artboard's visible
        # rect (x>=0, y>=0). exportSize is already the VISIBLE PNG pixel size
        # (exportAsync clips overflow), so we do NOT re-clamp width/height or use a
        # negative offset. Nested clips are recorded separately in effectiveClipRect.
        # PNG placement = the chosen render box (own or subtree union), origin
        # clamped to the artboard's visible rect. exportSize is the visible PNG
        # pixel size, so width/height stay = PNG dims.
        ab = {"x": fr[0], "y": fr[1], "width": fr[2] - fr[0],
              "height": fr[3] - fr[1]}
        # Effect bounds: plugin exportAsync rasterizes absoluteRenderBounds,
        # which INCLUDES visible effect spread (e.g. LAYER_BLUR radius). The
        # plain render box does NOT. When the PNG pixel size matches the
        # effect-expanded box clipped to the artboard, anchor there -- otherwise
        # a blurred layer lands offset by exactly the blur radius (observed:
        # 1:16 ellipse, 124px blur -> PNG 841 tall placed at node y instead of
        # 59, shifting the whole gradient down 124px).
        blur_r = 0.0
        for e in (u.get("effects") or []):
            if not isinstance(e, dict) or e.get("visible") is False:
                continue
            if e.get("type") in ("LAYER_BLUR", "BACKGROUND_BLUR"):
                blur_r = max(blur_r, float(e.get("radius") or 0))
        eff_box = [fr[0] - blur_r, fr[1] - blur_r, fr[2] + blur_r, fr[3] + blur_r]
        eff_c = clip_to_artboard(eff_box) if blur_r else eff_box
        eff_w, eff_h = eff_c[2] - eff_c[0], eff_c[3] - eff_c[1]
        use_effect_box = bool(dims and blur_r and
                              abs(eff_w - dims[0]) <= 3 and abs(eff_h - dims[1]) <= 3)
        if use_effect_box:
            anchor = "effect-bounds"
            png_place = {"x": round(max(eff_c[0], 0.0), 3),
                         "y": round(max(eff_c[1], 0.0), 3),
                         "width": dims[0], "height": dims[1]}
        else:
            png_place = {"x": round(max(ab["x"], 0.0), 3),
                         "y": round(max(ab["y"], 0.0), 3),
                         "width": dims[0] if dims else ab["width"],
                         "height": dims[1] if dims else ab["height"]}
        # effective clip = intersection of all ancestor clip rects (artboard space)
        cc = u.get("_clipchain") or []
        eff = [0.0, 0.0, float(ART["w"]), float(ART["h"])]
        for c in cc:
            r = c["rect"]
            eff = [max(eff[0], r["x"]), max(eff[1], r["y"]),
                   min(eff[2], r["x"] + r["width"]), min(eff[3], r["y"] + r["height"])]
        eff_rect = {"x": round(eff[0], 3), "y": round(eff[1], 3),
                    "width": round(eff[2] - eff[0], 3), "height": round(eff[3] - eff[1], 3)}
        rec = {
            "nodeId": u["id"],
            "parentId": u.get("_parent_id"),
            "name": u.get("name"),
            "type": u.get("type"),
            "zIndexChain": u.get("_zchain"),
            "visible": u.get("visible", True),
            "effectiveVisible": u.get("_eff", {}).get("effective_visible", True),
            "opacity": u.get("opacity", 1),
            "effectiveOpacity": round(u.get("_eff", {}).get("effective_opacity", 1.0), 4),
            # Layer blend mode (composite property; not baked by exportAsync).
            "blendMode": u.get("blendMode", "NORMAL"),
            "rotation": u.get("rotation", 0),
            "local": {"x": u.get("x"), "y": u.get("y"),
                      "width": u.get("width"), "height": u.get("height")},
            "intrinsicRenderBounds": {k: round(v, 3) for k, v in intrinsic.items()},
            "relativeToArtboard": {k: round(v, 3) for k, v in rb.items()},
            "absoluteTransform": [round(x, 4) for x in u["_abs"]],
            "designBox": designBox,
            "cssRect": designBox,
            "pngPlacement": png_place,
            "clipChain": cc,
            "effectiveClipRect": eff_rect,
            "fills": slim_fills(u.get("fills")),
            "cornerRadius": u.get("cornerRadius"),
            "strokes": u.get("strokes"),
            "strokeWeight": u.get("strokeWeight"),
            "effects": u.get("effects"),
            "isMask": u.get("isMask", False),
            "asset": os.path.basename(path) if path else None,
            "exportSize": {"width": dims[0], "height": dims[1]} if dims else None,
            "scale": scale,
            "placementAnchor": anchor,
            "residual": residual,
            "flags": {k: s.get(k) for k in ("recovered", "empty") if k in s},
            "autoLayout": autolayout_fields(u),
        }
        # auto-flag mask/clipping groups whose clipped box doesn't match the PNG
        if (not residual and dims and
                (abs(rb["width"] - dims[0]) > 3 or abs(rb["height"] - dims[1]) > 3)):
            if len(cc) > 1:
                rec["residual"] = (
                    f"ancestor-clip: PNG is the UNCLIPPED node ({dims[0]}x{dims[1]}); "
                    f"place at relativeToArtboard then clip-path to effectiveClipRect "
                    f"{eff_rect} (see clipChain)")
            else:
                rec["residual"] = (
                    f"mask group: internal mask clips content; computed box "
                    f"{round(rb['width'])}x{round(rb['height'])} vs PNG {dims[0]}x{dims[1]}; "
                    f"place by PNG size at relativeToArtboard")
        assets.append(rec)

    # --- container visuals: containers that were recursed into because they
    # contain TEXT, so their OWN fills/strokes/radius/effects/opacity never landed
    # in any asset. Builder renders simple ones in CSS; complex ones are listed. ---
    def fill_kind(f):
        t = f.get("type", "")
        if t == "SOLID":
            c = f.get("color", {})
            return "solid", "rgb(%d,%d,%d)" % (round(c.get("r", 0) * 255),
                                               round(c.get("g", 0) * 255),
                                               round(c.get("b", 0) * 255))
        if t == "IMAGE":
            return "image", f.get("scaleMode", "FILL")
        return t.lower(), None

    container_visuals = []
    def collect_containers(n, parent_id):
        for c in n.get("children", []) or []:
            ceff = c.get("_eff", {})
            if (c.get("type") in ("FRAME", "GROUP", "INSTANCE") and
                    ceff.get("effective_visible", True)):
                own_fills = [f for f in (c.get("fills") or []) if f.get("visible", True)]
                own_strokes = c.get("strokes") or []
                effs = c.get("effects") or []
                if has_text(c):
                    # classify
                    kinds = [fill_kind(f)[0] for f in own_fills]
                    has_img = "image" in kinds
                    has_gradient = any(k not in ("solid", "image") for k in kinds)
                    has_shadow = any(e.get("type") in ("DROP_SHADOW", "INNER_SHADOW")
                                     and e.get("visible", True) for e in effs)
                    has_blur = any(e.get("type") in ("LAYER_BLUR", "BACKGROUND_BLUR")
                                   and e.get("visible", True) for e in effs)
                    cls = "simple-css" if (not has_img and not has_gradient
                                           and not has_blur and not has_shadow) else "complex-native"
                    ab = render_bounds(c["_abs"], c.get("width", 0), c.get("height", 0))
                    dl = node_matrix(c.get("x", 0), c.get("y", 0),
                                     c.get("width", 0), c.get("height", 0), 0)
                    db = render_bounds(mat_mul(c["_parent_abs"], dl),
                                       c.get("width", 0), c.get("height", 0))
                    cc = c.get("_clipchain") or []
                    eff = [0.0, 0.0, float(ART["w"]), float(ART["h"])]
                    for cn in cc:
                        r = cn["rect"]
                        eff = [max(eff[0], r["x"]), max(eff[1], r["y"]),
                               min(eff[2], r["x"] + r["width"]),
                               min(eff[3], r["y"] + r["height"])]
                    eff_rect = {"x": round(eff[0], 3), "y": round(eff[1], 3),
                                "width": round(eff[2] - eff[0], 3),
                                "height": round(eff[3] - eff[1], 3)}
                    container_visuals.append({
                        "nodeId": c["id"], "parentId": parent_id,
                        "name": c.get("name"), "type": c.get("type"),
                        "zIndexChain": c.get("_zchain"),
                        "rotation": c.get("rotation", 0),
                        "opacity": c.get("opacity", 1),
                        "effectiveOpacity": round(ceff.get("effective_opacity", 1.0), 4),
                        "blendMode": c.get("blendMode", "NORMAL"),
                        "effectiveVisible": ceff.get("effective_visible", True),
                        "designBox": {k: round(v, 3) for k, v in db.items()},
                        "renderAABB": {k: round(v, 3) for k, v in ab.items()},
                        "absoluteTransform": [round(x, 4) for x in c["_abs"]],
                        "local": {"x": c.get("x"), "y": c.get("y"),
                                  "width": c.get("width"), "height": c.get("height")},
                        "effectiveClipRect": eff_rect,
                        "clipsContent": c.get("clipsContent"),
                        "fills": slim_fills(own_fills),
                        "fillKinds": kinds,
                        "cornerRadius": c.get("cornerRadius"),
                        "strokes": slim_fills(own_strokes),
                        "strokeWeight": c.get("strokeWeight"),
                        "effects": effs,
                        "classification": cls,
                        "autoLayout": autolayout_fields(c),
                    })
            collect_containers(c, c.get("id"))
    collect_containers(tree, None)

    text_manifest = []
    for t in texts:
        rb = render_bounds(t["_abs"], t.get("width", 0), t.get("height", 0))
        teff = t.get("_eff", {})
        text_manifest.append({
            "nodeId": t["id"],
            "characters": t.get("characters"),
            "zIndexChain": t.get("_zchain"),
            "visible": t.get("visible", True),
            "effectiveVisible": teff.get("effective_visible", True),
            "opacity": t.get("opacity", 1),
            "effectiveOpacity": round(teff.get("effective_opacity", 1.0), 4),
            "fontName": t.get("fontName"),
            "fontSize": t.get("fontSize"),
            "letterSpacing": t.get("letterSpacing"),
            "lineHeight": t.get("lineHeight"),
            "textAlignHorizontal": t.get("textAlignHorizontal"),
            "textAlignVertical": t.get("textAlignVertical"),
            "textAutoResize": t.get("textAutoResize"),
            "rotation": t.get("rotation", 0),
            "relativeToArtboard": {k: round(v, 3) for k, v in rb.items()},
            "absoluteTransform": [round(x, 4) for x in t["_abs"]],
            "local": {"x": t.get("x"), "y": t.get("y"),
                      "width": t.get("width"), "height": t.get("height")},
            "fills": slim_fills(t.get("fills")),
            "segments": t.get("segments"),
            "textLayout": text_layout_fields(t),
        })

    # --- content ledger: every node in the subtree, with disposition ---------
    # A live node MUST be rendered (asset/text/container) or have a recorded
    # skip reason. This is the machine-readable cross-check the audit layer
    # consumes; it never silently drops a node.
    rendered_ids = set()
    for u in units:
        rendered_ids.add(u["id"])
    for t in texts:
        rendered_ids.add(t["id"])
    for c in container_visuals:
        rendered_ids.add(c["nodeId"])

    ledger = []
    def walk_ledger(n, parent_id=None, baking_ancestor=None, is_root=False):
        eff = n.get("_eff", {})
        nid = n.get("id")
        own_disp = n.get("_disposition")
        reason = None
        ancestor_node_id = baking_ancestor

        # Determine disposition. Order matters:
        #  1. Root artboard frame -> rendered-stage (its fills land on #stage).
        #  2. Invisible / transparent -> skipped-* (no need to look further).
        #  3. Node itself in units/texts/containers -> rendered-*; it becomes a
        #     new baking ancestor for its descendants (text-free subtree baked
        #     into one PNG).
        #  4. Descendant of a rendered-asset ancestor -> rendered-by-ancestor
        #     (its pixels are inside that ancestor's PNG; no separate DOM node).
        #  5. Empty visual container (no fills/strokes of its own) that has
        #     rendered descendants -> rendered-container (layout wrapper).
        #  6. Empty visual container with no rendered descendants ->
        #     skipped-empty-container.
        if is_root:
            disp = "rendered-stage"
            ancestor_node_id = None
        elif not eff.get("effective_visible", True):
            if not eff.get("ancestor_visible", True):
                reason = "ancestor-invisible"
            elif not eff.get("self_visible", True):
                reason = "self-invisible"
            disp = own_disp or "skipped-invisible"
        elif eff.get("effective_opacity", 1.0) < 0.01:
            disp = "skipped-transparent"
            reason = f"effective-opacity={eff.get('effective_opacity'):.4f}<0.01"
        elif nid in {u["id"] for u in units}:
            disp = own_disp or "rendered-asset"
            ancestor_node_id = None  # this node IS the baking ancestor
        elif nid in {t["id"] for t in texts}:
            disp = own_disp or "rendered-text"
            ancestor_node_id = None
        elif nid in {c["nodeId"] for c in container_visuals}:
            disp = own_disp or "rendered-container"
            ancestor_node_id = None
        elif baking_ancestor is not None:
            disp = "rendered-by-ancestor"
            reason = f"baked into ancestor asset {baking_ancestor}"
        elif n.get("type") == "TEXT":
            disp = "unresolved"
            reason = "effective-visible TEXT not collected into text manifest"
        else:
            # Effective-visible non-text node with no own visual and no rendered
            # ancestor bake. Check whether it has any rendered descendant.
            has_rendered_child = any(
                (c.get("_eff", {}).get("effective_visible", True)
                 and eff.get("effective_opacity", 1.0) > 0.01)
                for c in (n.get("children", []) or [])
            )
            own_fills = [f for f in (n.get("fills") or []) if f.get("visible", True)]
            own_strokes = n.get("strokes") or []
            if not own_fills and not own_strokes:
                if has_rendered_child:
                    disp = "rendered-container"
                    reason = "transparent layout wrapper; children render"
                else:
                    disp = "skipped-empty-container"
                    reason = "no fills, no strokes, no rendered descendants"
            else:
                disp = "unresolved"
                reason = ("effective-visible non-text node not exported as "
                           "asset/container and not baked by an ancestor")
        ledger.append({
            "nodeId": nid, "parentId": parent_id, "name": n.get("name"),
            "type": n.get("type"),
            "selfVisible": eff.get("self_visible", True),
            "effectiveVisible": eff.get("effective_visible", True),
            "effectiveOpacity": round(eff.get("effective_opacity", 1.0), 4),
            "zIndexChain": n.get("_zchain"),
            "clipPath": n.get("_clippath"),
            "disposition": disp, "reason": reason,
            "ancestorNodeId": ancestor_node_id,
        })
        # Propagate baking ancestor: if this node is itself a rendered-asset,
        # its descendants are baked into IT (not the outer ancestor).
        child_baking = nid if (nid in {u["id"] for u in units}) else ancestor_node_id
        for c in n.get("children", []) or []:
            walk_ledger(c, nid, child_baking)
    walk_ledger(tree, is_root=True)

    # --- root background composite (build-friendly) -------------------------
    def fill_to_layer(f):
        t = f.get("type")
        if t == "SOLID":
            c = f.get("color", {})
            return {"kind": "solid",
                    "css": "rgb(%d,%d,%d)" % (round(c.get("r", 0) * 255),
                                              round(c.get("g", 0) * 255),
                                              round(c.get("b", 0) * 255)),
                    "opacity": f.get("opacity", 1)}
        if t == "IMAGE":
            sm = f.get("scaleMode", "FILL")
            key, kind = image_fill_spec(f)
            layer = {
                "kind": "image", "scaleMode": sm,
                # Figma->CSS object-fit (validated by independent registration):
                # FILL=cover (cover crop), FIT=contain, STRETCH=fill (stretch),
                # CROP=cover, TILE=repeat.
                "objectFit": {"FILL": "cover", "FIT": "contain",
                              "STRETCH": "fill", "CROP": "cover",
                              "TILE": "repeat"}.get(sm, "cover"),
                # Generic image identity + transform, surfaced verbatim so the
                # builder can bind the paint to its exported bytes.
                "imageRef": f.get("imageRef"),
                "imageHash": f.get("imageHash"),
                "imageKey": key, "imageKeyKind": kind,
                "imageTransform": f.get("imageTransform"),
                "scalingFactor": f.get("scalingFactor"),
                "rotation": f.get("rotation", 0),
                "opacity": f.get("opacity", 1),
                "resolvable": bool(key),
            }
            if not key:
                layer["note"] = ("IMAGE paint carries no imageRef/imageHash in "
                                 "this serializer; layer will be disclosed, not "
                                 "whitewashed")
            return layer
        return {"kind": t, "opacity": f.get("opacity", 1)}
    root_bg = [fill_to_layer(f) for f in (tree.get("fills") or []) if f.get("visible", True)]

    # --- generic root IMAGE fill export: bind each paint to its raw bytes -----
    # The artboard itself is never exported as a whole (that would bake text),
    # so a root IMAGE paint has no PNG of its own. When the serializer exposes
    # the image key (imageRef / imageHash), fetch the raw image bytes generically
    # and register them by key. When no key is present, leave file=None so the
    # builder discloses instead of painting white.
    root_image_assets = []
    image_asset_map = {}
    for f in (tree.get("fills") or []):
        if not isinstance(f, dict) or f.get("type") != "IMAGE" or not f.get("visible", True):
            continue
        key, kind = image_fill_spec(f)
        entry = {"key": key, "kind": kind, "file": None,
                 "scaleMode": f.get("scaleMode", "FILL"),
                 "imageTransform": f.get("imageTransform"),
                 "status": "no-image-ref" if not key else "pending"}
        if key:
            import hashlib
            digest = hashlib.sha256(f"{kind}:{key}".encode("utf-8")).hexdigest()[:12]
            fname = f"root-image-{digest}.png"
            dest = os.path.join(out_dir, fname)
            try:
                if export_image_by_key(fw, key, kind, dest):
                    entry["file"] = fname
                    entry["status"] = "exported"
                    image_asset_map[key] = fname
                else:
                    entry["status"] = "export-unavailable"
            except Exception as e:  # never let a live call kill the manifest
                entry["status"] = f"export-failed:{type(e).__name__}"
        root_image_assets.append(entry)

    # Optional operator-provided root background image (generic: no hardcoded
    # name). When present, point it at the exported PNG sitting next to the
    # manifest; the builder resolves and layers it. When the artboard has an
    # IMAGE root fill but no asset is provided, the builder discloses it rather
    # than painting white.
    root_asset = locals().get("bg_asset")
    manifest = {
        "artboardId": artboard_id,
        "artboardName": tree.get("name"),
        "artboardSize": {"width": tree.get("width"), "height": tree.get("height")},
        "rootFills": slim_fills(tree.get("fills")),
        "rootBackground": {"stackOrderBottomToTop": root_bg,
                           "cssRule": "layer them in listed order, bottom->top"},
        "rootBackgroundAsset": root_asset,
        # Generic keyed registry: imageRef/imageHash -> exported raw-image file.
        # The builder binds a root/container IMAGE paint to its bytes by key.
        "imageAssetMap": image_asset_map,
        "rootImageAssets": root_image_assets,
        "clipsContent": tree.get("clipsContent", True),
        "pipeline": "figwright-native-export 0.6.0",
        "scale": scale,
        "note": "Dev-mode: no clone/hide writes; text-free via maximal TEXT-free subtree export.",
        "assetCount": len(assets),
        "assets": assets,
        "containerVisualCount": len(container_visuals),
        "containerVisuals": container_visuals,
        "contentLedger": ledger,
        "contentLedgerSummary": {
            "total": len(ledger),
            "rendered": sum(1 for r in ledger if str(r.get("disposition", "")).startswith("rendered")),
            "skippedInvisible": sum(1 for r in ledger if r.get("disposition") == "skipped-invisible"),
            "skippedTransparent": sum(1 for r in ledger if r.get("disposition") == "skipped-transparent"),
            "skippedEmptyContainer": sum(1 for r in ledger if r.get("disposition") == "skipped-empty-container"),
            "bakedByAncestor": sum(1 for r in ledger if r.get("disposition") == "rendered-by-ancestor"),
            "unresolved": sum(1 for r in ledger if r.get("disposition") == "unresolved"),
        },
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, "text-manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"artboardId": artboard_id, "textCount": len(text_manifest),
                   "texts": text_manifest}, fh, ensure_ascii=False, indent=2)
    print(f"[{label}] wrote {len(assets)} assets + manifests -> {out_dir}")
    return manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("artboardIds", nargs="+")
    ap.add_argument("--out-root", required=True)
    ap.add_argument("--label", action="append", help="per-artboard label, e.g. sample")
    ap.add_argument("--scale", type=float, default=1.0)
    args = ap.parse_args()
    labels = args.label or [f"ab-{i}" for i in range(len(args.artboardIds))]
    assert len(labels) == len(args.artboardIds), "need one --label per artboard"
    fw = FW()
    try:
        for aid, lab in zip(args.artboardIds, labels):
            export_artboard(fw, aid, args.out_root, lab, scale=args.scale)
    finally:
        fw.close()


if __name__ == "__main__":
    main()
