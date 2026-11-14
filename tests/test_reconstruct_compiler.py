#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline end-to-end tests for the Design Compiler default flow in
scripts/reconstruct.py.

Zero third-party framework:  python3 tests/test_reconstruct_compiler.py

No live Figma, no Chrome launch: child scripts are intercepted by a fake
RUNNER (argv recorded + stub reports written), except compile_design.py which
runs for real to prove plan-hash reproducibility. Covers:

  1. default chain calls compile -> relations (full graph) -> browser-audit
     content -> text audit, in reuse and live modes;
  2. plan hash reproducibility (same export -> same planHash + sidecar);
  3. text gate failure triggers exactly ONE deterministic repair_text round
     (pre -> pre-wrap, never font-size) then one re-audit;
  4. connector strict-fail triggers exactly ONE repair_connectors fallback
     round then one re-audit;
  5. strict (default) blocks on unknown stages; --no-strict relaxes;
  6. a failing upstream stage (B) is NOT masked by a perfect pixel gate (G).
"""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import reconstruct  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
    else:
        FAIL.append((name, detail))


# ---------------------------------------------------------------------------
# fixture builders
# ---------------------------------------------------------------------------
PNG1x1 = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
          b'\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00'
          b'\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82')


def make_export(td):
    """Minimal export dir compile_design can consume (offline)."""
    export = pathlib.Path(td) / "export" / "sample"
    export.mkdir(parents=True)
    manifest = {
        "artboardId": "board-1",
        "artboardName": "Board",
        "artboardSize": {"width": 1000.0, "height": 800.0},
        "assets": [
            {"nodeId": "stage", "parentId": None, "type": "FRAME",
             "zIndexChain": [0], "visible": True, "effectiveVisible": True,
             "opacity": 1.0, "effectiveOpacity": 1.0,
             "local": {"x": 0, "y": 0, "width": 1000, "height": 800},
             "designBox": {"x": 0, "y": 0, "width": 1000, "height": 800},
             "renderAABB": {"x": 0, "y": 0, "width": 1000, "height": 800},
             "absoluteTransform": [1, 0, 0, 1, 0, 0],
             "effectiveClipRect": {"x": 0, "y": 0, "width": 1000, "height": 800},
             "fills": [{"type": "SOLID", "visible": True,
                        "color": {"r": 1, "g": 1, "b": 1}}]},
            {"nodeId": "cardA", "parentId": "stage", "type": "RECTANGLE",
             "zIndexChain": [0, 0], "visible": True, "effectiveVisible": True,
             "opacity": 1.0, "effectiveOpacity": 1.0,
             "local": {"x": 20, "y": 20, "width": 100, "height": 60},
             "designBox": {"x": 20, "y": 20, "width": 100, "height": 60},
             "renderAABB": {"x": 20, "y": 20, "width": 100, "height": 60},
             "absoluteTransform": [1, 0, 0, 1, 0, 0],
             "effectiveClipRect": {"x": 0, "y": 0, "width": 1000, "height": 800},
             "fills": [{"type": "SOLID", "visible": True,
                        "color": {"r": 0.9, "g": 0.8, "b": 0.7}}]},
        ],
        "containerVisuals": [],
        "contentLedger": [
            {"nodeId": "stage", "parentId": None, "type": "FRAME",
             "selfVisible": True, "effectiveVisible": True, "effectiveOpacity": 1.0,
             "zIndexChain": [0], "disposition": "rendered-stage"},
            {"nodeId": "cardA", "parentId": "stage", "type": "RECTANGLE",
             "selfVisible": True, "effectiveVisible": True, "effectiveOpacity": 1.0,
             "zIndexChain": [0, 0], "disposition": "rendered-asset"},
            {"nodeId": "title", "parentId": "stage", "type": "TEXT",
             "selfVisible": True, "effectiveVisible": True, "effectiveOpacity": 1.0,
             "zIndexChain": [0, 1], "disposition": "rendered-text"},
        ],
    }
    text_manifest = {
        "artboardId": "board-1",
        "texts": [
            {"nodeId": "title", "characters": "Hello World",
             "fontName": {"family": "Inter", "style": "Regular"},
             "fontSize": 14.0, "lineHeight": {"type": "PIXELS", "value": 20.0},
             "textAutoResize": "WIDTH_AND_HEIGHT",
             "absoluteTransform": [1, 0, 0, 1, 30.0, 620.0],
             "designBox": {"x": 30, "y": 620, "width": 120, "height": 20}},
        ],
    }
    (export / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (export / "text-manifest.json").write_text(json.dumps(text_manifest),
                                               encoding="utf-8")
    return export


def make_page(td, style="white-space:pre;"):
    page = pathlib.Path(td) / "page.html"
    page.write_text(
        '<html><body><div id="stage" style="width:1000px;height:800px">'
        f'<div class="tlayer" data-n="title" style="{style}">Hello World</div>'
        '<div class="layer" data-n="cardA" style="left:20px;top:20px;'
        'width:100px;height:60px;background:#eec"></div>'
        '</div></body></html>', encoding="utf-8")
    return page


class FakeRunner:
    """Records argv and writes stub reports so the rollup logic runs for real."""

    def __init__(self, mode="clean"):
        self.calls = []
        self.mode = mode  # clean / textfail / connfail / bfail

    def __call__(self, cmd, check=True):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        name = pathlib.Path(cmd[1]).name
        ev = self._ev(cmd)
        rc = 0

        class R:
            returncode = rc
        if name == "verify.py":
            ev = pathlib.Path(self._flag(cmd, "--out"))
            ev.mkdir(parents=True, exist_ok=True)
            json.dump({"raw": {"mismatch": 0.01},
                       "outside_text": {"mismatch": 0.01},
                       "text_region": {"mismatch": 0.02}},
                      open(ev / "metrics.json", "w"))
            (ev / "run.json").write_text("{}", encoding="utf-8")
            (ev / "run.json.sha256").write_text("deadbeef  run.json\n",
                                                encoding="utf-8")
            (ev / "browser.png").write_bytes(PNG1x1)
        elif name == "audit_relations.py":
            ev = pathlib.Path(self._flag(cmd, "--out-dir"))
            ev.mkdir(parents=True, exist_ok=True)
            json.dump({"ruleVersion": "design-compiler/1.0.0",
                       "planHash": "stub",
                       "summary": {"pass": 4, "fail": 0, "unknown": 0,
                                   "total": 4, "strictFail": 0}},
                      open(ev / "relations-audit.json", "w"))
            json.dump({"actions": [], "blocked": [], "unknown": []},
                      open(ev / "repair-plan.json", "w"))
        elif name == "audit_content.py":
            hard = 2 if self.mode == "bfail" else 0
            out = self._out(cmd)
            out.parent.mkdir(parents=True, exist_ok=True)
            json.dump({"hardFailures": hard, "missingInHtml": [],
                       "missingTexts": [], "overRenderedSkipped": [],
                       "unresolved": [], "ledgerSummary": {},
                       "browserAudit": {"allPass": hard == 0,
                                       "gateSummary": {"nodes": 3}}},
                      open(out, "w"))
        elif name == "audit_text.py":
            # Nth call driven by counting how many audit_text calls we already saw
            n = sum(1 for c in self.calls
                    if pathlib.Path(c[1]).name == "audit_text.py")
            out = self._out(cmd)
            out.parent.mkdir(parents=True, exist_ok=True)
            if self.mode == "textfail" and n == 1:
                json.dump({"hardFailureCount": 1,
                           "lineAudit": [{"node": "title", "overflow": {"x": 5, "y": 0},
                                          "hasTableWrapper": True,
                                          "computed": {"whiteSpace": "pre",
                                                       "lineHeight": "normal"},
                                          "mode": "fixed-size"}],
                           "colorAudit": {"unknowns": []},
                           "fonts": {"missing": []},
                           "errors": []},
                          open(out, "w"))
            else:
                json.dump({"hardFailureCount": 0, "lineAudit": [],
                           "colorAudit": {"unknowns": []},
                           "fonts": {"missing": []}, "errors": []},
                          open(out, "w"))
        elif name == "audit_connectors.py":
            n = sum(1 for c in self.calls
                    if pathlib.Path(c[1]).name == "audit_connectors.py")
            first_fail = (self.mode == "connfail" and n == 1)
            out = self._out(cmd)
            out.parent.mkdir(parents=True, exist_ok=True)
            json.dump({"connectors": [{"id": "c1", "verdict": "tip-miss"
                                       if first_fail else "pass"}],
                       "summary": {"total": 1, "tipVerified": 1 if not first_fail else 0,
                                   "strictFail": 1 if first_fail else 0,
                                   "byVerdict": {"pass": 1} if not first_fail
                                   else {"tip-miss": 1}}},
                      open(out, "w"))
        elif name == "repair_connectors.py":
            out = pathlib.Path(self._flag(cmd, "--out"))
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self._html(cmd), out)
        return R()

    # helpers --------------------------------------------------------------
    def _flag(self, cmd, flag):
        return cmd[cmd.index(flag) + 1]

    def _html(self, cmd):
        return self._flag(cmd, "--html")

    def _out(self, cmd):
        return pathlib.Path(self._flag(cmd, "--out"))

    def _ev(self, cmd):
        flag = "--out-dir" if "--out-dir" in cmd else "--out"
        return pathlib.Path(self._flag(cmd, flag))


def fake_measure(page, stage_selector, chrome_channel, out_json):
    pathlib.Path(out_json).write_text(json.dumps({"nodes": {}, "domOrder": []}),
                                      encoding="utf-8")


def run_main(td, **kw):
    """Invoke reconstruct.main in reuse mode; return (exit_code, summary)."""
    export = kw.pop("export")
    page = kw.pop("page")
    runner = kw.pop("runner")
    overview = kw.pop("overview", None)
    argv = ["--reference", str(kw.pop("reference")),
            "--workdir", str(pathlib.Path(td) / "work"),
            "--reuse-export-dir", str(export), "--html", str(page)]
    if kw.pop("no_compile", False):
        argv.append("--no-compile")
    if kw.pop("no_strict", False):
        argv.append("--no-strict")
    if kw.pop("connectors_spec", None):
        spec = pathlib.Path(td) / "spec.json"
        spec.write_text(json.dumps({"connectors": [
            {"id": "c1", "sourceSelector": '[data-n="cardA"]',
             "targetSelector": '[data-n="title"]', "sourceEdge": "right",
             "targetEdge": "left"}]}), encoding="utf-8")
        argv += ["--connectors-spec", str(spec)]
    if overview:
        ov = pathlib.Path(td) / "fit.png"
        ov.write_bytes(PNG1x1)
        argv += ["--overview-evidence", str(ov)]
    reconstruct.RUNNER = runner
    reconstruct.measure_dom = fake_measure
    code = 0
    try:
        reconstruct.main(argv)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    summary = json.loads((pathlib.Path(td) / "work" / "summary.json")
                         .read_text(encoding="utf-8"))
    return code, summary


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------
def t1_reuse_chain_default_calls():
    with tempfile.TemporaryDirectory() as td:
        export = make_export(td)
        # real compile once so layout-plan exists
        subprocess.run([sys.executable, str(SCRIPTS / "compile_design.py"),
                        "--export-dir", str(export)], check=True,
                       capture_output=True)
        page = make_page(td)
        ref = pathlib.Path(td) / "ref.png"; ref.write_bytes(PNG1x1)
        (export / "connectors.manifest.json").write_text(
            json.dumps({"connectorCount": 0, "exported": []}), encoding="utf-8")
        runner = FakeRunner("clean")
        code, summary = run_main(td, export=export, page=page, reference=ref,
                                 runner=runner, overview=True)
        scripts = [pathlib.Path(c[1]).name for c in runner.calls]
        check("t1 exit 0 on clean strict run", code == 0, str(code))
        check("t1 audit_relations called", "audit_relations.py" in scripts,
              str(scripts))
        rel = [c for c in runner.calls
               if pathlib.Path(c[1]).name == "audit_relations.py"][0]
        check("t1 relations uses --measured", "--measured" in rel, str(rel))
        con = [c for c in runner.calls
               if pathlib.Path(c[1]).name == "audit_content.py"][0]
        check("t1 content uses --browser-audit", "--browser-audit" in con, str(con))
        check("t1 builder walks plan (hash recorded)",
              summary["compiler"]["planHash"] is not None,
              json.dumps(summary["compiler"]))
        check("t1 all stages pass",
              all(v["status"] == "pass" for v in summary["stages"].values()),
              json.dumps(summary["stages"]))


def t2_plan_hash_reproducible():
    with tempfile.TemporaryDirectory() as td:
        e1 = make_export(td + "/a")
        e2 = make_export(td + "/b")
        for e in (e1, e2):
            subprocess.run([sys.executable, str(SCRIPTS / "compile_design.py"),
                            "--export-dir", str(e)], check=True,
                           capture_output=True)
        p1 = json.loads((e1 / "layout-plan.json").read_text(encoding="utf-8"))
        p2 = json.loads((e2 / "layout-plan.json").read_text(encoding="utf-8"))
        s1 = (e1 / "layout-plan.json.sha256").read_text(encoding="utf-8")
        s2 = (e2 / "layout-plan.json.sha256").read_text(encoding="utf-8")
        check("t2 planHash identical across runs",
              p1["planHash"] == p2["planHash"] and bool(p1["planHash"]),
              f'{p1["planHash"]} vs {p2["planHash"]}')
        check("t2 sidecar written", s1.split()[0] == p1["planHash"], s1)
        check("t2 sidecar reproducible", s1 == s2, f"{s1} vs {s2}")


def t3_text_repair_one_shot():
    with tempfile.TemporaryDirectory() as td:
        export = make_export(td)
        subprocess.run([sys.executable, str(SCRIPTS / "compile_design.py"),
                        "--export-dir", str(export)], check=True,
                       capture_output=True)
        page = make_page(td, style="white-space:pre;")
        ref = pathlib.Path(td) / "ref.png"; ref.write_bytes(PNG1x1)
        (export / "connectors.manifest.json").write_text(
            json.dumps({"connectorCount": 0}), encoding="utf-8")
        runner = FakeRunner("textfail")
        code, summary = run_main(td, export=export, page=page, reference=ref,
                                 runner=runner, overview=True)
        n_text = sum(1 for c in runner.calls
                     if pathlib.Path(c[1]).name == "audit_text.py")
        check("t3 exactly one repair round",
              summary["repairs"]["text"] and
              summary["repairs"]["text"]["rounds"] == 1,
              json.dumps(summary["repairs"]))
        check("t3 audit_text ran twice (first + re-audit)", n_text == 2,
              str(n_text))
        html = page.read_text(encoding="utf-8")
        check("t3 pre -> pre-wrap applied", "white-space:pre-wrap;" in html, html)
        check("t3 never rewrote font-size",
              summary["repairs"]["text"]["fontSizeRewrites"] == 0,
              json.dumps(summary["repairs"]))
        check("t3 stage D passes after repair",
              summary["stages"]["D_text"]["status"] == "pass",
              json.dumps(summary["stages"]["D_text"]))


def t4_connector_fallback_one_shot():
    with tempfile.TemporaryDirectory() as td:
        export = make_export(td)
        subprocess.run([sys.executable, str(SCRIPTS / "compile_design.py"),
                        "--export-dir", str(export)], check=True,
                       capture_output=True)
        page = make_page(td)
        ref = pathlib.Path(td) / "ref.png"; ref.write_bytes(PNG1x1)
        runner = FakeRunner("connfail")
        code, summary = run_main(td, export=export, page=page, reference=ref,
                                 runner=runner, overview=True,
                                 connectors_spec=True)
        n_conn = sum(1 for c in runner.calls
                     if pathlib.Path(c[1]).name == "audit_connectors.py")
        n_rep = sum(1 for c in runner.calls
                    if pathlib.Path(c[1]).name == "repair_connectors.py")
        check("t4 repair_connectors ran once", n_rep == 1, str(n_rep))
        check("t4 audit_connectors ran twice", n_conn == 2, str(n_conn))
        check("t4 stage E passes after fallback",
              summary["stages"]["E_connectors"]["status"] == "pass" and
              summary["stages"]["E_connectors"].get("repairRound") == 1,
              json.dumps(summary["stages"]["E_connectors"]))


def t5_strict_unknown_blocks():
    with tempfile.TemporaryDirectory() as td:
        export = make_export(td)  # no compile -> no layout-plan
        page = make_page(td)
        ref = pathlib.Path(td) / "ref.png"; ref.write_bytes(PNG1x1)
        runner = FakeRunner("clean")
        code, summary = run_main(td, export=export, page=page, reference=ref,
                                 runner=runner, no_compile=True)
        check("t5 strict exits non-zero on unknown", code == 2, str(code))
        check("t5 C relations unknown without plan",
              summary["stages"]["C_relations"]["status"] == "unknown",
              json.dumps(summary["stages"]["C_relations"]))
        check("t5 F unknown without fit evidence",
              summary["stages"]["F_visual"]["status"] == "unknown",
              json.dumps(summary["stages"]["F_visual"]))
        check("t5 unknowns machine-readable and listed",
              "C_relations" in summary["unknownStages"] and
              "F_visual" in summary["unknownStages"],
              json.dumps(summary["unknownStages"]))
        # relaxed mode
        code2, _ = run_main(td, export=export, page=page, reference=ref,
                           runner=FakeRunner("clean"), no_compile=True,
                           no_strict=True)
        check("t5 --no-strict exits 0 despite unknown", code2 == 0, str(code2))


def t6_upstream_fail_not_masked_by_pixel():
    with tempfile.TemporaryDirectory() as td:
        export = make_export(td)
        subprocess.run([sys.executable, str(SCRIPTS / "compile_design.py"),
                        "--export-dir", str(export)], check=True,
                       capture_output=True)
        page = make_page(td)
        ref = pathlib.Path(td) / "ref.png"; ref.write_bytes(PNG1x1)
        (export / "connectors.manifest.json").write_text(
            json.dumps({"connectorCount": 0}), encoding="utf-8")
        runner = FakeRunner("bfail")
        code, summary = run_main(td, export=export, page=page, reference=ref,
                                 runner=runner, overview=True)
        check("t6 pixel gate G passes anyway",
              summary["stages"]["G_pixel"]["status"] == "pass",
              json.dumps(summary["stages"]["G_pixel"]))
        check("t6 content gate B fails",
              summary["stages"]["B_content"]["status"] == "fail",
              json.dumps(summary["stages"]["B_content"]))
        check("t6 strict still exits 2", code == 2, str(code))
        check("t6 B listed in failedStages",
              "B_content" in summary["failedStages"],
              json.dumps(summary["failedStages"]))


def main():
    for name, fn in [
        ("default chain calls compile/relations/browser-audit", t1_reuse_chain_default_calls),
        ("plan hash reproducible across runs", t2_plan_hash_reproducible),
        ("text gate: one deterministic repair round + re-audit", t3_text_repair_one_shot),
        ("connectors: one repair_connectors fallback + re-audit", t4_connector_fallback_one_shot),
        ("strict (default) blocks unknown; --no-strict relaxes", t5_strict_unknown_blocks),
        ("upstream B fail not masked by perfect G pixel", t6_upstream_fail_not_masked_by_pixel),
    ]:
        try:
            fn()
        except Exception as e:
            import traceback
            FAIL.append((name, traceback.format_exc()[-400:]))
    width = max([len(n) for n, _ in FAIL + [(p, "") for p in PASS]] or [0])
    for p in PASS:
        print(f"[PASS] {p}")
    for n, d in FAIL:
        print(f"[FAIL] {n}\n       {d}")
    print(f"\n{len(PASS)}/{len(PASS)+len(FAIL)} passed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
