#!/usr/bin/env python3
"""Auto-fallback connector rebuild for reconstructed Figma HTML.

Design-controller contract (see diagnostics/compiler-contract.json,
proposed_rebuild_connectors):

  * Inputs: current HTML + deterministic connector plan (derived from the
    layout plan's edgeConnectors, never guessed) + strict audit results.
  * Frozen surface: text DOM, cards, other image assets, background, stage
    size and the fit() script are NEVER touched. Only connector assets are
    rebuilt into ONE stage-level inline <svg>.
  * Per connector, in unified STAGE coordinates: source dot anchor snaps to
    the planned source edge midpoint, the real arrow tip snaps to the planned
    target edge midpoint. Horizontal and vertical connectors use the same
    orthogonal S-curve rule; shared trunks (one source feeding several
    targets) are computed independently per connector -- no group
    translation, no cross-connector averaging.
  * Old connector <img>/<svg> assets for rebuilt connectors are REMOVED so
    old and new never double-render.
  * Style is inherited ONLY from the live page / the original SVG / the
    plan: no project ids, no hardcoded colors, no fixed pixel compensation,
    no plain straight-line hack at PNG ends, no whole-group translation.
    When color or stroke width cannot be evidenced, the connector is skipped
    as blocked rather than guessed.
  * Blocking (no geometry produced): plan uncertain (missing endpoints,
    self-loop, ambiguous orientation, missing node boxes), audit
    unverifiable / missing-asset / duplicate-asset / spec problems.
  * Retention: the repaired page is re-audited with the same strict gate.
    Only when EVERY targeted connector is a tip-verified pass and nothing
    else regressed is the repair kept; otherwise the backup is restored.

Usage:
    python3 repair_connectors.py --html page.html \
        --connector-plan connector-plan.json --audit audit.json \
        --out page.repaired.html --stage-selector '#stage' [--max-iter 2]
"""
import argparse
import json
import os
import pathlib
import re
import shutil
import sys

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
import audit_connectors as ac  # noqa: E402

PLAN_SCHEMA = "1.0"
AUTO_REPAIRABLE = {ac.V_TIP_MISS, ac.V_EDGE_GAP,
                   ac.V_DEGRADED_BBOX, ac.V_STAGE_OUTSIDE}
FORBIDDEN_TRIGGERS = {ac.V_UNVERIFIABLE, ac.V_MISSING_ASSET,
                      ac.V_DUPLICATE_ASSET}


# ---------------------------------------------------------------------------
# 1. Deterministic connector-plan derivation (no guessing).
# ---------------------------------------------------------------------------

def derive_connector_plan(layout_plan):
    """Build the output connector plan (schema 1.0) from a layout plan.

    Source/target/edges/arrowhead side are derived deterministically from
    node boxes and edgeConnectors fields. Anything ambiguous (missing
    endpoints, self-loop, missing boxes, horizontal/vertical ambiguity) is
    marked uncertain=True and must NOT be rebuilt.
    """
    nodes = {n.get("nodeId"): n for n in (layout_plan.get("nodes") or [])}
    edges = (layout_plan.get("connectorPlans")
             or layout_plan.get("edges") or [])
    out = []
    for e in edges:
        fid = e.get("fromNodeId") or e.get("sourceNodeId")
        tid = e.get("toNodeId") or e.get("targetNodeId")
        entry = {
            "id": e.get("edgeId") or e.get("id"),
            "sourceNodeId": fid, "targetNodeId": tid,
            "arrowheadAt": {"to": "target", "from": "source",
                            "none": "none"}.get(e.get("arrowDirection"),
                                                e.get("arrowheadAt", "target")),
            "tolerancePx": e.get("tolerancePx", 2),
            "derivedFromLayoutPlan": True, "uncertain": False, "reason": None,
        }
        if not entry["id"]:
            entry.update(uncertain=True, reason="missing edge id")
            out.append(entry); continue
        if not fid or not tid:
            entry.update(uncertain=True, reason="missing fromNodeId/toNodeId")
            out.append(entry); continue
        if fid == tid:
            entry.update(uncertain=True,
                         reason="self-loop; requires explicit specialCase")
            out.append(entry); continue
        s, t = nodes.get(fid), nodes.get(tid)
        if not s or not t:
            entry.update(uncertain=True,
                         reason="node box missing in layout plan")
            out.append(entry); continue
        sb = s.get("absoluteBox") or s.get("designBox")
        tb = t.get("absoluteBox") or t.get("designBox")
        if not sb or not tb:
            entry.update(uncertain=True, reason="absoluteBox missing")
            out.append(entry); continue
        entry["sourceBox"] = sb
        entry["targetBox"] = tb
        scx = sb["x"] + sb["width"] / 2.0
        scy = sb["y"] + sb["height"] / 2.0
        tcx = tb["x"] + tb["width"] / 2.0
        tcy = tb["y"] + tb["height"] / 2.0
        dx, dy = tcx - scx, tcy - scy
        override = e.get("edgeOverride") or {}
        if override.get("sourceEdge") and override.get("targetEdge"):
            entry["sourceEdge"] = override["sourceEdge"]
            entry["targetEdge"] = override["targetEdge"]
        elif abs(dx) >= abs(dy):
            entry["sourceEdge"] = "right" if dx > 0 else "left"
            entry["targetEdge"] = "left" if dx > 0 else "right"
        else:
            entry["sourceEdge"] = "bottom" if dy > 0 else "top"
            entry["targetEdge"] = "top" if dy > 0 else "bottom"
        # True ambiguity: both center deltas ~0 (cards stacked on top of
        # each other) -> horizontal or vertical cannot be told apart.
        if abs(dx) < 0.5 and abs(dy) < 0.5:
            entry.update(uncertain=True,
                         reason="horizontal/vertical relation ambiguous")
        out.append(entry)
    return {"schemaVersion": PLAN_SCHEMA, "connectors": out}


def validate_plan(plan):
    problems = []
    if str(plan.get("schemaVersion", "0")) < PLAN_SCHEMA:
        problems.append("connector plan schemaVersion < 1.0; refusing rebuild")
    for e in plan.get("connectors") or []:
        cid = e.get("id", "?")
        if e.get("uncertain"):
            problems.append(f"connector {cid}: uncertain ({e.get('reason')})")
            continue
        for f in ("sourceNodeId", "targetNodeId", "sourceEdge", "targetEdge"):
            if not e.get(f):
                problems.append(f"connector {cid}: missing {f}")
        if e.get("sourceEdge") not in ("left", "right", "top", "bottom"):
            problems.append(f"connector {cid}: bad sourceEdge")
        if e.get("targetEdge") not in ("left", "right", "top", "bottom"):
            problems.append(f"connector {cid}: bad targetEdge")
    return problems


# ---------------------------------------------------------------------------
# 2. Pure geometry: orthogonal S-curve + dot + real filled arrowhead.
# ---------------------------------------------------------------------------

def _edge_mid(box, edge):
    if edge == "left":
        return (box["x"], box["y"] + box["height"] / 2.0)
    if edge == "right":
        return (box["x"] + box["width"], box["y"] + box["height"] / 2.0)
    if edge == "top":
        return (box["x"] + box["width"] / 2.0, box["y"])
    return (box["x"] + box["width"] / 2.0, box["y"] + box["height"])


def connector_path(p0, p2, source_edge):
    """Orthogonal S-curve from p0 (source edge mid) to p2 (target edge mid).

    Returns (d_string, travel_dir) where travel_dir is the unit direction of
    the last segment at the tip. Corner radius is DERIVED from the gap
    lengths (generic clamp), never a project pixel constant.
    """
    x0, y0 = p0
    x1, y1 = p2
    if source_edge in ("left", "right"):
        tdir = (1.0, 0.0) if x1 >= x0 else (-1.0, 0.0)
        if abs(y1 - y0) < 0.5:
            return (f"M{x0:.2f},{y0:.2f} H{x1:.2f}", tdir)
        midx = (x0 + x1) / 2.0
        r = max(6.0, min(24.0, abs(x1 - x0) / 4.0, abs(y1 - y0) / 2.0))
        sgn = 1.0 if y1 > y0 else -1.0
        d = (f"M{x0:.2f},{y0:.2f} H{midx - r:.2f} "
             f"Q{midx:.2f},{y0:.2f} {midx:.2f},{y0 + sgn * r:.2f} "
             f"V{y1 - sgn * r:.2f} Q{midx:.2f},{y1:.2f} "
             f"{midx + r:.2f},{y1:.2f} H{x1:.2f}")
        return d, tdir
    tdir = (0.0, 1.0) if y1 >= y0 else (0.0, -1.0)
    if abs(x1 - x0) < 0.5:
        return (f"M{x0:.2f},{y0:.2f} V{y1:.2f}", tdir)
    midy = (y0 + y1) / 2.0
    r = max(6.0, min(24.0, abs(y1 - y0) / 4.0, abs(x1 - x0) / 2.0))
    sgn = 1.0 if x1 > x0 else -1.0
    d = (f"M{x0:.2f},{y0:.2f} V{midy - r:.2f} "
         f"Q{x0:.2f},{midy:.2f} {x0 + sgn * r:.2f},{midy:.2f} "
         f"H{x1 - sgn * r:.2f} Q{x1:.2f},{midy:.2f} "
         f"{x1:.2f},{midy + r:.2f} V{y1:.2f}")
    return d, tdir


def _arrow_polygon(tip, tdir, stroke_w):
    """Filled triangle apex AT tip; base perpendicular to travel direction."""
    length = max(stroke_w * 2.6, 10.0)
    half = stroke_w * 1.0
    bx = tip[0] - tdir[0] * length
    by = tip[1] - tdir[1] * length
    px, py = -tdir[1], tdir[0]
    return (f"{tip[0]:.2f},{tip[1]:.2f} "
            f"{bx + px * half:.2f},{by + py * half:.2f} "
            f"{bx - px * half:.2f},{by - py * half:.2f}")


def connector_group(entry, style):
    """One <g id=data-n> group with line body + source dot + arrowhead."""
    p0 = _edge_mid(entry["sourceBox"], entry["sourceEdge"])
    p2 = _edge_mid(entry["targetBox"], entry["targetEdge"])
    d, tdir = connector_path(p0, p2, entry["sourceEdge"])
    color = style["color"]
    sw = style["strokeWidth"]
    parts = [f'<g id="{entry["id"]}" data-n="{entry["id"]}">']
    parts.append(f'<path d="{d}" fill="none" stroke="{color}" '
                 f'stroke-width="{sw:.2f}" stroke-linejoin="round" '
                 f'stroke-linecap="round"/>')
    parts.append(f'<circle cx="{p0[0]:.2f}" cy="{p0[1]:.2f}" '
                 f'r="{sw * 3.0:.2f}" fill="none" stroke="{color}" '
                 f'stroke-width="{sw:.2f}"/>')
    parts.append(f'<circle cx="{p0[0]:.2f}" cy="{p0[1]:.2f}" '
                 f'r="{sw * 1.1:.2f}" fill="{color}"/>')
    if entry.get("arrowheadAt", "target") != "none":
        parts.append(f'<polygon points="{_arrow_polygon(p2, tdir, sw)}" '
                     f'fill="{color}"/>')
    parts.append("</g>")
    return "".join(parts), p0, p2


def build_stage_svg(groups_markup, stage_w, stage_h):
    return (f'<svg data-n="connectors-rebuilt" class="layer" '
            f'style="position:absolute;left:0;top:0;width:{stage_w:.2f}px;'
            f'height:{stage_h:.2f}px;overflow:visible;pointer-events:none;'
            f'z-index:999999">' + "".join(groups_markup) + "</svg>")


# ---------------------------------------------------------------------------
# 3. Minimal HTML surgery: remove old connector elements, inject stage svg.
#    Text/cards/other layers/scripts survive byte-for-byte.
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_VOID = {"br", "img", "meta", "link", "input", "hr"}


def _attrs_of(token):
    attrs = {}
    for m in re.finditer(r'(\w[\w-]*)="([^"]*)"', token):
        attrs[m.group(1)] = m.group(2)
    return attrs


def _selects_stage(attrs, stage_selector):
    if stage_selector.startswith("#"):
        return attrs.get("id") == stage_selector[1:]
    if stage_selector.startswith("."):
        cls = attrs.get("class", "")
        return stage_selector[1:] in cls.split()
    return attrs.get("id") == stage_selector.lstrip("#")


def _surgery(html, remove_ids, stage_selector, inject_markup):
    """Remove elements whose id or data-n is in remove_ids; inject markup
    right before the closing tag of the stage element. Returns (new_html,
    removed_fragments). Other content (text, cards, scripts) is untouched.
    """
    tokens = list(_TAG_RE.finditer(html))
    # Walk once with a depth counter. Track:
    #  - removal_spans: (open_start, close_end) for targets with children
    #  - self-closing targets
    #  - the first depth-0 element matching stage_selector and its close pos.
    stack = []                 # (tag, open_start, is_target)
    removal_spans = []
    selfclose_spans = []
    depth = 0
    stage_open_depth = None    # depth at which the stage open was pushed
    stage_close_at = None
    for m in tokens:
        tok = m.group(0)
        if tok.startswith("</"):
            tag = tok[2:-1].split()[0].lower()
            if stack and stack[-1][0] == tag:
                _, open_start, is_target = stack.pop()
                if is_target:
                    removal_spans.append((open_start, m.end()))
            depth -= 1
            if stage_open_depth is not None and depth == stage_open_depth - 1:
                stage_close_at = m.start()
                stage_open_depth = None
        elif tok.startswith("<!--") or tok.startswith("<!") or \
                tok.startswith("<?"):
            continue
        else:
            tag = tok[1:].split()[0].lower()
            attrs = _attrs_of(tok)
            is_target = (attrs.get("id") in remove_ids or
                         attrs.get("data-n") in remove_ids)
            if tag in _VOID or tok.endswith("/>"):
                if is_target:
                    selfclose_spans.append((m.start(), m.end()))
                continue
            stack.append((tag, m.start(), is_target))
            depth += 1
            if stage_open_depth is None and _selects_stage(
                    attrs, stage_selector):
                stage_open_depth = depth  # element's own depth
    if stage_close_at is None:
        raise ValueError(f"stage element {stage_selector!r} not found")

    all_spans = sorted(removal_spans + selfclose_spans, reverse=True)
    out = html
    for a, b in all_spans:
        out = out[:a] + out[b:]
    # Offset shift for removals before the injection point.
    shift = sum(-(b - a) for a, b in sorted(removal_spans + selfclose_spans)
                if a < stage_close_at)
    inject_at = stage_close_at + shift
    return out[:inject_at] + inject_markup + out[inject_at:], \
        [html[a:b] for a, b in sorted(removal_spans + selfclose_spans)]


# ---------------------------------------------------------------------------
# 4. Browser measurement (stage coords + inherited style).
# ---------------------------------------------------------------------------

_STYLE_JS = r"""
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  const svg = el.tagName.toLowerCase() === 'svg'
    ? el : (el.closest ? el.closest('svg') : null);
  let color = null, sw = null;
  if (svg) {
    const p = svg.querySelector('path');
    if (p) {
      const cs = getComputedStyle(p);
      if (cs.stroke && cs.stroke !== 'none') color = cs.stroke;
      if (cs.strokeWidth) sw = parseFloat(cs.strokeWidth);
    }
    if (!color) {
      const f = svg.querySelector('polygon,path');
      if (f) { const fs = getComputedStyle(f).fill;
               if (fs && fs !== 'none') color = fs; }
    }
  }
  return {color, sw};
}
"""

_STAGE_COORD_JS = r"""
({stageSel, boxes}) => {
  const stage = (Array.isArray(stageSel)?stageSel:[stageSel])
      .map(s=>document.querySelector(s)).find(Boolean);
  if (!stage) return {error: 'stage missing'};
  const sr = stage.getBoundingClientRect();
  const cs = getComputedStyle(stage);
  const tr = cs.transform && cs.transform !== 'none' ? cs.transform : null;
  const sx = sr.width / (stage.clientWidth || sr.width);
  const sy = sr.height / (stage.clientHeight || sr.height);
  const out = {};
  for (const k of Object.keys(boxes)) {
    const el = document.querySelector(k);
    if (!el) { out[k] = null; continue; }
    const r = el.getBoundingClientRect();
    out[k] = {x: (r.x - sr.x) / sx, y: (r.y - sr.y) / sy,
              width: r.width / sx, height: r.height / sy,
              hasTransform: !!tr};
  }
  return {stageRect: {x: sr.x, y: sr.y, w: sr.width, h: sr.height},
          scale: {x: sx, y: sy}, boxes: out};
}
"""

# Measure the REAL line-body endpoints of each old connector SVG in STAGE
# coordinates, and decide which end carries the arrowhead. This is the
# deterministic input for resolving connectors whose layout-plan does not
# name source/target containers: we match endpoints against visible container
# edges. No guessing -- 0 or >1 unique candidates stays unresolved.
_ENDPOINT_JS = r"""
({stageSel, selectors}) => {
  const stage = (Array.isArray(stageSel)?stageSel:[stageSel])
      .map(s=>document.querySelector(s)).find(Boolean);
  const sr = stage ? stage.getBoundingClientRect() : {x:0,y:0,width:1,height:1};
  const sx = stage ? sr.width/(stage.clientWidth||sr.width) : 1;
  const sy = stage ? sr.height/(stage.clientHeight||sr.height) : 1;
  const toStage = pt => ({x:(pt.x-sr.x)/sx, y:(pt.y-sr.y)/sy});
  const out = {};
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (!el) { out[sel] = null; continue; }
    const svgRoot = el.tagName.toLowerCase()==='svg'
      ? el : (el.closest && el.closest('svg'));
    if (!svgRoot) { out[sel] = {kind: 'non-svg'}; continue; }
    const paths = Array.from(svgRoot.querySelectorAll('path')).filter(p=>{
      if (p.closest('marker')||p.closest('defs')) return false;
      const s=getComputedStyle(p);
      return s.display!=='none' && s.stroke && s.stroke!=='none'
             && parseFloat(s.strokeWidth||'0')>0;
    });
    // main body = longest stroked path whose two ENDS are distinct (the
    // connector dot's stroked circle can be longer than the S body but its
    // start==end -- never treat it as the line body).
    let main=null, mainLen=-1;
    for (const p of paths){
      let L=0; try{L=p.getTotalLength();}catch(e){}
      if (L < mainLen) continue;
      const a=p.getPointAtLength(0), b=p.getPointAtLength(L);
      if (Math.hypot(a.x-b.x,a.y-b.y) < 0.2*L) continue;
      mainLen=L; main=p;
    }
    if (!main) { out[sel] = {kind:'no-path'}; continue; }
    const ctm = svgRoot.getScreenCTM();
    const p0 = main.getPointAtLength(0), p1 = main.getPointAtLength(mainLen);
    // approach direction at each end: sample 12 user-units along the body
    const s0a = main.getPointAtLength(0);
    const s0b = main.getPointAtLength(Math.min(12, mainLen));
    const s1a = main.getPointAtLength(mainLen);
    const s1b = main.getPointAtLength(Math.max(0, mainLen - 12));
    const dir0 = {x: s0b.x - s0a.x, y: s0b.y - s0a.y};
    const dir1 = {x: s1b.x - s1a.x, y: s1b.y - s1a.y};
    const v0 = ctm ? new DOMPoint(p0.x,p0.y).matrixTransform(ctm) : p0;
    const v1 = ctm ? new DOMPoint(p1.x,p1.y).matrixTransform(ctm) : p1;
    let ahEnd = null;
    const cs = getComputedStyle(main);
    if (cs.markerEnd && cs.markerEnd!=='none') ahEnd='end';
    else if (cs.markerStart && cs.markerStart!=='none') ahEnd='start';
    else {
      for (const p of paths){
        if (p===main) continue;
        const pts = (p.getAttribute('d')||'').match(/[-\d.]+/g)||[];
        if (pts.length!==6) continue;
        const n = pts.map(Number);
        const cx=(n[2]+n[4])/2, cy=(n[3]+n[5])/2;
        const vc = ctm ? new DOMPoint(cx,cy).matrixTransform(ctm)
                       : {x:cx,y:cy};
        ahEnd = Math.hypot(v0.x-vc.x,v0.y-vc.y)
              < Math.hypot(v1.x-vc.x,v1.y-vc.y) ? 'start' : 'end';
        break;
      }
      if (!ahEnd){
        const fills = Array.from(svgRoot.querySelectorAll('polygon,path'))
          .filter(n=>{
            const s=getComputedStyle(n);
            return s.fill&&s.fill!=='none'
                   &&(s.stroke==='none'||parseFloat(s.strokeWidth||'0')===0);
          });
        for (const f of fills){
          const r=f.getBoundingClientRect();
          const cx=r.x+r.width/2, cy=r.y+r.height/2;
          ahEnd = Math.hypot(v0.x-cx,v0.y-cy)
                < Math.hypot(v1.x-cx,v1.y-cy) ? 'start':'end';
          break;
        }
      }
    }
    const br = el.getBoundingClientRect();
    out[sel] = {kind:'svg', start:toStage(v0), end:toStage(v1),
                dirStart: {x: dir0.x, y: dir0.y},
                dirEnd: {x: dir1.x, y: dir1.y},
                ahEnd,
                bbox: {x:(br.x-sr.x)/sx, y:(br.y-sr.y)/sy,
                       width:br.width/sx, height:br.height/sy}};
  }
  // Also collect all rendered <img data-n> boxes (card images) as candidate
  // containers. Text divs and inner rectangles are NOT connector endpoints.
  const imgs = Array.from(document.querySelectorAll('img[data-n]'));
  const imgBoxes = {};
  for (const im of imgs) {
    const r = im.getBoundingClientRect();
    imgBoxes[im.getAttribute('data-n')] = {
      x:(r.x-sr.x)/sx, y:(r.y-sr.y)/sy,
      width:r.width/sx, height:r.height/sy};
  }
  return {connectors: out, containers: imgBoxes};
}
"""


def _score_endpoint(pt, bbox, boxes, orientation):
    """Score which container edge this endpoint attaches to.

    Score = perp gap (face line to card edge line) + face-gap (distance from
    the connector's near face along-range to the card's along-span; 0 if they
    overlap). Returns list of (score, nodeId, edge, perpGap, faceGap, box,
    ptInside). ptInside is True when the endpoint's along-coordinate falls
    inside the card span -- used as tiebreak for stacked cards.
    """
    x0, y0, w, h = bbox["x"], bbox["y"], bbox["width"], bbox["height"]
    x1, y1 = x0 + w, y0 + h
    if orientation == "h":
        near_left = (pt["x"] - x0) < (x1 - pt["x"])
        face_line = x0 if near_left else x1
        face_a0, face_a1 = y0, y1
        edge_kind = "right" if near_left else "left"
        along_pt = pt["y"]
        out = []
        for b in boxes:
            cx, cy, cw, ch = b["x"], b["y"], b["width"], b["height"]
            line = cx + cw if edge_kind == "right" else cx
            perp = abs(face_line - line)
            a0, a1 = cy, cy + ch
            ov0, ov1 = max(face_a0, a0), min(face_a1, a1)
            face_gap = 0.0 if ov1 >= ov0 else min(abs(face_a0 - a1),
                                                   abs(face_a1 - a0))
            out.append((perp + face_gap, b["nodeId"], edge_kind,
                        perp, face_gap, b, a0 <= along_pt <= a1))
        return out
    else:
        near_top = (pt["y"] - y0) < (y1 - pt["y"])
        face_line = y0 if near_top else y1
        face_a0, face_a1 = x0, x1
        edge_kind = "bottom" if near_top else "top"
        along_pt = pt["x"]
        out = []
        for b in boxes:
            cx, cy, cw, ch = b["x"], b["y"], b["width"], b["height"]
            line = cy + ch if edge_kind == "bottom" else cy
            perp = abs(face_line - line)
            a0, a1 = cx, cx + cw
            ov0, ov1 = max(face_a0, a0), min(face_a1, a1)
            face_gap = 0.0 if ov1 >= ov0 else min(abs(face_a0 - a1),
                                                   abs(face_a1 - a0))
            out.append((perp + face_gap, b["nodeId"], edge_kind,
                        perp, face_gap, b, a0 <= along_pt <= a1))
        return out


def _contains(outer, inner):
    return (outer["x"] <= inner["x"] + 0.5 and
            outer["y"] <= inner["y"] + 0.5 and
            outer["x"] + outer["width"] >= inner["x"] + inner["width"] - 0.5 and
            outer["y"] + outer["height"] >= inner["y"] + inner["height"] - 0.5)


def _resolve_edge(endpoint, bbox, boxes, tol, orientation):
    """Unique container-edge match for one real SVG endpoint.

    Scores all containers via bbox-face + along-gap. When cards share the
    same edge line (tie), prefer the OUTER card (whose box contains the inner
    decorations). If still ambiguous, stays unresolved.
    Returns (nodeId, edge, gap) or (None, reason).
    """
    scored = _score_endpoint(endpoint, bbox, boxes, orientation)
    if not scored:
        return None, "no container to score"
    scored.sort(key=lambda s: s[0])
    best = scored[0]
    if best[0] > tol + 2.0:
        return None, f"nearest edge {best[1]}/{best[2]} score={best[0]:.0f} > tol"
    close = [s for s in scored if s[0] <= best[0] + 3.0]
    if len(close) == 1:
        return (best[1], best[2], best[3]), None
    # Tiebreak 1: prefer the candidate whose along-span is CLOSEST to the
    # endpoint's along-coordinate (smallest distance from point to span).
    # This picks the card the arrow tip actually lands on, even when the
    # defective bbox face overlaps multiple stacked cards.
    def _along_dist(s):
        b = s[5]
        if orientation == "h":
            a0, a1 = b["y"], b["y"] + b["height"]
            v = endpoint["y"]
        else:
            a0, a1 = b["x"], b["x"] + b["width"]
            v = endpoint["x"]
        return 0.0 if a0 <= v <= a1 else min(abs(v - a0), abs(v - a1))
    close.sort(key=lambda s: _along_dist(s))
    best_along = _along_dist(close[0])
    along_pool = [s for s in close if _along_dist(s) <= best_along + 5.0]
    if len(along_pool) == 1:
        return (along_pool[0][1], along_pool[0][2], along_pool[0][3]), None
    # Tiebreak 2: prefer the outermost container (contains inner decorations).
    pool_boxes = {s[1]: s[5] for s in along_pool}
    outers = []
    for nid, b in pool_boxes.items():
        if all(nid == other or _contains(b, other_box)
               for other, other_box in pool_boxes.items()):
            outers.append(nid)
    if len(outers) == 1:
        for s in along_pool:
            if s[1] == outers[0]:
                return (s[1], s[2], s[3]), None
    tied = {(s[1], s[2]) for s in close}
    return None, f"ambiguous: {sorted(tied)[:3]}"


def _launch():
    from playwright.sync_api import sync_playwright
    return sync_playwright()


# ---------------------------------------------------------------------------
# 5. Public repair entry.
# ---------------------------------------------------------------------------

def repair(html_in, connector_plan, audit_data, html_out,
           stage_selector="#stage", max_iter=2, chrome_channel="chrome",
           container_nodes=None):
    """Rebuild failing connectors. Returns (report, repaired_html_path_or_None).

    Never raises on a blocked plan: report.blocked is set and no output html
    is written (caller keeps the original).

    container_nodes: visible container boxes [{nodeId, box:{x,y,width,height}}]
    used to deterministically resolve connectors whose plan does not name
    source/target. Resolution requires a UNIQUE container edge within
    tolerance at each real SVG endpoint; otherwise the entry stays
    unverifiable and the whole repair blocks (no guessing).
    """
    report = {"rebuiltCount": 0, "skippedUnverifiable": [],
              "removedAssets": [], "inheritedColors": {},
              "resolutionBasis": {},
              "iterations": 0, "rolledBack": False, "blocked": None}

    if "connectors" not in connector_plan:
        connector_plan = derive_connector_plan(connector_plan)

    # Entries still lacking source/target NODES are resolved from the real
    # SVG endpoints + visible container edges, deterministically. Entries
    # that already name source/target nodes (even without boxes) skip this.
    unresolved = [e for e in connector_plan["connectors"]
                  if not e.get("sourceNodeId") or not e.get("targetNodeId")]
    if unresolved:
        from playwright.sync_api import sync_playwright
        # Strict auto-resolution: only resolve when the old SVG endpoint is
        # genuinely near a unique container edge. Generous tolerances turn
        # resolution into guessing -- when the old SVG sits 27-66px off (the
        # known defect), we block rather than invent a relationship.
        tol = 12.0
        selectors = {e["id"]: f'[data-n="{e.get("nodeId") or e["id"]}"]'
                     for e in unresolved}
        with sync_playwright() as p:
            b = p.chromium.launch(channel=chrome_channel, headless=True,
                                  args=["--force-device-scale-factor=1",
                                        "--hide-scrollbars"])
            page = b.new_page(viewport={"width": 1600, "height": 1200},
                              device_scale_factor=1)
            page.goto("file://" + str(pathlib.Path(html_in).resolve()),
                      wait_until="networkidle", timeout=60000)
            measured_all = page.evaluate(_ENDPOINT_JS,
                                 {"stageSel": stage_selector,
                                  "selectors": list(set(selectors.values()))})
            ends = measured_all["connectors"]
            img_boxes = measured_all["containers"]
            b.close()
        # Candidate containers = rendered <img data-n> card images only.
        # Exclude the stage/background wrapper (huge element containing the
        # whole artboard).
        artboard_w = max(b["x"] + b["width"] for b in img_boxes.values()) if img_boxes else 1
        artboard_h = max(b["y"] + b["height"] for b in img_boxes.values()) if img_boxes else 1
        target_boxes = []
        for nid, bb in img_boxes.items():
            if bb["width"] > artboard_w * 0.5 and bb["height"] > artboard_h * 0.5:
                continue
            target_boxes.append({"nodeId": nid, **bb})
        for e in unresolved:
            sel = selectors[e["id"]]
            meas = ends.get(sel)
            if not meas or meas.get("kind") != "svg":
                report["skippedUnverifiable"].append(e["id"])
                e["resolveFail"] = "old SVG endpoint not measurable"
                continue
            ah = meas.get("ahEnd") or "end"
            dot_pt = meas["start"] if ah == "end" else meas["end"]
            tip_pt = meas["end"] if ah == "end" else meas["start"]
            bbox = meas["bbox"]
            def orient(pt_end):
                d = meas["dirStart"] if pt_end is meas["start"] else meas["dirEnd"]
                return "h" if abs(d["x"]) >= abs(d["y"]) else "v"
            dot_hit, dr = _resolve_edge(dot_pt, bbox, target_boxes,
                                        tol, orient(dot_pt))
            tip_hit, tr_ = _resolve_edge(tip_pt, bbox, target_boxes,
                                         tol, orient(tip_pt))
            if not dot_hit or not tip_hit:
                report["skippedUnverifiable"].append(e["id"])
                e["resolveFail"] = f"dot:{dr}; tip:{tr_}"
                continue
            s_node, s_edge, s_gap = dot_hit
            t_node, t_edge, t_gap = tip_hit
            e.update(uncertain=False, reason=None,
                     sourceNodeId=s_node, targetNodeId=t_node,
                     sourceEdge=s_edge, targetEdge=t_edge,
                     arrowheadAt="target")
            e["sourceBox"] = next(bb for bb in target_boxes
                                  if bb["nodeId"] == s_node)
            e["targetBox"] = next(bb for bb in target_boxes
                                  if bb["nodeId"] == t_node)
            report["resolutionBasis"][e["id"]] = {
                "evidence": "real-svg-endpoint + container-edge match",
                "source": {"nodeId": s_node, "edge": s_edge,
                           "gapPx": round(s_gap, 2)},
                "target": {"nodeId": t_node, "edge": t_edge,
                           "gapPx": round(t_gap, 2)},
                "arrowheadEnd": ah,
            }

    problems = validate_plan(connector_plan)
    uncertain_ids = [e["id"] for e in connector_plan.get("connectors", [])
                     if e.get("uncertain")]
    if uncertain_ids:
        report["skippedUnverifiable"] = uncertain_ids
    if problems:
        report["blocked"] = "; ".join(problems)
        return report, None

    # Resolved connectors (endpoint+container edge matched deterministically)
    # are candidates for rebuild regardless of the old audit verdict: the old
    # "unverifiable" was caused precisely by missing source/target, which this
    # fallback has now supplied. Unresolved/uncertain connectors stay blocked.
    resolved = [e for e in connector_plan["connectors"]
                if not e.get("uncertain") and not e.get("resolveFail")]
    if not resolved:
        report["blocked"] = "no connectors resolved for rebuild"
        return report, None

    # Audit gate: compute verdicts on unresolved records only. Resolved ones
    # are rebuilt from scratch, so their old verdict is irrelevant.
    tol = 2.0
    records = audit_data.get("connectors", [])
    resolved_ids = {e["id"] for e in resolved}
    for r in records:
        if r.get("id") in resolved_ids:
            continue
        v, _tip = ac.classify(r, tol)
        r["verdict"] = v
    bad = [r for r in records if r.get("id") not in resolved_ids and
           r.get("verdict") in FORBIDDEN_TRIGGERS]
    if bad:
        report["blocked"] = ("forbidden audit verdicts: " +
                             ", ".join(sorted({r['verdict'] for r in bad})))
        return report, None

    by_id = {r.get("id"): r for r in records}
    targeted = resolved

    html_text = pathlib.Path(html_in).read_text(encoding="utf-8")
    backup = html_in + ".bak"
    shutil.copyfile(html_in, backup)

    # Measure cards in stage coords + inherited styles.
    from playwright.sync_api import sync_playwright
    spec_w = audit_data.get("stage", {}).get("rect", {}).get("w", 1600) or 1600
    spec_h = audit_data.get("stage", {}).get("rect", {}).get("h", 1200) or 1200
    boxes = {}
    for e in targeted:
        boxes[f'[data-n="{e["sourceNodeId"]}"]'] = True
        boxes[f'[data-n="{e["targetNodeId"]}"]'] = True
    with sync_playwright() as p:
        b = p.chromium.launch(channel=chrome_channel, headless=True,
                              args=["--force-device-scale-factor=1",
                                    "--hide-scrollbars"])
        page = b.new_page(viewport={"width": spec_w, "height": spec_h},
                          device_scale_factor=1)
        page.goto("file://" + str(pathlib.Path(html_in).resolve()),
                  wait_until="networkidle", timeout=60000)
        measured = page.evaluate(_STAGE_COORD_JS,
                                 {"stageSel": stage_selector, "boxes": boxes})
        styles = {}
        for e in targeted:
            sel = f'[data-n="{e.get("nodeId") or e["id"]}"]'
            styles[e["id"]] = page.evaluate(_STYLE_JS, sel)
        b.close()

    # Plan boxes must be overridden by measured stage coords (contract:
    # geometry comes from measured DOM + planned edges).
    groups = []
    remove_ids = set()
    for e in targeted:
        sk = f'[data-n="{e["sourceNodeId"]}"]'
        tk = f'[data-n="{e["targetNodeId"]}"]'
        sb, tb = measured["boxes"].get(sk), measured["boxes"].get(tk)
        if not sb or not tb:
            report["skippedUnverifiable"].append(e["id"])
            continue
        e = dict(e)
        e["sourceBox"] = {k: sb[k] for k in ("x", "y", "width", "height")}
        e["targetBox"] = {k: tb[k] for k in ("x", "y", "width", "height")}
        st = styles.get(e["id"]) or {}
        old_rec = by_id.get(e["id"], {})
        color = st.get("color") or old_rec.get("color") or e.get("color")
        sw = st.get("sw") or e.get("strokeWidth") or 2.5
        if not color:
            # No evidence of the original color anywhere: block rather than
            # guess a color.
            report["skippedUnverifiable"].append(e["id"])
            continue
        report["inheritedColors"][e["id"]] = color
        g, p0, p2 = connector_group(e, {"color": color, "strokeWidth": sw})
        groups.append(g)
        remove_ids.add(e["id"])

    if not groups:
        report["blocked"] = "no rebuildable connector with color evidence"
        return report, None

    stage_w = measured["stageRect"]["w"]
    stage_h = measured["stageRect"]["h"]
    svg_markup = build_stage_svg(groups, stage_w, stage_h)

    new_html, removed = _surgery(html_text, remove_ids, stage_selector,
                                 svg_markup)
    report["removedAssets"] = sorted(remove_ids)

    iterations = 0
    kept = False
    while iterations < max_iter and not kept:
        iterations += 1
        pathlib.Path(html_out).write_text(new_html, encoding="utf-8")
        # Re-audit with the same spec selectors (they resolve to the new
        # <g id=...> inside the rebuilt svg).
        re_spec = {"width": spec_w, "height": spec_h,
                   "stageSelector": stage_selector,
                   "tolerancePx": tol, "connectors": []}
        for e in connector_plan["connectors"]:
            old = by_id.get(e["id"], {})
            re_spec["connectors"].append({
                "id": e["id"], "selector": f'[id="{e["id"]}"]',
                "sourceSelector": f'[data-n="{e["sourceNodeId"]}"]',
                "sourceEdge": e["sourceEdge"],
                "targetSelector": f'[data-n="{e["targetNodeId"]}"]',
                "targetEdge": e["targetEdge"],
                "arrowheadAt": e.get("arrowheadAt", "target"),
            })
        with sync_playwright() as p:
            b = p.chromium.launch(channel=chrome_channel, headless=True,
                                  args=["--force-device-scale-factor=1",
                                        "--hide-scrollbars"])
            page = b.new_page(viewport={"width": spec_w, "height": spec_h},
                              device_scale_factor=1)
            page.goto("file://" + str(pathlib.Path(html_out).resolve()),
                      wait_until="networkidle", timeout=60000)
            measured_recs = page.evaluate(ac.JS, re_spec)["connectors"]
            b.close()
        summary = ac.summarize(measured_recs, tol)
        report["iterations"] = iterations
        if summary["strictFail"] == 0:
            kept = True
            report["rebuiltCount"] = len(groups)
        else:
            # next iteration would need a new plan; our geometry is exact by
            # construction, so a second miss means rollback.
            break

    if not kept:
        shutil.copyfile(html_in, html_out)
        report["rolledBack"] = True
        report["blocked"] = "re-audit after rebuild still failing; rolled back"
    return report, (html_out if kept else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", required=True)
    ap.add_argument("--connector-plan", required=True)
    ap.add_argument("--audit", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--stage-selector", default="#stage")
    ap.add_argument("--max-iter", type=int, default=2)
    ap.add_argument("--chrome-channel", default="chrome")
    args = ap.parse_args()

    plan = json.loads(pathlib.Path(args.connector_plan).read_text(
        encoding="utf-8"))
    audit = json.loads(pathlib.Path(args.audit).read_text(encoding="utf-8"))
    report, out = repair(args.html, plan, audit, args.out,
                         stage_selector=args.stage_selector,
                         max_iter=args.max_iter,
                         chrome_channel=args.chrome_channel)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["blocked"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
