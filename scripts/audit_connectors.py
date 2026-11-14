#!/usr/bin/env python3
"""Per-connector audit for reconstructed Figma HTML (standalone small-node
SVG preferred; PNG bbox as degraded fallback).

Design contract (derived from historical regression evidence, see
references/connectors.md):

  * One connector = one record. Each record is judged on its own source edge,
    target edge, both endpoints, mid-path presence and arrowhead side. No
    group translation, no averaging across connectors.
  * Prefer an inline/standalone <svg> element whose <path> carries the line
    body, the bend(s), the start dot and a real arrowhead (marker-end or a
    filled triangle). For SVG we measure the REAL arrow tip via
    path.getPointAtLength(totalLength) -- that point is exactly where the
    marker is placed -- and compare it to the target edge midpoint.
  * When only a flattened <img> PNG is available, we can only measure the
    asset bbox against the source/target edge midpoints. That yields a
    `degraded-bbox` verdict, NEVER a tip-verified pass. The bbox must not be
    reported as the arrow tip.
  * Missing source or target selector/evidence => `unverifiable`, never pass.
  * The connector SVG must live INSIDE the stage container (the same element
    that carries the fit/scale transform) so it participates in stage scaling.
    If it is a sibling of the stage / placed after the section close tag, it
    does not scale and disappears from the overview fit. We detect that.
  * We also require the connector to be actually visible: non-zero bbox,
    computed stroke-width > 0 (or a filled arrowhead present), not
    display:none / visibility:hidden.

Machine-readable summary is always printed. Default strict gate:
`--strict` exits non-zero on ANY verdict that is NOT a tip-verified SVG pass
(including degraded-bbox PNG results) -- "未测不能给通过". Pass
`--allow-degraded-bbox` explicitly to relax the gate for PNG-only connectors;
other hard failures still block.

No user node ids, real text strings, brand colors, fixed pixel compensations
or artboard dimensions are baked into this script. All numbers come from the
spec file and the live DOM measurement.

Usage:
    python3 audit_connectors.py --html page.html --spec connectors.json \
        --out audit.json [--stage-selector .stage] [--strict]
"""
import argparse
import json
import pathlib
import sys
import urllib.parse

# ---------------------------------------------------------------------------
# Pure-python helpers (importable by tests without launching a browser).
# ---------------------------------------------------------------------------

# Verdict enum (machine-readable strings, stable for downstream consumers).
V_PASS = "pass"                      # SVG tip verified at target edge
V_DEGRADED_BBOX = "degraded-bbox"    # PNG bbox reaches edges; tip NOT verified
V_EDGE_GAP = "edge-gap"              # connector does not reach an expected edge
V_TIP_MISS = "tip-miss"              # SVG tip measured but misses target edge
V_STAGE_OUTSIDE = "stage-outside"    # connector not inside the stage element
V_INVISIBLE = "invisible"            # zero bbox / stroke-width 0 / display none
V_MISSING_ASSET = "missing-asset"    # connector selector found nothing
V_UNVERIFIABLE = "unverifiable"      # source/target evidence missing
V_DUPLICATE_ASSET = "duplicate-asset"  # selector matched >1 element (old +
                                       # new connector both rendered) -- hard
                                       # failure, NEVER auto-repaired


# Strict exit: anything that is NOT a tip-verified SVG pass blocks the gate.
# "未测不能给通过" -- a PNG bbox reach is reported separately but is NOT a
# pass under --strict. Use --allow-degraded-bbox to explicitly permit it.
ALL_NON_PASS = {V_DEGRADED_BBOX, V_EDGE_GAP, V_TIP_MISS, V_STAGE_OUTSIDE,
                V_INVISIBLE, V_MISSING_ASSET, V_UNVERIFIABLE,
                V_DUPLICATE_ASSET}
STRICT_FAIL = set(ALL_NON_PASS)  # default: everything except a real pass


def validate_spec(spec):
    """Return list of structural problems with the user-provided spec.

    An empty `connectors: []` list is a LEGITIMATE state: it means the board
    has no connectors (e.g. connectors.manifest.json with connectorCount=0),
    and we align with the reconstruct empty-manifest contract. We do NOT
    flag it. Per-connector field checks below apply only to connectors that
    are actually listed. We do not invent source/target for a connector that
    omits them; that is flagged AND the per-record audit marks it
    unverifiable (never pass).
    """
    problems = []
    conns = spec.get("connectors") or []
    # No problem for empty list.
    for i, c in enumerate(conns):
        cid = c.get("id", f"#{i}")
        if not c.get("selector"):
            problems.append(f"connector {cid}: missing selector")
        if not c.get("sourceSelector") or not c.get("targetSelector"):
            problems.append(f"connector {cid}: missing source/target selector")
        for side in ("sourceEdge", "targetEdge"):
            edge = c.get(side)
            if edge not in ("left", "right", "top", "bottom"):
                problems.append(
                    f"connector {cid}: {side} must be one of "
                    f"left/right/top/bottom (got {edge!r})")
        ah = c.get("arrowheadAt", "target")
        if ah not in ("source", "target", "none"):
            problems.append(f"connector {cid}: arrowheadAt must be "
                            f"source|target|none (got {ah!r})")
    return problems


def classify(rec, tol):
    """Attach a stable verdict + tipVerified flag to a measured record.

    Pure function: reads fields already measured in the browser JS, decides
    the verdict. Kept in Python so tests can exercise every branch without a
    browser.
    """
    status = rec.get("status")
    if status == V_MISSING_ASSET:
        return V_MISSING_ASSET, False
    if status == V_UNVERIFIABLE:
        return V_UNVERIFIABLE, False
    if status == V_STAGE_OUTSIDE:
        return V_STAGE_OUTSIDE, False
    if status == V_INVISIBLE:
        return V_INVISIBLE, False
    if status == V_DUPLICATE_ASSET:
        return V_DUPLICATE_ASSET, False

    # Geometry checks. Edge-line (NOT midpoint) is the contract: a horizontal
    # edge is the line x=card.left/right; we require the connector/tip to reach
    # that line within tolerance in the perpendicular axis, and to overlap the
    # card's span along the edge (with a small slack). Off-midpoint tip y/x
    # is NOT a defect.
    asset_kind = rec.get("assetKind")  # 'svg' | 'png' | 'unknown'

    def _edge_line_hit(side_hit, along_overlap):
        # side_hit: perpendicular distance px; along_overlap: bool
        if side_hit is None:
            return None
        return side_hit <= tol and along_overlap

    src_hit = _edge_line_hit(rec.get("sourceEdgeHitPx"),
                             rec.get("sourceAlongOverlap"))
    tgt_hit = _edge_line_hit(rec.get("targetEdgeHitPx"),
                             rec.get("targetAlongOverlap"))
    bad_edge = (src_hit is False) or (tgt_hit is False)

    if asset_kind == "svg":
        tip_gap = rec.get("tipEdgeHitPx")
        tip_on_seg = rec.get("tipAlongSegment")
        has_ah = rec.get("hasArrowhead")
        if has_ah and tip_gap is not None:
            if tip_gap > tol or not tip_on_seg:
                return V_TIP_MISS, False
            if bad_edge:
                return V_EDGE_GAP, False
            return V_PASS, True
        # No detected arrowhead -> require bbox to reach edge lines.
        if bad_edge:
            return V_EDGE_GAP, False
        return V_PASS, True

    if asset_kind == "png":
        if bad_edge:
            return V_EDGE_GAP, False
        return V_DEGRADED_BBOX, False

    return V_UNVERIFIABLE, False


def summarize(records, tol, allow_degraded_bbox=False):
    """Build the machine-readable summary block.

    allow_degraded_bbox=True removes V_DEGRADED_BBOX from strictFail (explicit
    relaxed mode). Default False: any non-tip-verified result blocks strict.
    """
    buckets = {}
    tip_verified = 0
    for r in records:
        v, ok = classify(r, tol)
        r["verdict"] = v
        r["tipVerified"] = ok
        buckets[v] = buckets.get(v, 0) + 1
        if ok:
            tip_verified += 1
    fail_set = set(ALL_NON_PASS)
    if allow_degraded_bbox:
        fail_set.discard(V_DEGRADED_BBOX)
    return {
        "total": len(records),
        "byVerdict": buckets,
        "tipVerified": tip_verified,
        "strictFail": sum(buckets.get(v, 0) for v in fail_set),
        "allowDegradedBbox": allow_degraded_bbox,
        "tolerancePx": tol,
    }


# ---------------------------------------------------------------------------
# Browser measurement.
# ---------------------------------------------------------------------------

# JS runs inside the page. It resolves each connector, source, target, and
# measures: stage containment, asset kind (inline svg vs img), real SVG tip
# via getPointAtLength, bbox gaps. Generic: no colors, no ids, no text.
JS = r"""(spec) => {
  const tol = spec.tolerancePx == null ? 2 : spec.tolerancePx;
  const stageSel = spec.stageSelector;
  const out = [];

  // Parse a path's d="..." into a flat list of {x,y} points along M/L
  // commands (we only need polyline vertices for chevron detection; curves
  // are ignored for the apex test). Generic, no colors/ids.
  function parsePoints(d) {
    if (!d) return [];
    const tokens = d.match(/[MmLlHhVvCcSsQqTtAaZz]|-?\d*\.?\d+(?:e[-+]?\d+)?/gi) || [];
    const pts = [];
    let cur = {x: 0, y: 0};
    let i = 0;
    let cmd = null;
    while (i < tokens.length) {
      const t = tokens[i];
      if (/^[MmLlHhVvCcSsQqTtAaZz]$/.test(t)) { cmd = t; i++; continue; }
      const nums = [];
      while (i < tokens.length && /^-?\d/.test(tokens[i])) {
        nums.push(parseFloat(tokens[i])); i++;
      }
      if (cmd === 'M' || cmd === 'm') {
        for (let k = 0; k + 1 < nums.length; k += 2) {
          cur = cmd === 'M' ? {x: nums[k], y: nums[k+1]}
                            : {x: cur.x + nums[k], y: cur.y + nums[k+1]};
          pts.push({x: cur.x, y: cur.y});
        }
        cmd = cmd === 'M' ? 'L' : 'l';
      } else if (cmd === 'L' || cmd === 'l') {
        for (let k = 0; k + 1 < nums.length; k += 2) {
          cur = cmd === 'L' ? {x: nums[k], y: nums[k+1]}
                            : {x: cur.x + nums[k], y: cur.y + nums[k+1]};
          pts.push({x: cur.x, y: cur.y});
        }
      } else if (cmd === 'H' || cmd === 'h') {
        for (const nx of nums) {
          cur = cmd === 'H' ? {x: nx, y: cur.y} : {x: cur.x + nx, y: cur.y};
          pts.push({x: cur.x, y: cur.y});
        }
      } else if (cmd === 'V' || cmd === 'v') {
        for (const ny of nums) {
          cur = cmd === 'V' ? {x: cur.x, y: ny} : {x: cur.x, y: cur.y + ny};
          pts.push({x: cur.x, y: cur.y});
        }
      } else {
        // curves/arcs: skip (we only need polyline vertices for chevron)
        break;
      }
    }
    return pts;
  }

  // Filled-triangle arrowhead apex. The historical bug: a filled triangle
  // was recognised as hasArrowhead but no branch measured its apex, so the
  // auditor fell back to main.getPointAtLength(end) -- the line body end at
  // the triangle's base, ~10-27px short of the real tip. Here we recover the
  // triangle corners and pick the corner furthest ALONG the outgoing tangent
  // of the main path near that end. Generic: no colors, no ids.
  function filledCorners(node) {
    const tag = node.tagName.toLowerCase();
    if (tag === 'polygon' || tag === 'polyline') {
      return Array.from(node.points || []).map(p => ({x: p.x, y: p.y}));
    }
    // closed/open triangle path: sample densely and cluster direction changes.
    let L = 0; try { L = node.getTotalLength(); } catch (e) { return []; }
    if (L <= 0) return [];
    const N = 96, pts = [];
    for (let i = 0; i < N; i++) {
      const p = node.getPointAtLength(L * i / N);
      pts.push({x: p.x, y: p.y});
    }
    const corners = [];
    for (let i = 0; i < N; i++) {
      const a = pts[(i - 3 + N) % N], b = pts[i], c = pts[(i + 3) % N];
      const v1x = b.x - a.x, v1y = b.y - a.y;
      const v2x = c.x - b.x, v2y = c.y - b.y;
      const l1 = Math.hypot(v1x, v1y), l2 = Math.hypot(v2x, v2y);
      if (l1 < 1e-3 || l2 < 1e-3) continue;
      let cos = (v1x * v2x + v1y * v2y) / (l1 * l2);
      cos = Math.max(-1, Math.min(1, cos));
      const ang = Math.acos(cos);
      if (ang > 0.6) corners.push(b);  // sharp corner of the triangle
    }
    // cluster nearby corner samples
    const out = [];
    for (const c of corners) {
      const merged = out.find(o => Math.hypot(o.x - c.x, o.y - c.y) < 6);
      if (merged) {
        merged.x = (merged.x + c.x) / 2; merged.y = (merged.y + c.y) / 2;
      } else out.push({x: c.x, y: c.y});
    }
    return out;
  }
  function filledApexLocal(fillNode, main, atEnd) {
    const corners = filledCorners(fillNode);
    if (!corners.length) return null;
    let dir = {x: 1, y: 0};
    if (main) {
      let L = 0; try { L = main.getTotalLength(); } catch (e) { L = 0; }
      if (L > 0.5) {
        const e = main.getPointAtLength(atEnd ? L : 0);
        const s = main.getPointAtLength(atEnd ? Math.max(0, L - 2)
                                             : Math.min(2, L));
        const dx = e.x - s.x, dy = e.y - s.y;
        const n = Math.hypot(dx, dy);
        if (n > 1e-6) dir = {x: dx / n, y: dy / n};
      }
    }
    // apex = corner furthest along the outgoing travel direction
    let best = null, bestPr = -Infinity;
    for (const c of corners) {
      const pr = c.x * dir.x + c.y * dir.y;
      if (pr > bestPr) { bestPr = pr; best = c; }
    }
    return best;
  }

  // Edge-line contract. An edge is a line segment. We measure the PERPENDICULAR
  // distance from a point/bbox to the edge's infinite line, and separately
  // whether the point/bbox overlaps the card's span along the edge. Off-
  // midpoint is NOT a defect.
  function edgeLineGeom(box, edge, isTarget) {
    // face: which bbox face touches this edge.
    // gapSign: +1 -> connector near face should be >= line (leaves source
    //   going right/down, or arrives target from left/above); -1 -> near face
    //   should be <= line (leaves source going left/up, or arrives target
    //   from right/below). Overshoot into the card is allowed; only a real
    //   open gap counts as failure.
    if (edge === 'right')
      return {axis: 'x', line: box.right, along0: box.y, along1: box.bottom,
              face: isTarget ? 'right' : 'left',
              gapSign: isTarget ? +1 : +1};
    if (edge === 'left')
      return {axis: 'x', line: box.left, along0: box.y, along1: box.bottom,
              face: isTarget ? 'right' : 'right',
              gapSign: isTarget ? +1 : -1};
    if (edge === 'bottom')
      return {axis: 'y', line: box.bottom, along0: box.x, along1: box.right,
              face: isTarget ? 'bottom' : 'top',
              gapSign: isTarget ? +1 : +1};
    if (edge === 'top')
      return {axis: 'y', line: box.top, along0: box.x, along1: box.right,
              face: isTarget ? 'bottom' : 'bottom',
              gapSign: isTarget ? +1 : -1};
    return {axis: 'x', line: (box.left+box.right)/2, along0: box.y,
            along1: box.bottom, face: 'left', gapSign: +1};
  }
  function perpDistToEdge(pt, g) {
    return g.axis === 'x' ? Math.abs(pt.x - g.line) : Math.abs(pt.y - g.line);
  }
  function alongCoord(pt, g) { return g.axis === 'x' ? pt.y : pt.x; }
  function overlapAlong(bbox, g, slack) {
    // does bbox's span along the edge overlap the card's span [along0,along1]?
    const b0 = g.axis === 'x' ? bbox.y : bbox.x;
    const b1 = g.axis === 'x' ? (bbox.y + bbox.height) : (bbox.x + bbox.width);
    return Math.max(b0, g.along0 - slack) <= Math.min(b1, g.along1 + slack);
  }
  function pointOnSegment(pt, g, slack) {
    const v = alongCoord(pt, g);
    return v >= g.along0 - slack && v <= g.along1 + slack;
  }

  // stageSelector may be a list of candidates (built HTML uses id="stage",
  // generic fixtures use class="stage"); take the first that exists.
  const stageCands = Array.isArray(stageSel) ? stageSel : [stageSel];
  const stage = stageCands.map(s => document.querySelector(s))
                          .find(Boolean) || null;

  for (const c of (spec.connectors || [])) {
    const rec = {
      id: c.id, selector: c.selector,
      sourceEdge: c.sourceEdge, targetEdge: c.targetEdge,
      arrowheadAt: c.arrowheadAt || 'target',
    };
    const els = document.querySelectorAll(c.selector);
    const el = els[0] || null;
    const src = c.sourceSelector ? document.querySelector(c.sourceSelector) : null;
    const tgt = c.targetSelector ? document.querySelector(c.targetSelector) : null;

    if (!el) { rec.status = 'missing-asset'; out.push(rec); continue; }
    if (els.length > 1) {
      // Hard failure: the same selector resolved to more than one rendered
      // element (e.g. old PNG and rebuilt SVG both present). querySelector
      // would silently test only the first and the other asset would still
      // render. This blocks strict and is NEVER auto-repaired.
      rec.status = 'duplicate-asset';
      rec.reason = `selector matched ${els.length} rendered elements`;
      out.push(rec); continue;
    }
    if (!src || !tgt) {
      rec.status = 'unverifiable';
      rec.reason = 'source/target selector missing or unmatched';
      out.push(rec); continue;
    }

    // 1. stage containment contract: the connector must sit inside the same
    //    element that carries the fit/scale transform.
    const inStage = stage && stage.contains(el);
    rec.stageSelector = stageSel;
    rec.inStage = !!inStage;
    if (!inStage) {
      rec.status = 'stage-outside';
      rec.reason = `connector ${c.selector} is not inside ${stageSel}`;
      out.push(rec); continue;
    }

    const tag = el.tagName.toLowerCase();
    const cs = getComputedStyle(el);
    const bbox = el.getBoundingClientRect();
    const zeroBox = bbox.width < 0.5 && bbox.height < 0.5;

    if (tag === 'img') {
      rec.assetKind = 'png';
      if (cs.display === 'none' || cs.visibility === 'hidden' || zeroBox) {
        rec.status = 'invisible'; out.push(rec); continue;
      }
    } else if (tag === 'svg' || (el.closest && el.closest('svg'))) {
      rec.assetKind = 'svg';
      const svgRoot = tag === 'svg' ? el : el.closest('svg');
      // Rebuild layout places every connector inside ONE stage-level <svg> as
      // <g id="conn-x">. Scope all geometry queries to the resolved element
      // (the group), not the whole svg, so per-connector measurement stays
      // independent (no group averaging, no cross-contamination).
      const scope = (tag === 'svg') ? svgRoot : el;
      const strokedPaths = Array.from(scope.querySelectorAll('path'))
        .filter(p => {
          // exclude paths inside <marker>/<defs> (their vertices are marker
          // glyph geometry, not connector chevrons)
          if (p.closest('marker') || p.closest('defs')) return false;
          const s = getComputedStyle(p);
          return s.display !== 'none' && s.stroke && s.stroke !== 'none' &&
                 parseFloat(s.strokeWidth || '0') > 0;
        });
      rec.pathCount = strokedPaths.length;

      // main body = longest stroked path whose two ENDS are distinct (the
      // connector dot's stroked circle can be longer than the S body but its
      // start==end -- never treat it as the line body).
      let main = null, mainLen = -1;
      for (const p of strokedPaths) {
        let L = 0; try { L = p.getTotalLength(); } catch (e) { L = 0; }
        if (L <= mainLen) continue;
        const a = p.getPointAtLength(0), b = p.getPointAtLength(L);
        if (Math.hypot(a.x - b.x, a.y - b.y) < 0.2 * L) continue;
        mainLen = L; main = p;
      }
      rec.mainPathLength = +mainLen.toFixed(2);

      // Chevron detection: a SHORT stroked 3-point polyline (M p0 L p1 L p2)
      // with a non-flat angle at p1. The apex p1 is the arrow tip. This
      // covers Figma's stroked chevron arrowheads (no marker, no fill).
      let chevron = null;
      for (const p of strokedPaths) {
        if (p === main) continue;
        const pts = parsePoints(p.getAttribute('d') || '');
        if (pts.length !== 3) continue;
        const [a, b, c2] = pts;
        // non-flat angle: b is the apex
        const ang = Math.hypot(a.x - b.x, a.y - b.y);
        const bng = Math.hypot(b.x - c2.x, b.y - c2.y);
        const base = Math.hypot(a.x - c2.x, a.y - c2.y);
        if (ang < 2 || bng < 2) continue;
        // apex b must be farther from the line a-c than the stroke width
        const h = Math.abs((c2.y-a.y)*b.x - (c2.x-a.x)*b.y + c2.x*a.y - c2.y*a.x) / base;
        if (h < 4) continue;  // nearly collinear -> stub, not chevron
        // chevron length must be small vs main body (barb-to-barb span)
        if (base > mainLen * 0.5) continue;
        chevron = {path: p, apexLocal: b, barbs: [a, c2]};
        break;
      }
      rec.chevronDetected = !!chevron;

      // marker detection (legacy)
      let hasAh = !!chevron;
      let tipKind = chevron ? 'chevron' : null;
      if (!chevron && main) {
        const ms = getComputedStyle(main);
        if (ms.markerEnd && ms.markerEnd !== 'none') {
          hasAh = true; tipKind = 'marker-end';
        } else if (ms.markerStart && ms.markerStart !== 'none') {
          hasAh = true; tipKind = 'marker-start';
        }
      }
      let filledNode = null;
      if (!hasAh) {
        // filled triangle arrowhead: record the node so we can compute its
        // REAL apex (corner furthest along the outgoing tangent) instead of
        // mistaking the line body end (triangle base) for the tip.
        const fills = Array.from(scope.querySelectorAll('path,polygon'))
          .filter(n => {
            const s = getComputedStyle(n);
            return s.fill && s.fill !== 'none' &&
                   (s.stroke === 'none' ||
                    parseFloat(s.strokeWidth || '0') === 0);
          });
        if (fills.length > 0) {
          hasAh = true; tipKind = 'filled'; filledNode = fills[0];
        }
      }
      // Visible dots (source anchor). Faded dots (opacity < 0.05) are
      // decorative: they must neither reach an edge on the connector's
      // behalf nor create a false open gap via the outer bbox.
      const dots = Array.from(scope.querySelectorAll('circle,ellipse'))
        .filter(n => {
          const s = getComputedStyle(n);
          if (s.display === 'none' || s.visibility === 'hidden') return false;
          const op = parseFloat(s.opacity || '1');
          if (op < 0.05) return false;
          return (s.fill && s.fill !== 'none') ||
                 (s.stroke && s.stroke !== 'none');
        });
      rec.hasArrowhead = hasAh;
      rec.tipKind = tipKind;

      const sw = main ? parseFloat(getComputedStyle(main).strokeWidth || '0') : 0;
      rec.computedStrokeWidthPx = sw;
      if (zeroBox || cs.display === 'none' ||
          (strokedPaths.length === 0 && !hasAh)) {
        rec.status = 'invisible';
        rec.reason = strokedPaths.length === 0 ? 'no stroked path' : 'zero-size';
        out.push(rec); continue;
      }

      // Resolve tip in SVG user units, then map to viewport via CTM.
      const atEnd = (rec.arrowheadAt !== 'source');
      let tipLocal = null;
      if (chevron) {
        tipLocal = chevron.apexLocal;
      } else if (main && tipKind && tipKind.startsWith('marker')) {
        // marker ref point == path endpoint
        tipLocal = main.getPointAtLength(atEnd ? mainLen : 0);
      } else if (tipKind === 'filled' && filledNode) {
        tipLocal = filledApexLocal(filledNode, main, atEnd);
      } else if (main) {
        // body-end as fallback (no arrowhead)
        tipLocal = main.getPointAtLength(atEnd ? mainLen : 0);
      }
      if (tipLocal) {
        const ctm = svgRoot.getScreenCTM();
        let vpt;
        if (ctm) {
          vpt = new DOMPoint(tipLocal.x, tipLocal.y).matrixTransform(ctm);
        } else {
          const sr = svgRoot.getBoundingClientRect();
          vpt = {x: sr.left + tipLocal.x, y: sr.top + tipLocal.y};
          rec.ctmFallback = true;
        }
        rec.tipViewport = {x: +vpt.x.toFixed(2), y: +vpt.y.toFixed(2)};
      }

      // Drawn-content union: edge reach must be judged from the actual
      // rendered stroke/arrow/dot geometry, NOT the outer SVG CSS box. A
      // stretched viewBox (designBox vs renderAABB mismatch) inflates the
      // outer box with empty padding and produces FAKE edge-gap readings that
      // must not trigger a repair. Faded dots are already excluded above.
      const u = {left: Infinity, top: Infinity,
                 right: -Infinity, bottom: -Infinity};
      let anyDrawn = false;
      const includeRect = r => {
        if (r.width < 0.5 && r.height < 0.5) return;
        u.left = Math.min(u.left, r.x); u.top = Math.min(u.top, r.y);
        u.right = Math.max(u.right, r.x + r.width);
        u.bottom = Math.max(u.bottom, r.y + r.height);
        anyDrawn = true;
      };
      // Line body + arrowhead define edge reach. The source dot ring is
      // drawn centered ON the edge (it deliberately crosses it), so it must
      // NOT extend the measured bbox or it fabricates a constant open gap.
      strokedPaths.forEach(p => includeRect(p.getBoundingClientRect()));
      if (filledNode) includeRect(filledNode.getBoundingClientRect());
      if (anyDrawn) {
        // DOMRect-like: downstream reads left/right/top/bottom too.
        rec.drawnRect = {x: u.left, y: u.top,
                         width: u.right - u.left, height: u.bottom - u.top,
                         left: u.left, right: u.right,
                         top: u.top, bottom: u.bottom};
        rec.drawnBounds = true;
      }
    } else {
      rec.assetKind = 'unknown';
    }

    // 4. Edge-line geometry (viewport). Drawn-content bbox reaches edge line
    // + overlaps the card span along the edge.
    const R = rec.drawnRect || el.getBoundingClientRect();
    const S = src.getBoundingClientRect();
    const T = tgt.getBoundingClientRect();
    rec.box = {x: R.x, y: R.y, w: R.width, h: R.height};
    const gSrc = edgeLineGeom(S, c.sourceEdge, false);
    const gTgt = edgeLineGeom(T, c.targetEdge, true);
    function facePos(bbox, g) {
      if (g.axis === 'x')
        return g.face === 'left' ? bbox.left : bbox.right;
      return g.face === 'top' ? bbox.top : bbox.bottom;
    }
    function bboxToEdge(bbox, g) {
      // Open gap = distance from edge line to nearest bbox extent on the
      // perpendicular axis. If the line passes through (or touches) the bbox,
      // gap=0 (over-shoot into the card is allowed per edge-line contract).
      let gap;
      if (g.axis === 'x') {
        if (g.line < bbox.left) gap = bbox.left - g.line;
        else if (g.line > bbox.right) gap = g.line - bbox.right;
        else gap = 0;
      } else {
        if (g.line < bbox.top) gap = bbox.top - g.line;
        else if (g.line > bbox.bottom) gap = g.line - bbox.bottom;
        else gap = 0;
      }
      const ov = overlapAlong(bbox, g, tol);
      return {hit: gap, overlap: ov};
    }
    const sHit = bboxToEdge(R, gSrc);
    const tHit = bboxToEdge(R, gTgt);
    rec.sourceEdgeHitPx = +sHit.hit.toFixed(2);
    rec.sourceAlongOverlap = sHit.overlap;
    rec.targetEdgeHitPx = +tHit.hit.toFixed(2);
    rec.targetAlongOverlap = tHit.overlap;

    // Tip vs target edge line (open gap) + on-segment.
    if (rec.tipViewport) {
      const g = (rec.arrowheadAt === 'source') ? gSrc : gTgt;
      const tip = rec.tipViewport;
      const fp = g.axis === 'x' ? tip.x : tip.y;
      const openGap = g.gapSign > 0 ? Math.max(0, g.line - fp)
                                    : Math.max(0, fp - g.line);
      rec.tipEdgeHitPx = +openGap.toFixed(2);
      rec.tipAlongSegment = pointOnSegment(tip, g, tol);
      rec.tipReferenceEdge = rec.arrowheadAt === 'source'
        ? rec.sourceEdge : rec.targetEdge;
    }

    rec.status = 'measured';
    out.push(rec);
  }
  return {connectors: out, tolerancePx: tol};
}
"""

# Stage-level probe: runs even when connectors=[] so an empty board still
# reports whether the stage element exists and carries a fit/scale transform.
STAGE_PROBE_JS = r"""(sel) => {
  const el = document.querySelector(sel);
  if (!el) return {stageSelector: sel, exists: false};
  const r = el.getBoundingClientRect();
  const cs = getComputedStyle(el);
  return {
    stageSelector: sel,
    exists: true,
    rect: {x: r.x, y: r.y, w: r.width, h: r.height},
    transform: cs.transform || cs.webkitTransform || 'none',
    display: cs.display,
    visibility: cs.visibility,
  };
};
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", required=True)
    ap.add_argument("--spec", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--stage-selector", default=None,
                    help="stage container selector. Default tries '#stage' "
                         "then '.stage' (built HTML emits id=stage; generic "
                         "fixtures use .stage class).")
    ap.add_argument("--tolerance", type=float, default=2.0)
    ap.add_argument("--strict", action="store_true",
                    help="non-zero exit on any verdict that is NOT a "
                         "tip-verified SVG pass (degraded-bbox / edge-gap / "
                         "tip-miss / stage-outside / invisible / missing-asset "
                         "/ unverifiable / duplicate-asset all fail). This is "
                         "the default strict gate; use --allow-degraded-bbox "
                         "to explicitly permit PNG bbox-only results.")
    ap.add_argument("--allow-degraded-bbox", action="store_true",
                    help="relaxed mode: do NOT fail strict when the only "
                         "non-pass results are degraded-bbox (PNG-only) "
                         "connectors. Other hard failures still block.")
    ap.add_argument("--chrome-channel", default="chrome")
    args = ap.parse_args()

    spec = json.loads(pathlib.Path(args.spec).read_text(encoding="utf-8"))
    # Built HTML uses <div id="stage">; generic fixtures use .stage. When the
    # caller does not pin a selector, probe both instead of defaulting to a
    # class that the real build never emits (would falsely report every
    # connector as stage-outside).
    spec["stageSelector"] = (args.stage_selector or ["#stage", ".stage"])
    spec["tolerancePx"] = args.tolerance

    problems = validate_spec(spec)
    if problems:
        print("spec problems:", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        # Structural problems mean we cannot trust the run; write an empty
        # audit and exit non-zero even without --strict.
        out_obj = {"specProblems": problems, "connectors": [],
                  "summary": {"total": 0, "byVerdict": {}, "tipVerified": 0,
                              "strictFail": len(problems),
                              "tolerancePx": args.tolerance}}
        pathlib.Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(args.out).write_text(json.dumps(out_obj, ensure_ascii=False,
                                                     indent=2), encoding="utf-8")
        sys.exit(3)

    # Playwright is imported lazily so the pure-python classify/summarize
    # helpers are importable in tests without a browser.
    from playwright.sync_api import sync_playwright  # noqa: E402

    W = spec.get("width", 1600)
    H = spec.get("height", 1200)
    with sync_playwright() as p:
        b = p.chromium.launch(channel=args.chrome_channel, headless=True,
                              args=["--force-device-scale-factor=1",
                                    "--hide-scrollbars"])
        page = b.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
        page.goto("file://" + urllib.parse.quote(
            str(pathlib.Path(args.html).resolve())),
            wait_until="networkidle", timeout=60000)
        page.evaluate("() => document.fonts && document.fonts.ready")
        measured = page.evaluate(JS, spec)
        stage_info = page.evaluate(
            r"""(sels) => {
              for (const s of (Array.isArray(sels) ? sels : [sels])) {
                const el = document.querySelector(s);
                if (el) {
                  const r = el.getBoundingClientRect();
                  const cs = getComputedStyle(el);
                  return {stageSelector: s, exists: true,
                          rect: {x: r.x, y: r.y, w: r.width, h: r.height},
                          transform: cs.transform || cs.webkitTransform || 'none',
                          display: cs.display, visibility: cs.visibility};
                }
              }
              return {exists: false};
            }""", spec["stageSelector"])
        b.close()

    records = measured.get("connectors", [])
    summary = summarize(records, args.tolerance,
                        allow_degraded_bbox=args.allow_degraded_bbox)
    # Empty connector list is a legitimate "board has no connectors" state.
    # It does NOT count as a strict failure: nothing to verify = nothing to
    # block on. The stage probe still confirms the stage container exists.
    if summary["total"] == 0:
        summary["empty"] = True
        summary["emptyReason"] = "no connectors listed in spec; aligns with " \
                                 "reconstruct empty-manifest contract"
        summary["strictFail"] = 0

    out_obj = {
        "html": str(pathlib.Path(args.html).resolve()),
        "stageSelector": args.stage_selector,
        "stage": stage_info,
        "connectors": records,
        "summary": summary,
        "note": ("Per-connector verdict; PNG bbox results are degraded-bbox "
                 "and never tip-verified; missing source/target is "
                 "unverifiable; stage containment and visibility are "
                 "required for SVG tip audits; an empty connectors list is "
                 "accepted as a no-connector board."),
    }
    pathlib.Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(args.out).write_text(
        json.dumps(out_obj, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({"connectors": summary["total"],
                      "byVerdict": summary["byVerdict"],
                      "tipVerified": summary["tipVerified"]},
                     ensure_ascii=False))
    if args.strict and summary["strictFail"] > 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
