#!/usr/bin/env python3
"""Synthetic regressions for the connector audit module.

No artboard ids, no user node ids, no real text, no brand colors, no fixed
compensation, no artboard dimensions are baked in. The fixtures build generic
shapes (horizontal SVG connector, vertical SVG connector, PNG-only connector,
SVG-outside-stage, invisible, missing source/target) and exercise the
classifier + summary logic. When Playwright is available, we also smoke-test
the full browser measurement against a tiny generic HTML page.

Run:  python3 tests/test_connectors.py
Exits non-zero on first failure.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import audit_connectors as ac  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok  " if cond else " FAIL ") + name +
          (("  -- " + str(detail)) if detail and not cond else ""))


# ---------------------------------------------------------------------------
# Helpers: build a measured record the way the browser JS would.
# ---------------------------------------------------------------------------

def _svg_record(**over):
    base = {
        "status": "measured",
        "assetKind": "svg",
        "inStage": True,
        "hasArrowhead": True,
        "mainPathLength": 120.0,
        "computedStrokeWidthPx": 2.5,
        "sourceEdgeHitPx": 0.0,
        "sourceAlongOverlap": True,
        "targetEdgeHitPx": 0.0,
        "targetAlongOverlap": True,
        "tipEdgeHitPx": 0.0,
        "tipAlongSegment": True,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# 1. classify(): horizontal / vertical SVG passes tip verification
# ---------------------------------------------------------------------------

def test_svg_pass():
    print("[1] SVG connector tip verification")
    r = ac.classify(_svg_record(tipGapPx=0.5), 2.0)
    check("horizontal SVG tip -> pass", r[0] == ac.V_PASS and r[1] is True, r)
    # vertical uses the same rule (tipGap to top/bottom edge midpoint)
    r = ac.classify(_svg_record(tipGapPx=1.8), 2.0)
    check("vertical SVG tip within tol -> pass", r[0] == ac.V_PASS, r)
    r = ac.classify(_svg_record(tipEdgeHitPx=3.5), 2.0)
    check("tip beyond tol -> tip-miss", r[0] == ac.V_TIP_MISS and r[1] is False, r)
    # bbox gap failure even when tip is close
    r = ac.classify(_svg_record(tipEdgeHitPx=0.5, sourceEdgeHitPx=5.0), 2.0)
    check("bbox gap failure -> edge-gap", r[0] == ac.V_EDGE_GAP, r)


# ---------------------------------------------------------------------------
# 2. PNG bbox-only must NOT be reported as tip-verified
# ---------------------------------------------------------------------------

def test_png_bbox():
    print("[2] PNG fallback (degraded-bbox, never tip)")
    r = ac.classify({
        "status": "measured", "assetKind": "png", "inStage": True,
        "sourceEdgeHitPx": 0.0, "sourceAlongOverlap": True,
        "targetEdgeHitPx": 0.5, "targetAlongOverlap": True,
    }, 2.0)
    check("PNG bbox reaches edges -> degraded-bbox",
          r[0] == ac.V_DEGRADED_BBOX and r[1] is False, r)
    r = ac.classify({
        "status": "measured", "assetKind": "png", "inStage": True,
        "sourceEdgeHitPx": 0.0, "sourceAlongOverlap": True,
        "targetEdgeHitPx": 4.0, "targetAlongOverlap": True,
    }, 2.0)
    check("PNG bbox misses edge -> edge-gap", r[0] == ac.V_EDGE_GAP, r)


# ---------------------------------------------------------------------------
# 3. stage containment, visibility, missing asset, unverifiable
# ---------------------------------------------------------------------------

def test_stage_and_visibility():
    print("[3] stage / visibility / missing / unverifiable")
    r = ac.classify({"status": "stage-outside"}, 2.0)
    check("stage-outside", r[0] == ac.V_STAGE_OUTSIDE, r)
    r = ac.classify({"status": "invisible"}, 2.0)
    check("invisible", r[0] == ac.V_INVISIBLE, r)
    r = ac.classify({"status": "missing-asset"}, 2.0)
    check("missing-asset", r[0] == ac.V_MISSING_ASSET, r)
    r = ac.classify({"status": "unverifiable"}, 2.0)
    check("unverifiable (missing src/tgt)",
          r[0] == ac.V_UNVERIFIABLE and r[1] is False, r)


# ---------------------------------------------------------------------------
# 4. summarize() counts per-connector; no averaging; strict exit
# ---------------------------------------------------------------------------

def test_summary():
    print("[4] summary bucket + strictFail")
    records = [
        _svg_record(tipGapPx=0.2),                              # pass
        _svg_record(tipGapPx=0.1),                              # pass
        {"status": "measured", "assetKind": "png",
         "sourceGapPx": 0, "targetGapPx": 0},                   # degraded-bbox
        {"status": "unverifiable"},                              # unverifiable
        {"status": "missing-asset"},                             # missing
    ]
    s = ac.summarize(records, 2.0)
    check("total=5", s["total"] == 5, s)
    check("pass count=2", s["byVerdict"].get(ac.V_PASS) == 2, s)
    check("degraded-bbox counted separately",
          s["byVerdict"].get(ac.V_DEGRADED_BBOX) == 1, s)
    check("tipVerified=2 (only SVG pass)", s["tipVerified"] == 2, s)
    # strictFail = pass以外全部: 2 SVG pass 之外的 3 条
    # (degraded-bbox + unverifiable + missing-asset) 都算 hard fail。
    check("strictFail=3 (degraded-bbox now also blocks strict)",
          s["strictFail"] == 3, s)
    # relaxed mode: allow_degraded_bbox removes degraded-bbox from strictFail.
    s2 = ac.summarize(records, 2.0, allow_degraded_bbox=True)
    check("relaxed: strictFail back to 2 (unverifiable + missing only)",
          s2["strictFail"] == 2, s2)
    check("relaxed flag echoed in summary",
          s2["allowDegradedBbox"] is True, s2)


# ---------------------------------------------------------------------------
# 5. validate_spec(): missing source/target / bad edge names flagged
# ---------------------------------------------------------------------------

def test_spec_validation():
    print("[5] spec validation")
    problems = ac.validate_spec({"connectors": [
        {"id": "a", "selector": "#a", "sourceSelector": "#s",
         "sourceEdge": "right", "targetSelector": "#t", "targetEdge": "left"},
        {"id": "b", "selector": "#b", "sourceEdge": "right",
         "targetEdge": "left"},  # missing selectors
        {"id": "c", "selector": "#c", "sourceSelector": "#s",
         "sourceEdge": "diagonal", "targetSelector": "#t",
         "targetEdge": "left"},  # bad edge
    ]})
    check("flags missing source/target for b",
         any("b" in p for p in problems), problems)
    check("flags bad edge for c",
         any("diagonal" in p for p in problems), problems)
    check("well-formed a clean",
         not any("'a'" in p or '#a' in p for p in problems),
         problems)


def test_empty_spec_accepted():
    print("[5b] empty connector spec accepted (aligns with empty-manifest)")
    problems = ac.validate_spec({"connectors": []})
    check("empty connectors list has no structural problems",
         problems == [], problems)
    problems2 = ac.validate_spec({})  # missing key entirely
    check("missing connectors key also accepted as empty",
         problems2 == [], problems2)
    s = ac.summarize([], 2.0)
    check("empty summary total=0", s["total"] == 0, s)
    check("empty summary strictFail=0 (nothing to verify)",
         s["strictFail"] == 0, s)


# ---------------------------------------------------------------------------
# 6. End-to-end browser smoke (only if Playwright is importable).
# ---------------------------------------------------------------------------

SMOKE_HTML = """<!doctype html><html><body>
<style>
  .stage { position: relative; width: 800px; height: 500px;
           transform: scale(0.5); transform-origin: 0 0; }
  .card { position: absolute; width: 60px; height: 40px;
          background: #ddd; border: 1px solid #888; }
</style>
<div class="stage">
  <div id="src-h" class="card" style="left:50px; top:100px;"></div>
  <div id="tgt-h" class="card" style="left:300px; top:100px;"></div>
  <svg id="conn-h" width="200" height="40"
       style="position:absolute; left:110px; top:100px;">
    <path d="M0,20 L190,20" stroke="#333" stroke-width="2.5"
          marker-end="url(#ah)"/>
    <defs><marker id="ah" markerWidth="10" markerHeight="10" refX="8"
                  refY="3" orient="auto">
      <path d="M0,0 L8,3 L0,6 Z" fill="#333"/>
    </marker></defs>
  </svg>
  <div id="src-v" class="card" style="left:500px; top:50px;"></div>
  <div id="tgt-v" class="card" style="left:500px; top:250px;"></div>
  <svg id="conn-v" width="40" height="160"
       style="position:absolute; left:510px; top:90px;">
    <path d="M20,0 L20,160" stroke="#333" stroke-width="2.5"
          marker-end="url(#ah2)"/>
    <defs><marker id="ah2" markerWidth="10" markerHeight="10" refX="3"
                  refY="8" orient="auto">
      <path d="M0,0 L3,8 L6,0 Z" fill="#333"/>
    </marker></defs>
  </svg>
  <div id="png-src" class="card" style="left:50px; top:250px;"></div>
  <div id="png-tgt" class="card" style="left:200px; top:250px;"></div>
  <img id="conn-png" style="position:absolute; left:110px; top:265px;
       width:90px; height:10px; background:#999;" />
  <!-- chevron arrowhead, off-midpoint tip: tip y=10 (not card mid y=20)
       but tip x reaches target left edge. -->
  <div id="ch-src" class="card" style="left:50px; top:350px;"></div>
  <div id="ch-tgt" class="card" style="left:300px; top:350px;"></div>
  <svg id="conn-ch" width="190" height="40"
       style="position:absolute; left:110px; top:350px;">
    <path d="M0,10 L190,10" stroke="#333" stroke-width="2.5"/>
    <path d="M180,2 L190,10 L180,18" stroke="#333" stroke-width="2.5"
          stroke-linejoin="round" fill="none"/>
  </svg>
</div>
<!-- svg outside stage -->
<svg id="conn-out" width="50" height="10"><path d="M0,5 L50,5"
     stroke="#333" stroke-width="2"/></svg>
</body></html>"""


def test_browser_smoke():
    print("[6] browser smoke (skipped if playwright unavailable)")
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"  skip: playwright not importable ({e})")
        return
    tmp = tempfile.mkdtemp(prefix="conn-smoke-")
    html = os.path.join(tmp, "page.html")
    with open(html, "w", encoding="utf-8") as fh:
        fh.write(SMOKE_HTML)
    spec = {
        "width": 900, "height": 600, "stageSelector": ".stage",
        "tolerancePx": 4,
        "connectors": [
            {"id": "h", "selector": "#conn-h",
             "sourceSelector": "#src-h", "sourceEdge": "right",
             "targetSelector": "#tgt-h", "targetEdge": "left",
             "arrowheadAt": "target"},
            {"id": "v", "selector": "#conn-v",
             "sourceSelector": "#src-v", "sourceEdge": "bottom",
             "targetSelector": "#tgt-v", "targetEdge": "top",
             "arrowheadAt": "target"},
            {"id": "png", "selector": "#conn-png",
             "sourceSelector": "#png-src", "sourceEdge": "right",
             "targetSelector": "#png-tgt", "targetEdge": "left",
             "arrowheadAt": "target"},
            {"id": "ch", "selector": "#conn-ch",
             "sourceSelector": "#ch-src", "sourceEdge": "right",
             "targetSelector": "#ch-tgt", "targetEdge": "left",
             "arrowheadAt": "target"},
            {"id": "out", "selector": "#conn-out",
             "sourceSelector": "#src-h", "sourceEdge": "right",
             "targetSelector": "#tgt-h", "targetEdge": "left",
             "arrowheadAt": "target"},
        ],
    }
    spec_path = os.path.join(tmp, "spec.json")
    with open(spec_path, "w", encoding="utf-8") as fh:
        json.dump(spec, fh)

    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        page = b.new_page(viewport={"width": 900, "height": 600})
        page.goto("file://" + html)
        page.wait_for_timeout(100)
        measured = page.evaluate(ac.JS, spec)
        b.close()
    by_id = {r["id"]: r for r in measured["connectors"]}
    check("h: assetKind=svg", by_id["h"].get("assetKind") == "svg", by_id["h"])
    check("h: inStage", by_id["h"].get("inStage") is True, by_id["h"])
    check("h: tip measured", "tipViewport" in by_id["h"], by_id["h"])
    check("v: assetKind=svg", by_id["v"].get("assetKind") == "svg", by_id["v"])
    check("v: inStage", by_id["v"].get("inStage") is True, by_id["v"])
    check("png: assetKind=png",
          by_id["png"].get("assetKind") == "png", by_id["png"])
    check("out: stage-outside",
          by_id["out"].get("status") == "stage-outside", by_id["out"])
    # classify the measured records
    for rid, rec in by_id.items():
        v, tip = ac.classify(rec, 4.0)
        rec["verdict"] = v
        rec["tipVerified"] = tip
    check("h verdict pass",
          by_id["h"].get("verdict") == ac.V_PASS, by_id["h"].get("verdict"))
    check("v verdict pass",
          by_id["v"].get("verdict") == ac.V_PASS, by_id["v"].get("verdict"))
    check("png verdict degraded-bbox (NOT pass)",
          by_id["png"].get("verdict") == ac.V_DEGRADED_BBOX,
          by_id["png"].get("verdict"))
    check("out verdict stage-outside",
          by_id["out"].get("verdict") == ac.V_STAGE_OUTSIDE,
          by_id["out"].get("verdict"))
    # chevron: stroked chevron arrowhead detected, tip at off-midpoint y but
    # on target edge line -> pass with tipVerified.
    check("ch: chevronDetected",
          by_id["ch"].get("chevronDetected") is True, by_id["ch"])
    check("ch: tipKind=chevron",
          by_id["ch"].get("tipKind") == "chevron", by_id["ch"])
    check("ch: tipViewport measured",
          "tipViewport" in by_id["ch"], by_id["ch"])
    check("ch verdict pass (off-midpoint tip accepted)",
          by_id["ch"].get("verdict") == ac.V_PASS and
          by_id["ch"].get("tipVerified") is True,
          by_id["ch"])


def test_cli_empty_spec():
    print("[6b] CLI regression: empty connector spec exits 0 under --strict")
    import subprocess
    empty_html = "<!doctype html><body><div class='stage'></div></body></html>"
    tmp = tempfile.mkdtemp(prefix="conn-empty-")
    html = os.path.join(tmp, "page.html")
    spec = os.path.join(tmp, "spec.json")
    out = os.path.join(tmp, "audit.json")
    with open(html, "w", encoding="utf-8") as fh:
        fh.write(empty_html)
    with open(spec, "w", encoding="utf-8") as fh:
        json.dump({"width": 800, "height": 600, "connectors": []}, fh)
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    script = os.path.join(root, "scripts", "audit_connectors.py")
    proc = subprocess.run(
        ["python3", script, "--html", html, "--spec", spec,
         "--out", out, "--stage-selector", ".stage", "--strict"],
        capture_output=True, text=True)
    check("empty spec CLI exit code 0 (not 3)", proc.returncode == 0,
          f"rc={proc.returncode} stderr={proc.stderr}")
    if os.path.exists(out):
        data = json.loads(open(out, encoding="utf-8").read())
        check("stage probe present in output",
              data.get("stage", {}).get("exists") is True, data.get("stage"))
        check("summary.empty flagged",
              data.get("summary", {}).get("empty") is True,
              data.get("summary"))
        check("summary.strictFail=0",
              data.get("summary", {}).get("strictFail") == 0,
              data.get("summary"))


def test_builder_connector_inline():
    print("[6a] builder inline-SVG connector replacement")
    import build_fixed_html as bfh
    tmp = tempfile.mkdtemp(prefix="conn-build-")
    # connector SVG file (anonymous ids)
    svg_path = os.path.join(tmp, "conn-a.svg")
    with open(svg_path, "w", encoding="utf-8") as fh:
        fh.write('<svg width="100" height="40" viewBox="0 0 100 40" fill="none">'
                 '<path d="M0,20 L90,20" stroke="#333" stroke-width="2"/>'
                 '<path d="M80,12 L90,20 L80,28" stroke="#333" stroke-width="2"/>'
                 '</svg>')
    # text-bearing SVG (must be refused)
    bad_svg = os.path.join(tmp, "conn-b.svg")
    with open(bad_svg, "w", encoding="utf-8") as fh:
        fh.write('<svg width="10" height="10"><text x="0" y="0">hi</text></svg>')
    manifest = {
        "artboardId": "ab-test",
        "artboardName": "test",
        "artboardSize": {"width": 400, "height": 200},
        "rootFills": [],
        "assets": [
            {"nodeId": "conn-a", "pngPlacement": {"x": 50, "y": 60, "width": 100, "height": 40},
             "exportSize": {"width": 100, "height": 40}, "asset": "conn-a.png",
             "visible": True, "zIndexChain": [0]},
            {"nodeId": "conn-b", "pngPlacement": {"x": 200, "y": 60, "width": 10, "height": 10},
             "exportSize": {"width": 10, "height": 10}, "asset": "conn-b.png",
             "visible": True, "zIndexChain": [1]},
            {"nodeId": "plain", "pngPlacement": {"x": 50, "y": 150, "width": 30, "height": 20},
             "exportSize": {"width": 30, "height": 20}, "asset": "plain.png",
             "visible": True, "zIndexChain": [2]},
        ],
        "containerVisuals": [],
    }
    text_man = {"texts": []}
    conn_map = {
        "conn-a": {"svgPath": svg_path, "designBox": {}},
        "conn-b": {"svgPath": bad_svg, "designBox": {}},
    }
    doc, report = bfh.build_html(manifest, text_man, tmp, tmp, conn_map=conn_map)
    check("conn-a replaced by inline svg",
          '<svg class="layer" data-n="conn-a"' in doc, doc[:500])
    check("conn-a has preserveAspectRatio=none",
          'preserveAspectRatio="none"' in doc)
    check("conn-a CSS box preserved (left:50 top:60)",
          "left:50.0000px;top:60.0000px;width:100.0000px;height:40.0000px" in doc)
    check("conn-a inner path carried into inline svg",
          'M0,20 L90,20' in doc)
    check("no double <img> for conn-a",
          '<img class="layer" data-n="conn-a"' not in doc)
    check("conn-b refused (text) -> PNG fallback",
          '<img class="layer" data-n="conn-b"' in doc)
    check("plain node still <img>",
          '<img class="layer" data-n="plain"' in doc)
    check("report counts replacement",
          report["connectorSvgReplaced"] == 1, report)
    check("report counts png fallback",
          report["connectorPngFallback"] >= 1, report)


# ---------------------------------------------------------------------------
# 7. duplicate-asset hard failure (never auto-repaired)
# ---------------------------------------------------------------------------

def test_duplicate_asset():
    print("[7] duplicate-asset hard failure")
    v, tip = ac.classify({"status": "duplicate-asset"}, 2.0)
    check("duplicate-asset verdict", v == ac.V_DUPLICATE_ASSET, v)
    check("duplicate-asset not tip-verified", tip is False, tip)
    s = ac.summarize([{"status": "duplicate-asset"}], 2.0)
    check("duplicate-asset blocks strict", s["strictFail"] == 1, s)


# ---------------------------------------------------------------------------
# 8. deterministic plan derivation + pure rebuild geometry
# ---------------------------------------------------------------------------

def _layout():
    return {"nodes": [
        {"nodeId": "A", "absoluteBox": {"x": 0, "y": 100, "width": 60, "height": 40}},
        {"nodeId": "B", "absoluteBox": {"x": 400, "y": 100, "width": 60, "height": 40}},
        {"nodeId": "C", "absoluteBox": {"x": 0, "y": 300, "width": 60, "height": 40}},
    ], "connectorPlans": [
        {"edgeId": "h", "fromNodeId": "A", "toNodeId": "B", "arrowDirection": "to"},
        {"edgeId": "v", "fromNodeId": "C", "toNodeId": "A", "arrowDirection": "to"},
        {"edgeId": "self", "fromNodeId": "A", "toNodeId": "A"},
        {"edgeId": "missing"},
    ]}


def test_plan_derivation():
    print("[8] deterministic connector-plan derivation")
    import repair_connectors as rc
    plan = rc.derive_connector_plan(_layout())
    by = {e["id"]: e for e in plan["connectors"]}
    check("h: right->left derived from layout",
          by["h"]["sourceEdge"] == "right" and by["h"]["targetEdge"] == "left",
          by["h"])
    check("h: certain", by["h"]["uncertain"] is False, by["h"])
    check("v: top->bottom derived",
          by["v"]["sourceEdge"] == "top" and by["v"]["targetEdge"] == "bottom",
          by["v"])
    check("self-loop -> uncertain (not guessed)",
          by["self"]["uncertain"] is True, by["self"])
    check("missing endpoints -> uncertain",
          by["missing"]["uncertain"] is True, by["missing"])
    probs = rc.validate_plan(plan)
    check("uncertain entries block rebuild",
          any("uncertain" in p for p in probs), probs)


def test_pure_geometry():
    print("[8b] pure connector geometry (no browser)")
    import repair_connectors as rc
    e = {"id": "x", "sourceEdge": "right", "targetEdge": "left",
         "arrowheadAt": "target",
         "sourceBox": {"x": 0, "y": 100, "width": 60, "height": 40},
         "targetBox": {"x": 400, "y": 120, "width": 60, "height": 40}}
    g, p0, p2 = rc.connector_group(e, {"color": "rgb(1,2,3)",
                                         "strokeWidth": 2.5})
    check("dot at source edge midpoint",
          abs(p0[0] - 60) < 0.01 and abs(p0[1] - 120) < 0.01, p0)
    check("tip at target edge midpoint",
          abs(p2[0] - 400) < 0.01 and abs(p2[1] - 140) < 0.01, p2)
    check("dot ring drawn at source anchor",
          '<circle cx="60.00" cy="120.00"' in g, g)
    check("arrow apex == tip", "400.00,140.00" in g, g)
    check("S-curve (not a plain straight line)",
          "Q" in g and "V" in g, g)
    ev = {"id": "y", "sourceEdge": "bottom", "targetEdge": "top",
          "arrowheadAt": "target",
          "sourceBox": {"x": 100, "y": 0, "width": 60, "height": 40},
          "targetBox": {"x": 100, "y": 300, "width": 60, "height": 40}}
    gv, q0, q2 = rc.connector_group(ev, {"color": "rgb(1,2,3)",
                                         "strokeWidth": 2.5})
    check("vertical: dot at source bottom mid",
          abs(q0[0] - 130) < .01 and abs(q0[1] - 40) < .01, q0)
    check("vertical: tip at target top mid",
          abs(q2[0] - 130) < .01 and abs(q2[1] - 300) < .01, q2)
    check("vertical straight when aligned", "V300.00" in gv, gv)


# ---------------------------------------------------------------------------
# 9. Browser smoke 2: filled apex / viewBox-padding pseudo-gap / duplicate /
#    shared trunk.
# ---------------------------------------------------------------------------

SMOKE2_HTML = """<!doctype html><html><body>
<style>
  .stage { position: relative; width: 800px; height: 500px;
           transform: scale(0.5); transform-origin: 0 0; }
  .card { position: absolute; width: 60px; height: 40px;
          background: #ddd; border: 1px solid #888; }
</style>
<div class="stage">
  <div id="f-src" class="card" style="left:50px; top:60px;"></div>
  <div id="f-tgt" class="card" style="left:400px; top:60px;"></div>
  <svg id="conn-f" width="290" height="30"
       style="position:absolute; left:110px; top:65px;">
    <path d="M0,15 L260,15" stroke="#333" stroke-width="2.5"/>
    <polygon points="290,15 260,8 260,22" fill="#333"/>
  </svg>
  <div id="p-src" class="card" style="left:50px; top:160px;"></div>
  <div id="p-tgt" class="card" style="left:400px; top:160px;"></div>
  <svg id="conn-p" width="320" height="30" viewBox="-30 0 380 30"
       style="position:absolute; left:80px; top:160px;"
       preserveAspectRatio="none">
    <path d="M5.6,15 L350,15" stroke="#333" stroke-width="2.5"/>
  </svg>
  <svg id="dup" width="50" height="10"
       style="position:absolute; left:110px; top:260px;">
    <path d="M0,5 L50,5" stroke="#333" stroke-width="2"/>
  </svg>
  <div id="dup" class="card" style="left:50px; top:260px;"></div>
  <div id="s-src" class="card" style="left:50px; top:330px;"></div>
  <div id="s-t1" class="card" style="left:400px; top:335px;"></div>
  <div id="s-t2" class="card" style="left:400px; top:350px;"></div>
  <div id="s-t3" class="card" style="left:400px; top:365px;"></div>
  <svg id="st1" width="290" height="10"
       style="position:absolute; left:110px; top:330px;">
    <path d="M0,5 L290,5" stroke="#333" stroke-width="2.5"
          marker-end="url(#ah1)"/>
    <defs><marker id="ah1" markerWidth="10" markerHeight="10" refX="8"
                  refY="3" orient="auto">
      <path d="M0,0 L8,3 L0,6 Z" fill="#333"/>
    </marker></defs>
  </svg>
  <svg id="st2" width="290" height="10"
       style="position:absolute; left:110px; top:345px;">
    <path d="M0,5 L290,5" stroke="#333" stroke-width="2.5"
          marker-end="url(#ah2)"/>
    <defs><marker id="ah2" markerWidth="10" markerHeight="10" refX="8"
                  refY="3" orient="auto">
      <path d="M0,0 L8,3 L0,6 Z" fill="#333"/>
    </marker></defs>
  </svg>
  <svg id="st3" width="290" height="10"
       style="position:absolute; left:110px; top:360px;">
    <path d="M0,5 L290,5" stroke="#333" stroke-width="2.5"
          marker-end="url(#ah3)"/>
    <defs><marker id="ah3" markerWidth="10" markerHeight="10" refX="8"
                  refY="3" orient="auto">
      <path d="M0,0 L8,3 L0,6 Z" fill="#333"/>
    </marker></defs>
  </svg>
</div></body></html>"""


def test_browser_smoke2():
    print("[9] browser smoke: filled apex / viewBox padding / duplicate / trunk")
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"  skip: playwright not importable ({e})")
        return
    tmp = tempfile.mkdtemp(prefix="conn-smoke2-")
    html = os.path.join(tmp, "page.html")
    with open(html, "w", encoding="utf-8") as fh:
        fh.write(SMOKE2_HTML)
    spec = {"width": 900, "height": 600, "stageSelector": ".stage",
            "tolerancePx": 4, "connectors": [
                {"id": "f", "selector": "#conn-f", "sourceSelector": "#f-src",
                 "sourceEdge": "right", "targetSelector": "#f-tgt",
                 "targetEdge": "left", "arrowheadAt": "target"},
                {"id": "p", "selector": "#conn-p", "sourceSelector": "#p-src",
                 "sourceEdge": "right", "targetSelector": "#p-tgt",
                 "targetEdge": "left", "arrowheadAt": "none"},
                {"id": "dup", "selector": "#dup", "sourceSelector": "#f-src",
                 "sourceEdge": "right", "targetSelector": "#f-tgt",
                 "targetEdge": "left", "arrowheadAt": "target"},
                {"id": "st1", "selector": "#st1", "sourceSelector": "#s-src",
                 "sourceEdge": "right", "targetSelector": "#s-t1",
                 "targetEdge": "left", "arrowheadAt": "target"},
                {"id": "st2", "selector": "#st2", "sourceSelector": "#s-src",
                 "sourceEdge": "right", "targetSelector": "#s-t2",
                 "targetEdge": "left", "arrowheadAt": "target"},
                {"id": "st3", "selector": "#st3", "sourceSelector": "#s-src",
                 "sourceEdge": "right", "targetSelector": "#s-t3",
                 "targetEdge": "left", "arrowheadAt": "target"},
            ]}
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        page = b.new_page(viewport={"width": 900, "height": 600})
        page.goto("file://" + html)
        page.wait_for_timeout(100)
        measured = page.evaluate(ac.JS, spec)
        b.close()
    by_id = {r["id"]: r for r in measured["connectors"]}
    # filled triangle: real apex (290,15) measured, not body end (260,15)
    check("f: tipKind=filled",
          by_id["f"].get("tipKind") == "filled", by_id["f"])
    check("f: tip at target edge (not body end)",
          by_id["f"].get("tipEdgeHitPx", 99) <= 4, by_id["f"])
    vf, _ = ac.classify(by_id["f"], 4.0)
    check("f verdict pass with tipVerified",
          vf == ac.V_PASS and by_id["f"].get("tipKind") == "filled", vf)
    # viewBox padding: outer box starts 30px short, drawn path reaches edge
    check("p: outer padding no longer fakes edge-gap",
          by_id["p"].get("sourceEdgeHitPx", 99) <= 4, by_id["p"])
    vp, _ = ac.classify(by_id["p"], 4.0)
    check("p verdict pass", vp == ac.V_PASS, vp)
    # duplicate
    check("dup: duplicate-asset hard failure",
          by_id["dup"].get("status") == "duplicate-asset", by_id["dup"])
    # shared trunk: three independent connectors all pass
    for k in ("st1", "st2", "st3"):
        v, tip = ac.classify(by_id[k], 4.0)
        check(f"trunk {k} pass tipVerified",
              v == ac.V_PASS and tip is True, (k, v, by_id[k]))


# ---------------------------------------------------------------------------
# 10. repair auto-fallback / rollback
# ---------------------------------------------------------------------------

def _broken_html():
    return """<!doctype html><html><head><style>
#stage{position:relative;width:800px;height:500px;overflow:hidden;background:#fff}
.card{position:absolute;width:60px;height:40px;background:#ddd;
      border:1px solid #888}
</style></head><body><div id="stage">
<div class="card" data-n="A" style="left:50px;top:100px"></div>
<div class="card" data-n="B" style="left:400px;top:120px"></div>
<img id="e1" data-n="e1" style="position:absolute;left:120px;top:110px;
     width:60px;height:8px;background:#333"/>
</div></body></html>"""


def test_repair_e2e():
    print("[10] repair auto-fallback end-to-end")
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception as e:
        print(f"  skip: playwright not importable ({e})")
        return
    import repair_connectors as rc
    tmp = tempfile.mkdtemp(prefix="conn-repair-")
    p_in = os.path.join(tmp, "page.html")
    with open(p_in, "w", encoding="utf-8") as fh:
        fh.write(_broken_html())
    audit = {"stage": {"rect": {"w": 800, "h": 500}}, "connectors": [
        {"id": "e1", "selector": "#e1", "status": "measured",
         "assetKind": "png", "sourceEdgeHitPx": 0,
         "sourceAlongOverlap": True, "targetEdgeHitPx": 0,
         "targetAlongOverlap": True}]}
    plan = {"schemaVersion": "1.0", "connectors": [{
        "id": "e1", "sourceNodeId": "A", "targetNodeId": "B",
        "sourceEdge": "right", "targetEdge": "left", "arrowheadAt": "target",
        "tolerancePx": 2, "uncertain": False, "color": "rgb(51,51,51)"}]}
    p_out = os.path.join(tmp, "repaired.html")
    report, out = rc.repair(p_in, plan, audit, p_out,
                            stage_selector="#stage")
    check("rebuiltCount=1", report["rebuiltCount"] == 1, report)
    check("no rollback after reverify pass",
          report["rolledBack"] is False, report)
    check("iterations=1", report["iterations"] == 1, report)
    doc = open(out, encoding="utf-8").read()
    check("old PNG connector removed (no double render)",
          '<img id="e1"' not in doc, doc[:300])
    check("one rebuilt svg inside stage",
          'data-n="connectors-rebuilt"' in doc and doc.count("<g id=") == 1,
          doc)
    check("cards and text layer untouched",
          'data-n="A"' in doc and 'data-n="B"' in doc, doc)


def test_repair_blocked_uncertain():
    print("[10b] uncertain plan blocks rebuild (no guessing)")
    import repair_connectors as rc
    tmp = tempfile.mkdtemp(prefix="conn-blocked-")
    p_in = os.path.join(tmp, "page.html")
    with open(p_in, "w", encoding="utf-8") as fh:
        fh.write(_broken_html())
    plan = {"schemaVersion": "1.0", "connectors": [{
        "id": "e1", "sourceNodeId": "A", "targetNodeId": "B",
        "sourceEdge": "right", "targetEdge": "left", "arrowheadAt": "target",
        "uncertain": True, "reason": "missing toNodeId"}]}
    audit = {"connectors": []}
    report, out = rc.repair(p_in, plan, audit,
                            os.path.join(tmp, "out.html"),
                            stage_selector="#stage")
    check("blocked reported", bool(report["blocked"]), report)
    check("no output html when blocked", out is None, out)
    check("skippedUnverifiable lists the entry",
          "e1" in report["skippedUnverifiable"], report)


def main():
    test_svg_pass()
    test_png_bbox()
    test_stage_and_visibility()
    test_summary()
    test_spec_validation()
    test_empty_spec_accepted()
    test_builder_connector_inline()
    test_browser_smoke()
    test_cli_empty_spec()
    test_duplicate_asset()
    test_plan_derivation()
    test_pure_geometry()
    test_browser_smoke2()
    test_repair_e2e()
    test_repair_blocked_uncertain()
    print()
    print(f"passed={len(PASS)} failed={len(FAIL)}")
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
