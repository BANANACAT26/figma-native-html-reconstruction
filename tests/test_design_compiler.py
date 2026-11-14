#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic Design Compiler tests (iteration-3).

Zero third-party framework: run with  python3 tests/test_design_compiler.py

Synthetic fixtures only -- NO live Figma, NO project node ids / colors /
pixel constants. Every case drives scripts/compile_design.py through its
public library surface (extract_from_data -> normalize -> analyze -> plan)
and asserts the compiler auto-derives the plan from the input.
"""
import copy
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import compile_design as cd  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
    else:
        FAIL.append((name, detail))


# ---------------------------------------------------------------------------
# synthetic fixture builders
# ---------------------------------------------------------------------------
def R(x, y, w, h):
    return {"x": float(x), "y": float(y), "width": float(w), "height": float(h)}


def IDENT():
    return [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]


def _asset(nid, parent, box, *, kind="RECTANGLE", matrix=None, zc=None,
           visible=True, op=1.0, design_box=None):
    local = dict(box)
    db = dict(design_box) if design_box else dict(box)
    return {
        "nodeId": nid, "parentId": parent, "type": kind,
        "zIndexChain": zc or [], "visible": visible,
        "effectiveVisible": visible and op >= 0.01,
        "opacity": op, "effectiveOpacity": op,
        "local": local,
        "designBox": db,
        "renderAABB": dict(db),
        "absoluteTransform": matrix or IDENT(),
        "effectiveClipRect": {"x": 0.0, "y": 0.0, "width": 1000.0, "height": 800.0},
        "fills": [{"type": "SOLID", "visible": True, "color": {"r": 0.5, "g": 0.5, "b": 0.5}}],
    }


def _ledger(nid, parent, *, kind="RECTANGLE", zc=None, disp="rendered-asset",
            visible=True, op=1.0, reason=None):
    return {
        "nodeId": nid, "parentId": parent, "type": kind,
        "selfVisible": visible, "effectiveVisible": visible and op >= 0.01,
        "effectiveOpacity": op, "zIndexChain": zc or [],
        "disposition": disp, "reason": reason,
    }


def build_bundle():
    """A small generic board: stage > row(cards/icon/hidden) > title/rotated,
    plus a 180-flipped row (the green-card-class case) and two connectors."""
    assets = []
    ledger = []

    def add(nid, parent, box, **kw):
        asset_kw = {k: v for k, v in kw.items()
                    if k not in ("disp", "kind", "reason")}
        assets.append(_asset(nid, parent, box, **asset_kw))
        ledger.append(_ledger(nid, parent, kind=kw.get("kind", "RECTANGLE"),
                             zc=kw.get("zc"), disp=kw.get("disp", "rendered-asset"),
                             visible=kw.get("visible", True), op=kw.get("op", 1.0),
                             reason=kw.get("reason")))

    # root stage
    assets.append(_asset("stage", None, R(0, 0, 1000, 800), kind="FRAME", zc=[0]))
    ledger.append(_ledger("stage", None, kind="FRAME", zc=[0],
                         disp="rendered-stage"))

    # row + children
    add("row", "stage", R(0, 0, 600, 200), kind="FRAME", zc=[0, 0],
        disp="rendered-container")
    add("cardA", "row", R(20, 20, 100, 60), zc=[0, 0, 0])
    add("cardB", "row", R(200, 20, 100, 60), zc=[0, 0, 1])
    add("icon", "row", R(340, 30, 40, 40), zc=[0, 0, 2])
    add("hiddenChild", "row", R(400, 20, 50, 50), zc=[0, 0, 3],
        visible=False, disp="skipped-invisible", reason="self-invisible")

    # title text
    assets.append(_asset("title", "stage", R(0, 600, 200, 30), kind="TEXT",
                         zc=[0, 1]))
    ledger.append(_ledger("title", "stage", kind="TEXT", zc=[0, 1],
                          disp="rendered-text"))

    # rotated node (-90deg)
    add("rotated", "stage", R(500, 100, 80, 80), zc=[0, 2],
        matrix=[0.0, -1.0, 1.0, 0.0, 540.0, 140.0])

    # 180-flipped row with a bubble child (green-card-class offset case)
    assets.append(_asset("flippedRow", "stage", R(0, 400, 300, 100),
                         kind="FRAME", zc=[0, 3],
                         matrix=[-1.0, 0.0, 0.0, -1.0, 300.0, 500.0],
                         design_box=R(0, 400, 300, 100)))
    ledger.append(_ledger("flippedRow", "stage", kind="FRAME", zc=[0, 3],
                          disp="rendered-container"))
    add("bubble", "flippedRow", R(20, 10, 100, 40), zc=[0, 3, 0],
        matrix=[-1.0, 0.0, 0.0, -1.0, 120.0, 120.0],
        design_box=R(20, 10, 100, 40))

    texts = [{
        "nodeId": "title", "characters": "hello",
        "fontName": {"family": "Generic", "style": "Regular"},
        "fontSize": 14.0,
        "letterSpacing": {"unit": "PIXELS", "value": 0.0},
        "lineHeight": {"unit": "AUTO", "value": None},
        "textAlignHorizontal": "LEFT", "textAlignVertical": "TOP",
        "textAutoResize": "NONE",
        "local": R(0, 600, 200, 30),
        "relativeToArtboard": R(0, 600, 200, 30),
        "absoluteTransform": IDENT(),
    }]

    connectors = {"exported": [
        {"nodeId": "arrowH", "name": "h", "type": "CONNECTOR",
         "designBox": R(120, 50, 80, 0),
         "sourceNodeId": "cardA", "targetNodeId": "cardB",
         "arrowDirection": "to"},
        {"nodeId": "arrowV", "name": "v", "type": "CONNECTOR",
         "designBox": R(250, 80, 0, 120),
         "sourceNodeId": "cardB", "targetNodeId": "title",
         "arrowDirection": "to"},
        {"nodeId": "arrowOrphan", "name": "o", "type": "CONNECTOR",
         "designBox": R(0, 0, 10, 10)},  # no source/target
    ]}

    manifest = {
        "artboardId": "synthetic-board",
        "artboardSize": {"width": 1000, "height": 800},
        "assets": assets,
        "containerVisuals": [],
        "contentLedger": ledger,
    }
    text_manifest = {"artboardId": "synthetic-board", "texts": texts}
    return cd.extract_from_data(manifest, text_manifest, connectors)


def compile_once(bundle):
    return cd.compile_bundle(copy.deepcopy(bundle))


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------
def test_all_nodes_have_disposition():
    print("[1] every node incl. hidden has a disposition in IR")
    ir, rels, plan, ph = compile_once(build_bundle())
    ids = set(ir["nodes"].keys())
    for must in ["stage", "row", "cardA", "cardB", "icon", "hiddenChild",
                 "title", "rotated", "flippedRow", "bubble",
                 "arrowH", "arrowV", "arrowOrphan"]:
        check("node present: " + must, must in ids, sorted(ids))
    check("hidden node has disposition",
          ir["nodes"]["hiddenChild"]["disposition"] == "self-invisible")
    check("hidden node does not render a layer",
          next(l for l in plan["nodes"] if l["nodeId"] == "hiddenChild"
               )["renderLayer"] is False)
    check("ledger coverage == IR node count",
          rels["summary"]["nodeCount"] == len(ir["nodes"]))


def test_determinism():
    print("[2] same input twice -> identical bytes & hash")
    ir1, rels1, plan1, ph1 = compile_once(build_bundle())
    ir2, rels2, plan2, ph2 = compile_once(build_bundle())
    check("planHash identical", ph1 == ph2, (ph1, ph2))
    canon1 = json.dumps(plan1, sort_keys=True, ensure_ascii=False)
    canon2 = json.dumps(plan2, sort_keys=True, ensure_ascii=False)
    check("plan bytes identical", canon1 == canon2)
    check("relations edges identical",
          [e["edgeId"] for e in rels1["edges"]] ==
          [e["edgeId"] for e in rels2["edges"]])


def test_position_change_changes_plan():
    print("[3] moving cardB +20px changes plan and only cardB edges")
    base = build_bundle()
    ir0, rels0, plan0, ph0 = compile_once(base)

    moved = build_bundle()
    for a in moved["manifest"]["assets"]:
        if a["nodeId"] == "cardB":
            a["local"]["x"] += 20
            a["designBox"]["x"] += 20
            a["renderAABB"]["x"] += 20
    ir1, rels1, plan1, ph1 = compile_once(moved)

    check("plan hash changes on move", ph0 != ph1, (ph0, ph1))

    def gap(edges, src, tgt):
        for e in edges:
            if e["source"] == src and e["target"] == tgt and e["kind"] == "siblingGap":
                return e["expected"]["gap"]
        return None
    check("sibling gap cardA->cardB grew by 20",
          abs((gap(rels1["edges"], "cardA", "cardB") or 0)
              - (gap(rels0["edges"], "cardA", "cardB") or 0) - 20.0) < 1e-6)
    # cardA's own parentChild inset must NOT change
    def inset(edges, parent, child):
        for e in edges:
            if (e["kind"] == "parentChildInset" and e["source"] == parent
                    and e["target"] == child):
                return e["expected"]
        return None
    check("unrelated cardA inset unchanged",
          inset(rels0["edges"], "row", "cardA") ==
          inset(rels1["edges"], "row", "cardA"))


def test_size_change_changes_plan():
    print("[4] changing cardB size changes plan")
    base = build_bundle()
    _, _, _, ph0 = compile_once(base)
    grown = build_bundle()
    for a in grown["manifest"]["assets"]:
        if a["nodeId"] == "cardB":
            a["local"]["width"] += 30
            a["designBox"]["width"] += 30
    _, _, _, ph1 = compile_once(grown)
    check("plan hash changes on resize", ph0 != ph1)


def test_hidden_and_transparent_disposition():
    print("[5] hidden node disclosed, transparent node unknown-not-zero")
    ir, rels, plan, ph = compile_once(build_bundle())
    check("hidden is not effective-visible",
          ir["nodes"]["hiddenChild"]["effectiveVisibility"] is False)
    # autoLayout absent -> derivation unknown, not zero
    check("autoLayout absent is unknown",
          ir["nodes"]["cardA"]["autoLayout"]["present"] is False
          and ir["nodes"]["cardA"]["autoLayout"]["derivation"] == "unknown")


def test_text_length_change():
    print("[6] text characters change -> text plan / resolved line metrics change")
    base = build_bundle()
    ir0, _, _, ph0 = compile_once(base)
    long = build_bundle()
    long["text_manifest"]["texts"][0]["characters"] = "line1\nline2\nline3"
    ir1, _, _, ph1 = compile_once(long)
    check("plan hash changes on text length", ph0 != ph1, (ph0, ph1))
    lh0 = ir0["nodes"]["title"]["text"]["lineHeightPx"]
    lh1 = ir1["nodes"]["title"]["text"]["lineHeightPx"]
    # AUTO line-height WITHOUT a measured resolvedPx must stay unknown (None);
    # the compiler must NOT fabricate px from box height / line count. Changing
    # the text length must therefore NOT invent a different px.
    check("AUTO line-height without resolvedPx stays unknown (not faked)",
          lh0 is None and lh1 is None, (lh0, lh1))


def test_rotated_box_family():
    print("[7] rotated node flagged; cross-family not compared numerically")
    ir, rels, plan, ph = compile_once(build_bundle())
    rot = ir["nodes"]["rotated"]
    check("rotated flag set", rot["boxComparability"]["rotated"] is True)
    layer = next(l for l in plan["nodes"] if l["nodeId"] == "rotated")
    check("rotated layer uses render family", layer["boxFamily"] == "render")


def test_flipped_row_transform_corrected():
    print("[8] 180-flipped parent: insets computed transform-corrected, not overflow")
    ir, rels, plan, ph = compile_once(build_bundle())
    edge = None
    for e in rels["edges"]:
        if e["kind"] == "parentChildInset" and e["source"] == "flippedRow" \
                and e["target"] == "bubble":
            edge = e
    check("bubble parentChild edge exists", edge is not None)
    if edge:
        check("derivation is transform-corrected",
              edge["derivation"] == "transform-corrected", edge["derivation"])
        check("same-family edge is derived (not cross-family unknown)",
              edge["status"] == "derived", edge["status"])
        exp = edge["expected"]
        check("artboard insets present", "left" in exp and "top" in exp, exp)


def test_sibling_gap_and_order():
    print("[9] sibling gap / order auto-derived")
    ir, rels, plan, ph = compile_once(build_bundle())
    gap = None
    for e in rels["edges"]:
        if e["kind"] == "siblingGap" and {e["source"], e["target"]} == \
                {"cardA", "cardB"}:
            gap = e
    check("horizontal sibling gap derived", gap is not None,
          [e["kind"] for e in rels["edges"]])
    if gap:
        check("gap axis horizontal", gap["expected"]["axis"] == "horizontal")
        # cardA ends at 120, cardB starts at 200 -> 80
        check("gap == 80", abs(gap["expected"]["gap"] - 80.0) < 1e-6,
              gap["expected"])
    order = [e for e in rels["edges"] if e["kind"] == "siblingOrder"]
    check("siblingOrder edge emitted", len(order) >= 1, len(order))
    if order:
        o = order[0]
        check("siblingOrder targets __order__", o["target"] == "__order__",
              o["target"])
        check("siblingOrder carries ordered child list",
              isinstance(o["expected"].get("order"), list), o["expected"])


def test_icon_host_center():
    print("[10] small contained node pairs with host via iconHostCenter")
    ir, rels, plan, ph = compile_once(build_bundle())
    host = [e for e in rels["edges"] if e["kind"] == "iconHostCenter"
            and e["source"] == "row" and e["target"] == "icon"]
    check("iconHostCenter edge derived", len(host) == 1,
          [e["kind"] for e in rels["edges"]])


def test_connector_horizontal_and_vertical():
    print("[11] connectors derive source/target edge deterministically")
    ir, rels, plan, ph = compile_once(build_bundle())
    def conn(src, tgt):
        for e in rels["edges"]:
            if e["kind"] == "connectorEdge" and e["source"] == src \
                    and e["target"] == tgt:
                return e
        return None
    h = conn("cardA", "cardB")
    check("horizontal connector derived", h is not None)
    if h:
        check("h: source right / target left",
              h["expected"]["sourceEdge"] == "right"
              and h["expected"]["targetEdge"] == "left", h["expected"])
        check("h not uncertain", h.get("uncertain") is False)
    v = conn("cardB", "title")
    check("vertical connector derived", v is not None)
    if v:
        check("v: source bottom / target top",
              v["expected"]["sourceEdge"] == "bottom"
              and v["expected"]["targetEdge"] == "top", v["expected"])


def test_connector_unknown_is_unverifiable():
    print("[12] connector without source/target -> unverifiable, never guessed")
    ir, rels, plan, ph = compile_once(build_bundle())
    orphan = [e for e in rels["edges"] if e["kind"] == "connectorEdge"
             and e["source"] == "arrowOrphan"]
    check("orphan connector emitted", len(orphan) == 1, len(orphan))
    if orphan:
        check("orphan status unverifiable",
              orphan[0]["status"] == "unverifiable", orphan[0]["status"])
        check("orphan uncertain", orphan[0].get("uncertain") is True)
    # unknown must not be counted as derived
    check("unknown never counted as derived",
          rels["summary"]["unknownEdges"] >= 1, rels["summary"])


def test_nested_structure():
    print("[13] nested parentChild insets cover every parentId link")
    ir, rels, plan, ph = compile_once(build_bundle())
    links = {(e["source"], e["target"]) for e in rels["edges"]
             if e["kind"] == "parentChildInset"}
    for parent, child in [("stage", "row"), ("row", "cardA"), ("row", "cardB"),
                          ("row", "icon"), ("flippedRow", "bubble"),
                          ("stage", "title"), ("stage", "rotated")]:
        check("parentChild link %s->%s" % (parent, child),
              (parent, child) in links, sorted(links))


def test_text_plan_decision_table():
    print("[14] fixed-box NONE text -> pre-wrap, not pre")
    ir, rels, plan, ph = compile_once(build_bundle())
    tp = next(t for t in plan["textPlans"] if t["nodeId"] == "title")
    check("NONE fixed box -> pre-wrap", tp["whiteSpace"] == "pre-wrap",
          tp["whiteSpace"])
    check("outer width is box width", tp["outerWidth"] == 200.0, tp)


def test_write_outputs_and_sidecar(tmp_dir=None):
    print("[15] write_outputs writes 4 files incl. sha256 sidecar")
    import tempfile
    ir, rels, plan, ph = compile_once(build_bundle())
    with tempfile.TemporaryDirectory() as td:
        paths = cd.write_outputs(td, ir, rels, plan, ph)
        for k in ["design-ir.json", "component-relations.json",
                  "layout-plan.json", "layout-plan.json.sha256"]:
            check("file written: " + k, k in paths and os.path.exists(paths[k]))
        side = open(paths["layout-plan.json.sha256"]).read().split()[0]
        check("sidecar hash matches planHash", side == ph, (side, ph))


def main():
    test_all_nodes_have_disposition()
    test_determinism()
    test_position_change_changes_plan()
    test_size_change_changes_plan()
    test_hidden_and_transparent_disposition()
    test_text_length_change()
    test_rotated_box_family()
    test_flipped_row_transform_corrected()
    test_sibling_gap_and_order()
    test_icon_host_center()
    test_connector_horizontal_and_vertical()
    test_connector_unknown_is_unverifiable()
    test_nested_structure()
    test_text_plan_decision_table()
    test_write_outputs_and_sidecar()

    print("\n=== RESULT ===")
    print(f"passed: {len(PASS)}  failed: {len(FAIL)}")
    if FAIL:
        print("FAILURES:")
        for name, detail in FAIL:
            print(" -", name, "::", detail)
        sys.exit(1)


if __name__ == "__main__":
    main()
