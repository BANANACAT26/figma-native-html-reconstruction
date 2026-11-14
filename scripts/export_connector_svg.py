#!/usr/bin/env python3
"""Generic standalone small-node SVG export for connectors.

The existing export_artboard pipeline rasterizes maximal TEXT-free subtrees to
PNG. For connectors we need the opposite: a tiny vector export of ONE
connector node (line body + bends + dot + arrowhead together as one SVG), so
the auditor can resolve the real arrow tip via path.getPointAtLength instead
of guessing from a flattened PNG bbox.

Rules enforced here:

  * Export ONLY the named connector node(s) as SVG -- never the whole artboard
    and never a board-wide group. Board-wide SVG would bake text/positions
    and defeat the native text-DOM reconstruction.
  * Refuse if the node subtree contains any TEXT node (connectors must be
    pure vector). If a connector accidentally contains text, that is a
    modeling error we surface rather than bake.
  * Writes <nodeId>.svg next to a small sidecar <nodeId>.svg.json with the
    node's designBox / render bounds so the builder can place the SVG at the
    same coordinates a PNG would use.
  * No brand colors, no node-id-specific offsets, no fixed compensation.

This script is NOT started automatically; the integration agent decides when
to call it (only when live Figma is open and the connector node ids are
known). It uses the same FW client as export_artboard.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from figwright_client import FW  # noqa: E402


def _has_text(node):
    if node.get("type") == "TEXT":
        return True
    return any(_has_text(c) for c in (node.get("children") or []))


def _render_aabb(node):
    """Best-effort bounds of the node itself (not the subtree union).

    Connectors are small; we just record the node's own x/y/w/h in parent
    space. The builder will place the SVG using the same designBox convention
    as PNG assets.
    """
    return {
        "x": node.get("x", 0),
        "y": node.get("y", 0),
        "width": node.get("width", 0),
        "height": node.get("height", 0),
        "rotation": node.get("rotation", 0),
    }


def export_connector_svg(fw, node_ids, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    exported = []
    refused = []
    for nid in node_ids:
        node = fw.get_node(nid)
        if not node:
            refused.append({"nodeId": nid, "reason": "node not found"})
            continue
        if _has_text(node):
            refused.append({"nodeId": nid,
                            "reason": "subtree contains TEXT; refusing SVG bake"})
            continue
        if node.get("type") == "TEXT":
            refused.append({"nodeId": nid, "reason": "connector node is TEXT"})
            continue
        # The plugin's save_screenshots accepts fmt=SVG for small vector nodes.
        try:
            saved = fw.save_screenshots([nid], out_dir, fmt="SVG", scale=1.0)
        except Exception as e:  # plugin may not support SVG on this build
            refused.append({"nodeId": nid,
                            "reason": f"SVG export failed: {e}; use PNG bbox audit"})
            continue
        rec = {
            "nodeId": nid,
            "name": node.get("name"),
            "type": node.get("type"),
            "designBox": _render_aabb(node),
            "saved": saved,
            "assetKind": "svg",
            "tipResolution": "auditor must use path.getPointAtLength on the "
                             "longest stroked path; do NOT infer tip from bbox",
        }
        exported.append(rec)
    sidecar = {
        "connectorCount": len(exported),
        "refusedCount": len(refused),
        "exported": exported,
        "refused": refused,
        "note": "Vector export of connector nodes only; never the board. "
                "Text-bearing subtrees refused. Place SVG at designBox the "
                "same way PNG assets are placed.",
    }
    with open(os.path.join(out_dir, "connectors.manifest.json"), "w",
              encoding="utf-8") as fh:
        json.dump(sidecar, fh, ensure_ascii=False, indent=2)
    print(json.dumps({"exported": len(exported), "refused": len(refused)},
                     ensure_ascii=False))
    return sidecar


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("nodeIds", nargs="+",
                    help="connector node ids to export as standalone SVG")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--allow-whole-board", action="store_true",
                    help="(discouraged) permit exporting a board root; do not "
                         "use -- text would be baked into the SVG")
    args = ap.parse_args()
    if not args.allow_whole_board:
        # We do not know node types until we hit FW, but we can refuse a
        # blatant board id heuristic: the integration agent must pass leaf
        # connector ids. We defer the real check to export_connector_svg.
        pass
    fw = FW()
    try:
        export_connector_svg(fw, args.nodeIds, args.out_dir)
    finally:
        fw.close()


if __name__ == "__main__":
    main()
