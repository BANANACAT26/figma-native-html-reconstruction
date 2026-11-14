#!/usr/bin/env python3
"""Font landing self-check for figma-native-html-reconstruction.

Answers one question before you are allowed to ship a page: **is every font the
design actually uses present in this project, declared by a real @font-face, and
the right family/style?**

Missing fonts are a TODO in this Skill, not a disclosure: run this, and if it
reports a gap, go download the font (see references/font-sourcing.md) instead of
falling back to a system font.

Per required (family, style) it checks:
  1. an @font-face declares that family (case-insensitive, quotes stripped)
  2. that face's weight covers the requested style  (ExtraBold never satisfies
     Regular -- a wrong cut is a gap, not a pass)
  3. the src URL resolves to an existing file **in the same context the browser
     will use**: relative to the HTML when the CSS is inlined by the builder,
     relative to the CSS file when it is linked. The classic
     "fonts/fonts/xxx.ttf" double-prefix bug fails here.
  4. the file's name table (nameID 1/2/4/6) is self-consistent and agrees with
     the requested family
  5. optional --glyphs: cmap coverage for the characters that matter

Fonts shipped by the OS (Zapfino on macOS) are reported as `system` with a
portability warning instead of a gap -- they are not embedded in the delivery,
so another machine may fall back.

Exit 0 = every required family/style is landed (or is an OS font);
exit 1 = at least one gap that must be resolved by downloading a font.
"""
import argparse
import hashlib
import json
import logging
import pathlib
import re
import sys

# fontTools is chatty about odd head timestamps in vendor fonts; that noise
# must not look like a check failure.
logging.getLogger("fontTools").setLevel(logging.ERROR)

STYLE_WEIGHT = {
    "thin": 100, "hairline": 100,
    "extralight": 200, "ultralight": 200,
    "light": 300,
    "book": 400, "regular": 400, "normal": 400, "roman": 400, "": 400,
    "medium": 500,
    "semibold": 600, "demibold": 600, "demi": 600,
    "bold": 700,
    "extrabold": 800, "ultrabold": 800, "heavy": 800,
    "black": 900,
}

SYSTEM_FONT_DIRS = {
    "darwin": ["/System/Library/Fonts", "/System/Library/Fonts/Supplemental", "/Library/Fonts"],
    "linux": ["/usr/share/fonts", "/usr/local/share/fonts"],
    "win32": [r"C:\Windows\Fonts"],
}


def style_weight(style):
    """'Regular' / 'SemiBold' / 'ExtraBold Italic' / '400' -> weight int (or None)."""
    if style is None:
        return None
    s = str(style).strip().lower()
    if s.isdigit():
        return int(s)
    for sep in (" italic", " oblique", " slanted"):
        s = s.replace(sep, "")
    s = re.sub(r"[\s\-_]", "", s)
    if s in STYLE_WEIGHT:
        return STYLE_WEIGHT[s]
    for key in sorted(STYLE_WEIGHT, key=len, reverse=True):  # longest first
        k = re.sub(r"[\s\-_]", "", key)
        if k and k in s:
            return STYLE_WEIGHT[key]
    return None


def parse_font_faces(css_path):
    text = css_path.read_text(encoding="utf-8", errors="replace")
    faces = []
    for m in re.finditer(r"@font-face\s*\{(.*?)\}", text, re.S):
        body = m.group(1)
        fam = re.search(r"font-family\s*:\s*([^;]+);", body)
        weight = re.search(r"font-weight\s*:\s*([^;]+);", body)
        src = re.search(r"src\s*:\s*(.*?);", body, re.S)
        srcs = []
        if src:
            for u in re.finditer(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", src.group(1)):
                srcs.append(u.group(1).strip())
        faces.append({
            "family": (fam.group(1).strip().strip("'\"") if fam else ""),
            "weight": (weight.group(1).strip() if weight else "400"),
            "srcs": srcs,
            "line": text[:m.start()].count("\n") + 1,
        })
    return faces


def resolve_in_contexts(src, bases):
    """Resolve a CSS url() like a browser would, in every candidate base dir."""
    s = src.strip()
    if s.startswith("data:"):
        return None, "data-uri"
    s = s.split("?")[0].split("#")[0]
    p = pathlib.Path(s)
    out = []
    if p.is_absolute():
        out.append(p)
    else:
        out = [(b / p) for b in bases]
    return out, None


def read_font_file(p):
    from fontTools.ttLib import TTFont
    f = TTFont(str(p), lazy=True)
    names = {}
    for n in f["name"].names:
        try:
            names.setdefault(n.nameID, n.toUnicode())
        except Exception:
            pass
    info = {
        "family": names.get(1, ""),
        "subfamily": names.get(2, ""),
        "full": names.get(4, ""),
        "postscript": names.get(6, ""),
        "unitsPerEm": f["head"].unitsPerEm if "head" in f else None,
        "glyphs": len(f.getGlyphOrder()),
        "cmap": None,
    }
    try:
        info["cmap"] = set(f.getBestCmap())
    except Exception:
        pass
    return info


def system_font_for(family):
    """Is this family shipped by the OS? (filename heuristic, name table not read)"""
    key = re.sub(r"[^a-z0-9]", "", family.lower())
    if not key:
        return None
    for d in SYSTEM_FONT_DIRS.get(sys.platform, []):
        root = pathlib.Path(d)
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            stem = re.sub(r"[^a-z0-9]", "", p.stem.lower())
            if key in stem or stem in key:
                return str(p)
    return None


def _add(req, node_id, fam, style):
    entry = {"nodeId": node_id, "family": fam, "style": style or "Regular"}
    if entry not in req:
        req.append(entry)


def collect_required(manifest_path, plan_path):
    """(family, style) pairs the design uses. Per-run (plan) beats node-level."""
    per_node = {}
    if plan_path:
        plan = json.loads(pathlib.Path(plan_path).read_text(encoding="utf-8"))
        for t in plan.get("textPlans") or []:
            nid = t.get("nodeId")
            for r in t.get("runs") or []:
                fn = r.get("fontName")
                if isinstance(fn, dict) and fn.get("family"):
                    per_node.setdefault(nid, []).append((fn["family"], fn.get("style")))
    if manifest_path:
        m = json.loads(pathlib.Path(manifest_path).read_text(encoding="utf-8"))
        texts = m.get("texts") or m.get("nodes") or (m if isinstance(m, list) else [])
        for t in texts:
            if not isinstance(t, dict):
                continue
            nid = t.get("nodeId")
            if nid in per_node:
                continue  # plan already gave per-run truth
            runs = t.get("runs") or []
            for r in runs:
                fn = r.get("fontName")
                if isinstance(fn, dict) and fn.get("family"):
                    per_node.setdefault(nid, []).append((fn["family"], fn.get("style")))
            if nid in per_node:
                continue
            fn = t.get("fontName")
            if isinstance(fn, dict) and fn.get("family"):
                per_node.setdefault(nid, []).append((fn["family"], fn.get("style")))
            elif isinstance(fn, str) and fn == "mixed":
                per_node.setdefault(nid, []).append(("mixed", "mixed"))
    req = []
    for nid, pairs in per_node.items():
        seen = set()
        for fam, st in pairs:
            key = (fam.casefold() if fam != "mixed" else fam, (st or "").casefold())
            if key in seen:
                continue
            seen.add(key)
            _add(req, nid, fam, st)
    return req


def main():
    ap = argparse.ArgumentParser(description="Verify the design's fonts are landed in the project")
    ap.add_argument("--fonts-dir", required=True)
    ap.add_argument("--css", help="fonts.css that declares @font-face (default: <fonts-dir>/fonts.css)")
    ap.add_argument("--html", help="page.html the CSS gets inlined into (src base for inlined CSS)")
    ap.add_argument("--text-manifest", help="text-manifest.json from the export dir")
    ap.add_argument("--plan", help="layout-plan.json (has per-run fontName, beats the manifest)")
    ap.add_argument("--expect", action="append", default=[], help='extra "Family|Style" requirement')
    ap.add_argument("--glyphs", help='extra cmap check, e.g. "ABCabc0123"')
    ap.add_argument("--out", help="write the report JSON here")
    args = ap.parse_args()

    fonts_dir = pathlib.Path(args.fonts_dir).resolve()
    css_path = pathlib.Path(args.css).resolve() if args.css else fonts_dir / "fonts.css"
    bases = [css_path.parent]
    if args.html:
        bases.insert(0, pathlib.Path(args.html).resolve().parent)

    report = {"fontsDir": str(fonts_dir), "css": str(css_path),
              "srcBases": [str(b) for b in bases], "faces": [],
              "requirements": [], "gaps": [], "warnings": [], "ok": False}

    if not css_path.exists():
        print(f"GAP  css not found: {css_path}")
        print("     a font is never loaded just because a page writes its name; "
              "declare @font-face first (references/font-sourcing.md)")
        return 1

    faces = parse_font_faces(css_path)

    # --- 1/2/3: every @font-face needs at least one src that really exists ---
    for face in faces:
        face["resolved"] = []
        missing = []
        for src in face["srcs"]:
            cands, note = resolve_in_contexts(src, bases)
            if note:
                face["resolved"].append({"src": src, "note": note})
                continue
            hit = next((p for p in cands if p.exists()), None)
            rec = {"src": src, "exists": bool(hit),
                   "resolvedPath": str(hit) if hit else str(cands[0]),
                   "tried": [str(p) for p in cands]}
            if hit:
                rec["bytes"] = hit.stat().st_size
                rec["md5"] = hashlib.md5(hit.read_bytes()).hexdigest()
                try:
                    info = read_font_file(hit)
                    for k in ("family", "subfamily", "full", "postscript",
                              "unitsPerEm", "glyphs"):
                        rec[k] = info[k]
                    face.setdefault("_font", info)
                    face["_path"] = hit
                except Exception as e:
                    rec["error"] = f"{type(e).__name__}: {e}"
                    report["gaps"].append(
                        f"@font-face '{face['family']}' (css line {face['line']}): "
                        f"{hit.name} is not a readable font ({type(e).__name__})")
            else:
                missing.append(rec)
            face["resolved"].append(rec)
        # A src alias that does not exist is fine (browser falls through), but if
        # NOT ONE src of this face resolves, the face can never load -> gap.
        if missing and not face.get("_font"):
            report["gaps"].append(
                f"@font-face '{face['family']}' (css line {face['line']}): none of its "
                f"{len(missing)} src url(s) resolve to a file; tried "
                + "; ".join(missing[0]["tried"]))
        elif missing:
            report["warnings"].append(
                f"@font-face '{face['family']}': {len(missing)} alternate src alias(es) not "
                f"found (harmless: the browser falls through to the landed file)")

    # --- 4: requirements from the design -----------------------------------
    reqs = collect_required(args.text_manifest, args.plan)
    for e in args.expect:
        fam, _, st = e.partition("|")
        _add(reqs, None, fam.strip(), st.strip() or "Regular")

    for req in reqs:
        fam, st = req["family"], req["style"]
        entry = dict(req)
        if fam == "mixed":
            entry["status"] = "gap"
            entry["reason"] = "fontName=mixed and no per-run font resolved it (pass --plan)"
            report["gaps"].append(
                f"node {req.get('nodeId')}: fontName=mixed without per-run fonts -> "
                f"read layout-plan.json textPlans")
            report["requirements"].append(entry)
            continue
        want_w = style_weight(st)
        cand = [f for f in faces if f["family"].casefold() == fam.casefold()]
        if not cand:
            sysp = system_font_for(fam)
            if sysp:
                entry.update(status="system", systemPath=sysp,
                             reason="shipped by the OS, not embedded in the delivery")
                warn = (f"family '{fam}': provided by the OS ({sysp}); it is not embedded, "
                        f"so a machine without it will fall back")
                if warn not in report["warnings"]:
                    report["warnings"].append(warn)
                report["requirements"].append(entry)
            else:
                entry.update(status="gap", reason="no @font-face declares this family")
                report["gaps"].append(
                    f"family '{fam}' ({st}) used by {req.get('nodeId')}: no @font-face -- "
                    f"download it and declare @font-face")
            report["requirements"].append(entry)
            continue
        hit = None
        for f in cand:
            if want_w is None or style_weight(f["weight"]) == want_w:
                hit = f
                break
        if hit is None:
            got = ", ".join(f"{f['weight']}({style_weight(f['weight'])})" for f in cand)
            entry.update(status="gap",
                         reason=f"declared weights [{got}] do not cover style '{st}'")
            report["gaps"].append(
                f"family '{fam}': style '{st}' (weight {want_w}) not declared; have [{got}] "
                f"-- a wrong cut (e.g. ExtraBold for Regular) is a gap, not a pass")
            report["requirements"].append(entry)
            continue
        font = hit.get("_font")
        entry.update(faceLine=hit["line"], file=hit["_path"].name if hit.get("_path") else None)
        if font is None:
            entry.update(status="gap", reason="no readable font file behind this @font-face")
            report["requirements"].append(entry)
            continue
        file_fams = {x for x in (font["family"], font["full"], font["postscript"]) if x}
        fam_ok = any(fam.casefold() in str(x).casefold() or str(x).casefold() in fam.casefold()
                     for x in file_fams)
        sub_w = style_weight(font["subfamily"]) or style_weight(font["postscript"])
        entry.update(fontFamily=font["family"], fontStyle=font["subfamily"],
                     postscript=font["postscript"], md5=next(
                         (r.get("md5") for r in hit["resolved"]
                          if r.get("resolvedPath") == str(hit.get("_path"))), None))
        if not fam_ok:
            entry.update(status="gap",
                         reason=f"name table says {sorted(file_fams)}, design asks '{fam}'")
            report["gaps"].append(
                f"family '{fam}': {entry['file']} name table = {sorted(file_fams)} (mismatch)")
        elif want_w is not None and sub_w is not None and sub_w != want_w:
            entry.update(status="gap",
                         reason=f"file is {font['subfamily']} ({sub_w}), design wants {st} ({want_w})")
            report["gaps"].append(
                f"family '{fam}': {entry['file']} is {font['subfamily']} but design uses '{st}'")
        else:
            entry.update(status="ok")
        if args.glyphs and entry["status"] == "ok" and font.get("cmap"):
            missing = [c for c in args.glyphs if ord(c) not in font["cmap"]]
            if missing:
                entry.update(status="gap",
                             reason=f"cmap missing {len(missing)} chars: {''.join(missing[:20])}")
                report["gaps"].append(
                    f"family '{fam}': cmap lacks {len(missing)} requested glyphs")
        report["requirements"].append(entry)

    report["ok"] = not report["gaps"]

    # --- human-readable summary --------------------------------------------
    print(f"fonts dir : {fonts_dir}")
    print(f"css       : {css_path}  ({len(faces)} @font-face)")
    print(f"src bases : " + " | ".join(report["srcBases"]))
    for face in faces:
        for rec in face.get("resolved", []):
            if rec.get("exists"):
                print(f"  face '{face['family']}' w{face['weight']} -> "
                      f"{pathlib.Path(rec['resolvedPath']).name}  {rec['bytes']}B  "
                      f"md5 {rec['md5']}  name1='{rec.get('family')}' "
                      f"name2='{rec.get('subfamily')}' name6='{rec.get('postscript')}'"
                      + (f"  ERROR {rec['error']}" if rec.get("error") else ""))
            elif "note" not in rec:
                print(f"  face '{face['family']}' w{face['weight']} -> {rec['src']}  MISSING")
    seen_sys = set()
    for r in report["requirements"]:
        if r["status"] == "system":
            key = (r["family"], r["style"])
            if key in seen_sys:
                continue
            seen_sys.add(key)
            nodes = [n["nodeId"] for n in report["requirements"]
                     if n["status"] == "system" and (n["family"], n["style"]) == key and n.get("nodeId")]
            nodes = sorted(set(nodes))
            print(f"  SYS {r['family']} / {r['style']}  -> {r['systemPath']}"
                  + (f"  (nodes {', '.join(nodes)})" if nodes else ""))
            continue
        mark = {"ok": "OK  ", "gap": "GAP "}.get(r["status"], r["status"].upper())
        print(f"  {mark} {r['family']} / {r['style']}"
              + (f"  (node {r['nodeId']})" if r.get("nodeId") else "")
              + ("" if r["status"] == "ok" else f"  -> {r.get('reason')}"))

    if report["gaps"]:
        print(f"\n{len(report['gaps'])} gap(s). Do NOT ship a system-font fallback: "
              f"download the font (references/font-sourcing.md), land it, re-run.")
        for g in report["gaps"]:
            print(f"  - {g}")
    else:
        print("\nall required fonts are landed and self-consistent"
              + (f" ({len(report['warnings'])} portability warning(s))" if report["warnings"] else ""))
    for w in report["warnings"]:
        print(f"  ! {w}")

    if args.out:
        out = pathlib.Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"report -> {out}")

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
