#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Iteration-4 generic gates (RC1 rootFills / RC2 rotation geometry / RC3 runs).

Zero third-party framework; run:  python3 tests/test_iteration4_gates.py

Synthetic fixtures ONLY -- no live Figma, no frame-02 node ids, no project
names, no pixel compensation. Every case proves that changing an INPUT field
propagates deterministically to the compiled plan and the built HTML, and that
unknown values HARD-FAIL instead of being impersonated by a 16px/transparent
default.
"""
import json
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import compile_design as cd          # noqa: E402
import build_fixed_html as bfh      # noqa: E402
import design_text as dt            # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS.append(name)) if cond else FAIL.append((name, detail))


def _px(v):
    import re
    m = re.match(r"^\s*([0-9.]+)\s*px", str(v or ""))
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# synthetic raw bundle: artboard + one rotated image asset + one text node
# ---------------------------------------------------------------------------
def make_bundle(root_fills=[{"type": "SOLID", "color": {"r": 0, "g": 0, "b": 0}}],
                rotated=True, text_fs=40.0, box_w=200.0):
    rot = 45.0 if rotated else 0.0
    if rotated:
        ib = {"x": 81.7, "y": 81.7, "width": 113.1, "height": 113.1}
        mat = [0.7071, 0.7071, -0.7071, 0.7071, 100.0, 100.0]
        pp = {"x": 81.7, "y": 81.7, "width": 113.1, "height": 113.1}
        es = {"width": 113, "height": 113}
    else:
        ib = {"x": 100.0, "y": 100.0, "width": 80.0, "height": 80.0}
        mat = [1, 0, 0, 1, 100.0, 100.0]
        pp = {"x": 100.0, "y": 100.0, "width": 80.0, "height": 80.0}
        es = {"width": 80, "height": 80}
    assets = [{
        "nodeId": "IMG1", "type": "RECTANGLE", "name": "tile",
        "parentId": None, "visible": True, "effectiveVisibility": True,
        "opacity": 1, "rotation": rot,
        "local": {"x": 100.0, "y": 100.0, "width": 80.0, "height": 80.0},
        "designBox": {"x": 100.0, "y": 100.0, "width": 80.0, "height": 80.0},
        "intrinsicRenderBounds": ib,
        "relativeToArtboard": dict(ib),
        "absoluteTransform": mat,
        "pngPlacement": pp,
        "exportSize": es,
        "asset": "IMG1.png", "fills": [], "disposition": "rendered-asset",
        "zIndexChain": [1],
    }]
    texts = [{
        "nodeId": "TXT1", "type": "TEXT", "name": "title", "parentId": None,
        "visible": True, "characters": "Hello", "textAutoResize": "NONE",
        "local": {"x": 20.0, "y": 20.0, "width": box_w, "height": 60.0},
        "relativeToArtboard": {"x": 20.0, "y": 20.0, "width": box_w, "height": 60.0},
        "fontName": {"family": "PingFang SC", "style": "Regular"},
        "fontSize": text_fs,
        "lineHeight": {"unit": "AUTO"},
        "letterSpacing": {"unit": "PIXELS", "value": 0},
        "fills": [{"type": "SOLID", "color": {"r": 1, "g": 1, "b": 1}}],
        "textAlignHorizontal": "LEFT", "textAlignVertical": "TOP",
        "zIndexChain": [2],
    }]
    manifest = {
        "artboardId": "AB1", "artboardName": "synthetic",
        "artboardSize": {"width": 400.0, "height": 300.0},
        "rootFills": root_fills,
        "assets": assets, "containerVisuals": [],
        "contentLedger": [{"nodeId": "IMG1"}, {"nodeId": "TXT1"}],
    }
    return {"manifest": manifest, "text_manifest": {"texts": texts}}


# ---------------------------------------------------------------------------
# T1: rootFills propagates end-to-end; missing -> hard fail; [] -> explicit none
# ---------------------------------------------------------------------------
def test_rootfills():
    print("[T1] rootFills chain + classification gate")
    ir, rels, plan, _ = cd.compile_bundle(make_bundle())
    check("ir.source carries rootFills", ir["source"].get("rootFills") is not None)
    check("plan.artboard carries rootFills", plan["artboard"].get("rootFills") is not None)

    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "IMG1.png")
        open(path, "wb").write(b"\x89PNG\r\n\x1a\n")
        doc, rep = bfh.build_from_plan(plan, td, td)
        seg = doc[doc.find("#stage{"):doc.find(".layer")]
        check("solid root fill -> non-transparent stage",
              "background:rgba(0,0,0" in seg, seg)

    # empty array -> explicit no fill, transparent + disclosure
    p2 = json.loads(json.dumps(plan))
    p2["artboard"]["rootFills"] = []
    with tempfile.TemporaryDirectory() as td:
        open(os.path.join(td, "IMG1.png"), "wb").write(b"\x89PNG\r\n\x1a\n")
        doc2, rep2 = bfh.build_from_plan(p2, td, td)
        check("empty rootFills -> transparent stage",
              "rgba(0, 0, 0, 0)" in doc2 or "transparent" in doc2)
        check("explicit-no-fill disclosed",
              any(d.get("kind") == "stage-explicit-no-fill" for d in rep2["disclosures"]))

    # unknown (key missing) -> HARD FAIL, not silent transparent
    p3 = json.loads(json.dumps(plan))
    p3["artboard"].pop("rootFills", None)
    try:
        with tempfile.TemporaryDirectory() as td:
            bfh.build_from_plan(p3, td, td)
        check("missing rootFills blocks build", False, "no exception raised")
    except ValueError as e:
        check("missing rootFills blocks build", "rootFills" in str(e))


# ---------------------------------------------------------------------------
# T2: rotated PNG placed flat at pngPlacement/exportSize; no matrix, no stretch
# ---------------------------------------------------------------------------
def test_rotated_png():
    print("[T2] rotation-baked PNG: flat placement, no CSS matrix, no stretch")
    ir, rels, plan, _ = cd.compile_bundle(make_bundle(rotated=True))
    img = next(n for n in plan["nodes"] if n["nodeId"] == "IMG1")
    # renderAABB must be the export truth (intrinsic/relative), NOT a corner reproj
    check("renderAABB uses export truth (~81.7,113)",
          abs(img["renderAABB"]["x"] - 81.7) < 1.0 and abs(img["renderAABB"]["w"] - 113.1) < 1.0,
          img["renderAABB"])
    with tempfile.TemporaryDirectory() as td:
        open(os.path.join(td, "IMG1.png"), "wb").write(b"\x89PNG\r\n\x1a\n")
        doc, _ = bfh.build_from_plan(plan, td, td)
        check("no CSS matrix wrapper on asset", "matrix(" not in doc)
        check("PNG placed at pngPlacement 81.7", "left:81.7000px" in doc and "top:81.7000px" in doc)
        check("PNG kept ~113px (not stretched to 80)",
              "width:113" in doc and "width:80.0000px" not in doc.split("<img")[1][:120])


# ---------------------------------------------------------------------------
# T3: mixed fontSize needs per-run numeric runs; missing -> hard fail
# ---------------------------------------------------------------------------
def test_mixed_runs():
    print("[T3] mixed fontSize consumes runs; missing numeric run -> hard fail")
    ir, rels, plan, _ = cd.compile_bundle(make_bundle())
    tp = next(t for t in plan["textPlans"] if t["nodeId"] == "TXT1")
    # numeric parent size passes through
    out = dt.build_text_css(bfh._wire_text_ir(tp))
    check("numeric fontSize emitted on cell", "font-size:40.000px" in out["css"], out["css"])

    # mixed parent + per-run numeric runs -> spans carry each size
    mixed = dict(tp)
    mixed["fontSize"] = "mixed"
    mixed["runs"] = [
        {"characters": "AB", "fontSize": 30.0,
         "fontName": {"family": "D", "style": "Semibold"},
         "fills": [{"type": "SOLID", "color": {"r": 1, "g": 1, "b": 1}}]},
        {"characters": "C\u2028D", "fontSize": 52.0,
         "fontName": {"family": "D", "style": "Regular"},
         "fills": [{"type": "SOLID", "color": {"r": 1, "g": 1, "b": 1}}]},
    ]
    out2 = dt.build_text_css(bfh._wire_text_ir(mixed))
    html_runs, _ = bfh._runs_html_from_runs(out2["runs"])
    check("mixed runs emit per-run px spans",
          "font-size:30.000px" in html_runs and "font-size:52.000px" in html_runs, html_runs)
    check("mixed runs emit per-run weights (no parent Medium leak)",
          "font-weight:600" in html_runs and "font-weight:400" in html_runs, html_runs)
    check("Figma U+2028 becomes a real forced break",
          "<br>" in html_runs and "\u2028" not in html_runs, html_runs)
    justified_runs, _ = bfh._runs_html_from_runs(out2["runs"], justify_hard_lines=True)
    check("JUSTIFIED hard-break paragraph gets text-align-last:justify",
          "display:block;text-align-last:justify" in justified_runs
          and "display:block;text-align-last:auto" in justified_runs, justified_runs)

    # mixed parent + a run WITHOUT numeric fontSize -> HARD FAIL (no 16px)
    bad = dict(tp)
    bad["fontSize"] = "mixed"
    bad["runs"] = [{"characters": "AB", "fontSize": None, "fills": []}]
    try:
        dt.build_text_css(bfh._wire_text_ir(bad))
        check("missing run fontSize hard-fails", False, "no exception")
    except ValueError as e:
        check("missing run fontSize hard-fails", "fontSize" in str(e))


# ---------------------------------------------------------------------------
# T4: AUTO line-height without resolvedPx -> normal, never faked px
# ---------------------------------------------------------------------------
def test_auto_lineheight():
    print("[T4] AUTO line-height unresolved -> normal, no box-height fabrication")
    out = dt.build_text_css({"characters": "x", "textAutoResize": "NONE",
                             "local": {"width": 100}, "fontSize": 20,
                             "lineHeight": {"unit": "AUTO"}})
    check("unresolved AUTO -> normal", "line-height:normal;" in out["css"], out["css"])
    check("no fabricated 24/48px", "line-height:24px" not in out["css"]
          and "line-height:48px" not in out["css"])


# ---------------------------------------------------------------------------
# T5/T6/T7: changing input propagates (fontSize, box width, geometry)
# ---------------------------------------------------------------------------
def test_input_propagation():
    print("[T5-T7] changing fontSize / box width / rotation auto-reflects")
    _, _, p_small, ph_small = cd.compile_bundle(make_bundle(text_fs=24.0))
    _, _, p_big, ph_big = cd.compile_bundle(make_bundle(text_fs=60.0))
    t_small = next(t for t in p_small["textPlans"] if t["nodeId"] == "TXT1")
    t_big = next(t for t in p_big["textPlans"] if t["nodeId"] == "TXT1")
    check("fontSize change reflected in plan", t_small["fontSize"] == 24.0
          and t_big["fontSize"] == 60.0)
    cs = dt.build_text_css(bfh._wire_text_ir(t_small))["css"]
    cb = dt.build_text_css(bfh._wire_text_ir(t_big))["css"]
    check("fontSize change reflected in CSS", "font-size:24.000px" in cs
          and "font-size:60.000px" in cb)

    _, _, p_narrow, _ = cd.compile_bundle(make_bundle(box_w=120.0))
    _, _, p_wide, _ = cd.compile_bundle(make_bundle(box_w=360.0))
    w_narrow = next(t for t in p_narrow["textPlans"] if t["nodeId"] == "TXT1")["outerWidth"]
    w_wide = next(t for t in p_wide["textPlans"] if t["nodeId"] == "TXT1")["outerWidth"]
    check("box width change reflected in outerWidth",
          float(w_narrow) == 120.0 and float(w_wide) == 360.0, (w_narrow, w_wide))

    _, _, rot, _ = cd.compile_bundle(make_bundle(rotated=True))
    _, _, flat, _ = cd.compile_bundle(make_bundle(rotated=False))
    r_rot = next(n for n in rot["nodes"] if n["nodeId"] == "IMG1")["renderAABB"]
    r_flat = next(n for n in flat["nodes"] if n["nodeId"] == "IMG1")["renderAABB"]
    check("rotation changes render box (no double-translate off-canvas)",
          abs(r_rot["x"] - 81.7) < 1.0 and abs(r_flat["x"] - 100.0) < 1.0, (r_rot, r_flat))


# ---------------------------------------------------------------------------
# T8: hidden node skipped from DOM
# ---------------------------------------------------------------------------
def test_hidden():
    print("[T8] hidden text node skipped, not rendered")
    # Direct plan: builder must honour effectiveVisibility=False (compiler-side
    # hidden derivation is covered by test_visibility_compiler.py).
    plan = {
        "ruleVersion": "design-compiler/1.0.0", "planHash": "h",
        "artboard": {"artboardId": "a", "artboardName": "a",
                     "artboardSize": {"w": 400, "h": 300}, "rootFills": []},
        "nodes": [
            {"nodeId": "TXT1", "type": "TEXT", "effectiveVisibility": False,
             "effectiveMatrix": [1, 0, 0, 1, 20, 20],
             "renderAABB": {"x": 20, "y": 20, "w": 120, "h": 40}, "zIndexChain": [1]},
        ],
        "assets": [],
        "textPlans": [
            {"nodeId": "TXT1", "characters": "Hello", "fontSize": 40,
             "fontName": {"family": "D", "style": "Regular"},
             "lineHeight": {"unit": "AUTO"}, "outerWidth": 120, "mode": "fixed-box"},
        ],
        "connectorPlans": [], "relations": [], "unresolved": [],
    }
    with tempfile.TemporaryDirectory() as td:
        doc, rep = bfh.build_from_plan(plan, td, td)
        check("hidden text skipped",
              any(m["nodeId"] == "TXT1" and not m["rendered"] for m in rep["mappings"]))
        check("hidden characters absent from DOM", "Hello" not in doc)


# ---------------------------------------------------------------------------
# T9 (R1): audit_text hard-failure buckets really fire on synthetic DOM
# ---------------------------------------------------------------------------
def test_audit_buckets():
    print("[T9] audit_text fontSizeMismatch / transparentButSolid buckets fire")
    import audit_text as at
    design = {
        "T_OK": {"characters": "ok", "fontSize": 20,
                 "fontName": {"family": "D", "style": "Regular"},
                 "fills": [{"type": "SOLID", "color": {"r": 1, "g": 1, "b": 1}}],
                 "lineHeight": {"unit": "AUTO"}},
        "T_BADFS": {"characters": "x", "fontSize": 20,
                    "fontName": {"family": "D", "style": "Regular"},
                    "fills": [{"type": "SOLID", "color": {"r": 1, "g": 1, "b": 1}}]},
        "T_TRANS": {"characters": "x", "fontSize": 16,
                    "fontName": {"family": "D", "style": "Regular"},
                    "fills": [{"type": "SOLID", "color": {"r": 1, "g": 0, "b": 0}}]},
        # mixed node: expected runs 30 and 52
        "T_MIX_OK": {"characters": "AB\u2028", "fontSize": "mixed",
                     "fontName": {"family": "D", "style": "Regular"},
                     "segments": [
                         {"characters": "A", "fontSize": 30.0,
                          "fills": [{"type": "SOLID", "color": {"r": 1, "g": 1, "b": 1}}]},
                         {"characters": "B", "fontSize": 52.0,
                          "fills": [{"type": "SOLID", "color": {"r": 1, "g": 1, "b": 1}}]},
                         {"characters": "\u2028", "fontSize": 10.0,
                          "fills": [{"type": "SOLID", "color": {"r": 1, "g": 1, "b": 1}}]}]},
        "T_MIX_BAD": {"characters": "CD", "fontSize": "mixed",
                      "fontName": {"family": "D", "style": "Regular"},
                      "segments": [
                          {"characters": "C", "fontSize": 30.0,
                           "fills": [{"type": "SOLID", "color": {"r": 1, "g": 1, "b": 1}}]},
                          {"characters": "D", "fontSize": 52.0,
                           "fills": [{"type": "SOLID", "color": {"r": 1, "g": 1, "b": 1}}]}]},
    }
    def node(nid, dom_fs, color, run_spans=None):
        return {"nid": nid, "text": "x", "box": {"x": 0, "y": 0, "w": 100, "h": 20},
                "lines": [{"x": 0, "y": 0, "w": 100, "h": 20}], "lineCount": 1,
                "avgLineHeight": 20, "overflow": {"x": 0, "y": 0},
                "computed": {"fontSize": dom_fs, "whiteSpace": "pre-wrap",
                             "fontFamily": "D", "lineHeight": "normal"},
                "runSpans": run_spans or [],
                "hasTableWrapper": False, "color": color}
    result = {
        "nodes": [node("T_OK", "20px", "rgba(255, 255, 255, 1)"),
                  node("T_BADFS", "25px", "rgba(255, 255, 255, 1)"),
                  node("T_TRANS", "16px", "rgba(0, 0, 0, 0)"),
                  node("T_MIX_OK", "20px", "rgba(255, 255, 255, 1)",
                       [{"index": 0, "text": "A", "fontSize": "30px"},
                        {"index": 1, "text": "B", "fontSize": "52px"}]),
                  node("T_MIX_BAD", "20px", "rgba(255, 255, 255, 1)",
                       [{"index": 0, "text": "C", "fontSize": "30px"},
                        {"index": 1, "text": "D", "fontSize": "40px",
                         "fontWeight": "500", "fontStyle": "normal"}])],
        "colorAudit": {"suspects": [], "unknowns": []},
        "coverage": {"missingInDom": [], "extraInDom": []},
        "errors": [],
    }
    out = at.post_process(design, result, [], [])
    fs = out["hardFailures"]["fontSizeMismatch"]
    ts = out["hardFailures"]["transparentButSolid"]
    check("fontSizeMismatch catches 20vs25px",
          any(x["node"] == "T_BADFS" and x["deltaPx"] == 5.0 for x in fs), fs)
    check("fontSizeMismatch ignores 20vs20px",
          not any(x["node"] == "T_OK" for x in fs), fs)
    check("correct mixed runs (30/52) NOT falsely flagged",
          not any(x["node"] == "T_MIX_OK" for x in fs), fs)
    check("wrong mixed run (40 vs 52) IS flagged",
          any(x["node"] == "T_MIX_BAD" and x.get("deltaPx") == 12.0 for x in fs), fs)
    check("mixed run parent-weight leak IS flagged",
          any(x["node"] == "T_MIX_BAD" and x.get("property") == "font-weight"
              and x.get("expected") == 400 and x.get("actual") == 500
              for x in out["hardFailures"]["fontStyleMismatch"]),
          out["hardFailures"]["fontStyleMismatch"])
    check("transparentButSolid catches SOLID fill + transparent DOM",
          any(x["node"] == "T_TRANS" for x in ts), ts)
    check("hardFailureCount counts buckets (BADFS+MIX_BAD+WEIGHT+TRANS=4)",
          out["hardFailureCount"] == 4, out["hardFailureCount"])
    # D3: main() must not crash on the final summary print (post-RC3 NameError).
    import subprocess
    h = subprocess.run([sys.executable, str(SCRIPTS / "audit_text.py"), "--help"],
                        capture_output=True, text=True)
    check("audit_text main CLI imports/parses without NameError",
          h.returncode == 0 and "usage" in (h.stdout + h.stderr).lower(),
          (h.returncode, h.stderr[:200]))


# ---------------------------------------------------------------------------
# T10 (R2): nested parent-child accumulated matrix + built HTML width changes
# ---------------------------------------------------------------------------
def test_nested_matrix_and_html_width():
    print("[T10] nested rotated parent -> child accumulated box; box width -> HTML width")
    # Parent rotated 45deg at (200,200); child local rect (50,50,40,40).
    # Export carries the artboard-space render truth for the whole subtree PNG.
    assets = [{
        "nodeId": "CHILD", "type": "RECTANGLE", "name": "kid", "parentId": "PARENT",
        "visible": True, "effectiveVisibility": True, "opacity": 1, "rotation": 0.0,
        "local": {"x": 50.0, "y": 50.0, "width": 40.0, "height": 40.0},
        "designBox": {"x": 250.0, "y": 250.0, "width": 40.0, "height": 40.0},
        "intrinsicRenderBounds": {"x": 231.7, "y": 231.7, "width": 56.6, "height": 56.6},
        "relativeToArtboard": {"x": 231.7, "y": 231.7, "width": 56.6, "height": 56.6},
        "absoluteTransform": [0.7071, 0.7071, -0.7071, 0.7071, 250.0, 250.0],
        "pngPlacement": {"x": 231.7, "y": 231.7, "width": 56.6, "height": 56.6},
        "exportSize": {"width": 57, "height": 57},
        "asset": "CHILD.png", "fills": [], "disposition": "rendered-asset",
        "zIndexChain": [1],
    }]
    texts = []
    manifest = {
        "artboardId": "AB", "artboardName": "nested",
        "artboardSize": {"width": 500.0, "height": 500.0},
        "rootFills": [], "assets": assets, "containerVisuals": [],
        "contentLedger": [{"nodeId": "CHILD"}],
    }
    _, _, plan0, _ = cd.compile_bundle({"manifest": manifest, "text_manifest": {"texts": texts}})
    child = next(n for n in plan0["nodes"] if n["nodeId"] == "CHILD")
    # Render box must be the accumulated export truth (231.7), NOT a reprojection
    # of local (50,50) onto the parent matrix (which would double-translate).
    check("nested child render box uses accumulated export truth",
          abs(child["renderAABB"]["x"] - 231.7) < 1.0, child["renderAABB"])

    # Box width change MUST change the built HTML emitted width, not just plan.
    def build_with_width(w):
        b = make_bundle(box_w=float(w))
        _, _, pl, _ = cd.compile_bundle(b)
        with tempfile.TemporaryDirectory() as td:
            open(os.path.join(td, "IMG1.png"), "wb").write(b"\x89PNG\r\n\x1a\n")
            doc, _ = bfh.build_from_plan(pl, td, td)
        return doc
    doc_narrow = build_with_width(120)
    doc_wide = build_with_width(360)
    import re
    wn = re.search(r'class="tlayer"[^>]*style="([^"]+)"', doc_narrow)
    ww = re.search(r'class="tlayer"[^>]*style="([^"]+)"', doc_wide)
    check("narrow box emits width:120px in HTML", wn and "width:120px" in wn.group(1),
          wn.group(1) if wn else None)
    check("wide box emits width:360px in HTML", ww and "width:360px" in ww.group(1),
          ww.group(1) if ww else None)
    check("HTML width actually differs between narrow/wide", wn.group(1) != ww.group(1))


# ---------------------------------------------------------------------------
# T11 (R3): data_gate passes a clean export and blocks a broken one
# ---------------------------------------------------------------------------
def test_data_gate():
    print("[T11] data_gate passes clean export, blocks rootFills/run violations")
    import data_gate as dg
    good = {
        "manifest": {"artboardId": "a", "rootFills": []},
        "ir": {"source": {"rootFills": []}},
        "plan": {"artboard": {"rootFills": []},
                 "nodes": [{"nodeId": "N1", "disposition": "rendered-asset",
                            "renderAABB": {"x": 0, "y": 0, "w": 10, "h": 10}}],
                 "assets": [{"nodeId": "N1", "pngPlacement": {"x": 0, "y": 0, "w": 10},
                             "exportSize": {"width": 10, "height": 10}}],
                 "textPlans": [{"nodeId": "T1", "fontSize": 20, "runs": [],
                                "lineHeightSource": "authoritative"}]},
    }
    bad = json.loads(json.dumps(good))
    bad["manifest"].pop("rootFills")                       # G1 unknown
    bad["plan"]["textPlans"][0]["fontSize"] = "mixed"      # G2 mixed w/o runs
    bad["plan"]["textPlans"][0]["lineHeightSource"] = "resolved"  # fabricated

    with tempfile.TemporaryDirectory() as td:
        for name, blob in [("manifest.json", good["manifest"]),
                           ("design-ir.json", good["ir"]),
                           ("layout-plan.json", good["plan"])]:
            open(os.path.join(td, name), "w").write(json.dumps(blob))
        r = dg.run_gate(td)
        check("data_gate passes clean export", r["pass"], r["failures"])

        for name, blob in [("manifest.json", bad["manifest"]),
                           ("design-ir.json", bad["ir"]),
                           ("layout-plan.json", bad["plan"])]:
            open(os.path.join(td, name), "w").write(json.dumps(blob))
        r2 = dg.run_gate(td)
        gates_hit = {f["gate"] for f in r2["failures"]}
        check("data_gate blocks rootFills/mixed/fabricated",
              (not r2["pass"]) and {"G1", "G2"}.issubset(gates_hit),
              (r2["failures"], gates_hit))


def test_pdf_line_calibration():
    print("[T14] native PDF line plan -> deterministic DOM lines")
    raw = make_bundle()
    raw["text_calibration"] = {"nodes": {"TXT1": {
        "source": "figma-pdf-text-lines", "calibrationNodeId": "CAL1",
        "lines": ["He", "ll", "o"], "lineHeightPx": 35.0}}}
    ir, rels, plan, _ = cd.compile_bundle(raw)
    tp = plan["textPlans"][0]
    check("calibrated lines enter plan", tp["linePlan"]["lines"] == ["He", "ll", "o"])
    check("baseline step resolves AUTO", tp["lineHeightPx"] == 35.0 and
          tp["lineHeightSource"] == "figma-pdf-baseline-step")
    with tempfile.TemporaryDirectory() as td:
        open(os.path.join(td, "IMG1.png"), "wb").write(b"\x89PNG\r\n\x1a\n")
        doc, rep = bfh.build_from_plan(plan, td, td)
        cell = doc[doc.find('data-n="TXT1"'):]
        check("calibrated lines stay real DOM", ">He</span>" in cell and
              ">ll</span>" in cell and ">o</span>" in cell)
        check("calibrated lines forbid CSS rewrap", cell.count("white-space:pre") >= 3)
        check("measured line height reaches CSS", "line-height:35.000px" in cell)
    bad = make_bundle()
    bad["text_calibration"] = {"nodes": {"TXT1": {"lines": ["Hx"]}}}
    try:
        cd.compile_bundle(bad)
        check("calibration character mismatch blocks", False, "no exception")
    except ValueError as e:
        check("calibration character mismatch blocks", "does not match" in str(e))


def test_inherited_clip():
    print("[T15] ancestor clipsContent -> inherited text clip")
    raw = make_bundle()
    stage = {"x": 0.0, "y": 0.0, "width": 400.0, "height": 300.0}
    clip_box = {"x": 10.0, "y": 20.0, "width": 100.0, "height": 100.0}
    raw["manifest"]["containerVisuals"] = [{
        "nodeId": "P1", "parentId": None, "type": "FRAME", "name": "clip",
        "designBox": dict(clip_box), "renderAABB": dict(clip_box),
        "local": dict(clip_box), "absoluteTransform": [1, 0, 0, 1, 10.0, 20.0],
        "effectiveClipRect": dict(stage), "clipsContent": True,
        "fills": [], "strokes": [], "effects": [], "zIndexChain": [0]}]
    raw["manifest"]["contentLedger"].append({"nodeId": "P1"})
    raw["text_manifest"]["texts"][0]["parentId"] = "P1"
    for row in raw["manifest"]["contentLedger"]:
        if row.get("nodeId") == "TXT1":
            row["parentId"] = "P1"
    ir, rels, plan, _ = cd.compile_bundle(raw)
    tp = plan["textPlans"][0]
    check("text inherits clipping ancestor", tp["clipRect"] == {
        "x": 10.0, "y": 20.0, "w": 100.0, "h": 100.0})
    with tempfile.TemporaryDirectory() as td:
        open(os.path.join(td, "IMG1.png"), "wb").write(b"\x89PNG\r\n\x1a\n")
        doc, rep = bfh.build_from_plan(plan, td, td)
        cell = doc[doc.find('data-n="TXT1"'):]
        check("builder emits inherited text clip", "clip-path:inset(" in cell)


def main():
    test_rootfills()
    test_rotated_png()
    test_mixed_runs()
    test_auto_lineheight()
    test_input_propagation()
    test_hidden()
    test_audit_buckets()
    test_nested_matrix_and_html_width()
    test_data_gate()
    test_pdf_line_calibration()
    test_inherited_clip()
    print(f"\n=== RESULT ===\npassed: {len(PASS)}  failed: {len(FAIL)}")
    for n, d in FAIL:
        print(" FAIL:", n, "::", d)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
