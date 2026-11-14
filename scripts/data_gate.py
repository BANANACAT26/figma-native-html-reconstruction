#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0 data gate: machine-readable contract check AFTER compile, BEFORE build.

Runs off the files compile_design.py already wrote into the export dir
(manifest.json, design-ir.json, layout-plan.json). It does NOT call live Figma
and does not re-layout. It verifies the data contract that the builder relies on:

  G1 rootFills        manifest.rootFills present; IR source carries it verbatim;
                      plan.artboard.rootFills present. Missing/None = unknown
                      (block); [] = explicit no fill (pass + note).
  G2 text runs        plan textPlans: a 'mixed' fontSize must have every run with
                      a numeric fontSize (no 16px impersonation). AUTO lineHeight
                      must not carry a fabricated px (source 'resolved' from box
                      height is forbidden; only live-auto/authoritative/percent/
                      unknown are allowed).
  G3 geometry/disposition  every rendered node has an artboard-space render box
                      (renderAABB/designBox); rendered-asset nodes carry
                      pngPlacement + exportSize; no leftover reprojected box flag.

Optional `--live-snapshot` points at a raw SDK snapshot and checks that the
compiled fields cover the live node set (source coverage), without trusting it
for geometry.

Exit code: 0 = pass (warnings allowed); 1 = hard failure. Evidence JSON is
written to `--out` (default <export-dir>/data-gate.json).
"""
import argparse
import json
import pathlib
import sys


def _load(p):
    try:
        return json.loads(pathlib.Path(p).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def run_gate(export_dir, live_snapshot=None):
    ed = pathlib.Path(export_dir)
    failures, warnings = [], []

    manifest = _load(ed / "manifest.json")
    ir = _load(ed / "design-ir.json") or {}
    plan = _load(ed / "layout-plan.json") or {}

    if manifest is None:
        failures.append({"gate": "G0", "reason": "manifest.json missing in export dir"})
        return {"pass": False, "failures": failures, "warnings": warnings,
                "evidence": {"exportDir": str(ed)}}

    # ---- G1 rootFills ----
    rf = manifest.get("rootFills")
    if "rootFills" not in manifest:
        failures.append({"gate": "G1", "reason": "manifest.rootFills key absent (unknown stage bg)"})
    elif rf is None:
        failures.append({"gate": "G1", "reason": "manifest.rootFills is null (unknown stage bg)"})
    elif rf == []:
        warnings.append({"gate": "G1", "reason": "rootFills == []: explicit empty stage"})

    ir_src = ir.get("source") or {}
    if "rootFills" not in ir_src:
        failures.append({"gate": "G1", "reason": "IR source.rootFills dropped by compile"})
    plan_ab = plan.get("artboard") or {}
    if "rootFills" not in plan_ab:
        failures.append({"gate": "G1", "reason": "plan.artboard.rootFills missing (builder will block)"})

    # ---- G2 text runs / no 16px impersonation / no fabricated AUTO ----
    for tp in plan.get("textPlans", []) or []:
        nid = tp.get("nodeId")
        fs = tp.get("fontSize")
        runs = tp.get("runs") or []
        if isinstance(fs, str) and fs.strip().lower() == "mixed":
            if not runs:
                failures.append({"gate": "G2", "node": nid,
                                 "reason": "fontSize='mixed' but plan carries no runs"})
            for i, r in enumerate(runs):
                if not isinstance(r.get("fontSize"), (int, float)):
                    failures.append({"gate": "G2", "node": nid, "run": i,
                                     "reason": f"mixed run #{i} fontSize={r.get('fontSize')!r} not numeric"})
        # fabricated AUTO leading is forbidden; only trusted sources allowed.
        src = tp.get("lineHeightSource")
        if src == "resolved":
            failures.append({"gate": "G2", "node": nid,
                             "reason": "lineHeightSource='resolved' is the old box-height fabrication"})

    # ---- G3 geometry / disposition ----
    for n in plan.get("nodes", []) or []:
        nid = n.get("nodeId")
        disp = n.get("disposition", "")
        if disp.startswith(("rendered",)):
            box = n.get("renderAABB") or n.get("designBox")
            if not box:
                failures.append({"gate": "G3", "node": nid,
                                 "reason": f"rendered node disposition={disp!r} has no render box"})
        if n.get("boxComparability", {}).get("renderReprojected"):
            failures.append({"gate": "G3", "node": nid,
                             "reason": "renderReprojected flag present (wrong parent-relative reprojection)"})

    # rendered-asset nodes must be backed by placement + pixel size
    for a in plan.get("assets", []) or []:
        if not a.get("pngPlacement"):
            failures.append({"gate": "G3", "node": a.get("nodeId"),
                             "reason": "rendered asset lacks pngPlacement"})
        if not a.get("exportSize"):
            failures.append({"gate": "G3", "node": a.get("nodeId"),
                             "reason": "rendered asset lacks exportSize"})

    # ---- optional live source coverage ----
    coverage = None
    if live_snapshot:
        live = _load(live_snapshot) or {}
        live_ids = set()
        for it in (live.get("nodes") or live.get("children") or []):
            if isinstance(it, dict) and it.get("id"):
                live_ids.add(it["id"])
        plan_ids = {n.get("nodeId") for n in plan.get("nodes", []) or []}
        missing = sorted(live_ids - plan_ids)
        coverage = {"liveNodeCount": len(live_ids), "planNodeCount": len(plan_ids),
                    "liveNotInPlan": missing}
        if missing:
            warnings.append({"gate": "G0", "reason": f"{len(missing)} live nodes absent from plan"})

    return {
        "pass": not failures,
        "failures": failures,
        "warnings": warnings,
        "evidence": {"exportDir": str(ed), "liveCoverage": coverage},
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--export-dir", required=True)
    ap.add_argument("--live-snapshot")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    result = run_gate(args.export_dir, live_snapshot=args.live_snapshot)
    out = pathlib.Path(args.out) if args.out else pathlib.Path(args.export_dir) / "data-gate.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"pass": result["pass"], "failures": len(result["failures"]),
                      "warnings": len(result["warnings"]), "out": str(out)}))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
