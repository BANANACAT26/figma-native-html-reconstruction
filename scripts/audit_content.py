#!/usr/bin/env python3
"""Content-ledger and text-coverage audit (browser-free, deterministic).

Cross-checks three inputs:
  * manifest.json       -- assets, containerVisuals, contentLedger
  * text-manifest.json  -- effectively-visible TEXT nodes
  * built HTML          -- which data-n selectors actually rendered

Rules (generic, no node IDs or colors hardcoded):
  1. Every ledger entry has a disposition: rendered-* / skipped-invisible /
     skipped-transparent / unresolved. ``unresolved`` is a hard failure.
  2. Every rendered asset / container / text node MUST appear as a ``data-n``
     in the HTML. If it does not, the builder dropped it silently.
  3. Every effective-visible TEXT in text-manifest MUST appear in the HTML.
  4. A data-n in the HTML that no manifest entry declares is an orphan (soft
     warning, because legacy fixtures may add decorative nodes).
  5. Skipped nodes (invisible / transparent) must NOT appear in the HTML --
     that is "hidden over-render".

Machine-readable output goes to --out; --strict exits non-zero on any hard
failure so a pipeline can gate on it.
"""
import argparse, json, pathlib, re, sys

import visibility_rules as vr  # noqa: E402  (same scripts/ dir)


_DATA_N_RE = re.compile(r'data-n="([^"]+)"')
_DATA_CLIP_RE = re.compile(r'data-clip-for="([^"]+)"')
# Raw opening tag carrying a data-n attribute; used to tell a zero-visual
# marker div (class 'container-visual marker' / visibility:hidden) apart from
# a layer that actually paints pixels.
_DATA_TAG_RE = re.compile(r'<[^>]*data-n="([^"]+)"[^>]*>')


def extract_data_n(html_text):
    return set(_DATA_N_RE.findall(html_text))


def extract_clip_for(html_text):
    return set(_DATA_CLIP_RE.findall(html_text))


def extract_marker_ids(html_text):
    """data-n anchors whose opening tag renders zero pixels (markers).

    Per G-marker these are ledger anchors only: they must NOT be counted as
    painted containers, and an empty visual marker does not by itself make a
    content pass."""
    out = set()
    for m in _DATA_TAG_RE.finditer(html_text):
        if vr.is_marker_fragment(m.group(0)):
            out.add(m.group(1))
    return out


def audit(manifest, text_manifest, html_text):
    html_ids = extract_data_n(html_text)
    clip_ids = extract_clip_for(html_text)
    marker_ids = extract_marker_ids(html_text)

    rendered_assets = {a['nodeId'] for a in manifest.get('assets', [])
                       if a.get('asset') and a.get('effectiveVisible', True)}
    rendered_containers = {c['nodeId'] for c in manifest.get('containerVisuals', [])
                           if c.get('effectiveVisible', True)}
    rendered_texts = {t['nodeId'] for t in text_manifest.get('texts', [])
                      if t.get('effectiveVisible', True)}
    skipped_texts = {t['nodeId'] for t in text_manifest.get('texts', [])
                      if not t.get('effectiveVisible', True)}

    ledger = manifest.get('contentLedger', [])
    unresolved = [r for r in ledger if r.get('disposition') == 'unresolved']

    # Dispositions that do NOT need their own data-n in HTML:
    #  - rendered-stage: the artboard root itself is #stage.
    #  - rendered-by-ancestor: pixels are baked into an ancestor asset PNG;
    #    the audit verifies the ancestor's data-n is present instead.
    baked_nodes = [r for r in ledger if r.get('disposition') == 'rendered-by-ancestor']
    stage_nodes = [r for r in ledger if r.get('disposition') == 'rendered-stage']
    empty_skipped = [r for r in ledger if r.get('disposition') == 'skipped-empty-container']

    # Rule 2: independently rendered nodes must be in HTML.
    missing_in_html = []
    for nid in sorted(rendered_assets | rendered_containers | rendered_texts):
        if nid not in html_ids:
            missing_in_html.append(nid)

    # Rule 2b: baked descendants must have their ancestor asset in HTML.
    missing_ancestor = []
    for r in baked_nodes:
        anc = r.get('ancestorNodeId')
        if anc and anc not in html_ids and anc not in rendered_containers:
            # The ancestor is either an asset (must have data-n) or a container.
            if anc in rendered_assets and anc not in html_ids:
                missing_ancestor.append({'nodeId': r['nodeId'], 'ancestorNodeId': anc})

    # Rule 3: effective-visible texts must be in HTML.
    missing_texts = sorted(rendered_texts - html_ids)

    # Rule 4: orphans in HTML.
    declared = (rendered_assets | rendered_containers | rendered_texts | skipped_texts)
    orphans = sorted(html_ids - declared)

    # Rule 5: skipped texts must NOT be in HTML.
    over_rendered = sorted(skipped_texts & html_ids)

    # Rule 5b (DC-3): the same over-render ban applies to EVERY ledger row that
    # is not painted -- skipped-invisible / skipped-transparent / empty, plus
    # the compiler's non-painted dispositions. A hidden node with a data-n in
    # HTML means hidden content got rendered (or baked into a visible layer).
    skipped_ledger_ids = {
        r['nodeId'] for r in ledger
        if r.get('disposition') in vr.LEGACY_SKIPPED_DISPOSITIONS
        or r.get('disposition') in vr.NON_PAINTED_DISPOSITIONS
    }
    over_rendered_ledger = sorted(skipped_ledger_ids & html_ids)

    # G-marker: zero-visual marker divs are ledger anchors only. They are
    # excluded from the painted-container count -- an empty visual marker does
    # not make a content pass.
    painted_containers = rendered_containers - marker_ids

    # DC-5 baking conflicts (both directions).
    asset_ids = {a['nodeId'] for a in manifest.get('assets', [])}
    baked_rows = [r for r in ledger
                  if r.get('disposition') == 'rendered-by-ancestor']
    baked_desc_ids = {r['nodeId'] for r in baked_rows}
    baked_map = {}
    for r in baked_rows:
        anc = r.get('ancestorNodeId')
        if anc:
            baked_map.setdefault(anc, []).append(r['nodeId'])

    baking_conflicts = []
    for nid in vr.check_double_exposure(asset_ids, baked_desc_ids):
        baking_conflicts.append({
            'kind': vr.CONFLICT_BAKED_VISIBILITY, 'rule': 'BC-2',
            'nodeId': nid,
            'detail': 'node exported both as standalone asset AND baked '
                      'into another PNG (double exposure)'})
    baking_conflicts.extend(
        vr.find_hidden_baked_conflicts(skipped_ledger_ids, baked_map))

    # Ledger summary.
    ledger_summary = {
        'total': len(ledger),
        'rendered': sum(1 for r in ledger if str(r.get('disposition', '')).startswith('rendered')),
        'renderedStage': len(stage_nodes),
        'bakedByAncestor': len(baked_nodes),
        'skippedInvisible': sum(1 for r in ledger if r.get('disposition') == 'skipped-invisible'),
        'skippedTransparent': sum(1 for r in ledger if r.get('disposition') == 'skipped-transparent'),
        'skippedEmptyContainer': len(empty_skipped),
        'unresolved': len(unresolved),
    }

    result = {
        'htmlDataNCount': len(html_ids),
        'clipWrappedCount': len(clip_ids),
        'renderedAssets': len(rendered_assets),
        'renderedContainers': len(rendered_containers),
        'renderedTexts': len(rendered_texts),
        'skippedTexts': len(skipped_texts),
        'ledgerSummary': ledger_summary,
        'unresolved': unresolved,
        'missingInHtml': sorted(missing_in_html),
        'missingBakedAncestor': missing_ancestor,
        'missingTexts': missing_texts,
        'orphanDataN': orphans,
        'overRenderedSkipped': over_rendered,
        'overRenderedSkippedLedger': over_rendered_ledger,
        'markerAnchors': sorted(marker_ids),
        'renderedPaintedContainers': len(painted_containers),
        'bakingConflicts': baking_conflicts,
        'hardFailures': (
            len(unresolved) + len(missing_in_html) + len(missing_texts)
            + len(over_rendered) + len(missing_ancestor)
            + len(over_rendered_ledger) + len(baking_conflicts)
        ),
        'softWarnings': len(orphans),
    }
    return result


# ---------------------------------------------------------------------------
# real-browser audit: measured computed style / clip / mask / stage / pixels
# ---------------------------------------------------------------------------

# Hide every mapped node except the target while sampling pixels, so the
# screenshot contains ONLY the target's own paint (plus stage background).
# Without this, sibling/background pixels inside the clip region are wrongly
# counted as this node's paint. visibility:hidden is layout-invariant, so no
# geometry shifts.
_HIDE_OTHERS_JS = r"""(nodeId) => {
  document.querySelectorAll('[data-n]').forEach((el) => {
    if (el.getAttribute('data-n') !== nodeId) {
      el.dataset.__origVis = el.style.visibility || '';
      el.style.visibility = 'hidden';
    }
  });
}"""

_RESTORE_JS = r"""() => {
  document.querySelectorAll('[data-n]').forEach((el) => {
    if (el.dataset.__origVis !== undefined) {
      el.style.visibility = el.dataset.__origVis;
      delete el.dataset.__origVis;
    }
  });
}"""

_BROWSER_JS = r"""(stageSelector) => {
  const stage = document.querySelector(stageSelector);
  const sr = stage ? stage.getBoundingClientRect()
                   : {x:0, y:0, width: innerWidth, height: innerHeight};
  function intersect(a, b) {
    const x1 = Math.max(a.x, b.x), y1 = Math.max(a.y, b.y);
    const x2 = Math.min(a.x + a.width, b.x + b.width);
    const y2 = Math.min(a.y + a.height, b.y + b.height);
    return (x2 <= x1 || y2 <= y1)
      ? null : {x: x1, y: y1, width: x2 - x1, height: y2 - y1};
  }
  function ancestorClip(el) {
    // effective clip rect = element rect intersected with every ancestor that
    // establishes a clipping context (overflow != visible / clip-path).
    let acc = {x: el.getBoundingClientRect().x, y: el.getBoundingClientRect().y,
               width: el.getBoundingClientRect().width,
               height: el.getBoundingClientRect().height};
    let node = el.parentElement;
    while (node) {
      const cs = getComputedStyle(node);
      const clips = (cs.overflow !== 'visible' || cs.overflowX !== 'visible'
                     || cs.clipPath !== 'none');
      if (clips) acc = intersect(acc, node.getBoundingClientRect()) ||
                       {x:0, y:0, width:0, height:0};
      node = node.parentElement;
    }
    return acc;
  }
  const nodes = [];
  document.querySelectorAll('[data-n]').forEach((el) => {
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    const rect = {x: r.x, y: r.y, width: r.width, height: r.height};
    nodes.push({
      nodeId: el.getAttribute('data-n'),
      display: cs.display,
      visibility: cs.visibility,
      opacity: parseFloat(cs.opacity),
      rect: rect,
      stageIntersection: intersect(rect, sr),
      clipRect: ancestorClip(el),
      hasMask: (cs.maskImage && cs.maskImage !== 'none')
               || (cs.webkitMaskImage && cs.webkitMaskImage !== 'none'),
      naturalWidth: el.naturalWidth || null,
      naturalHeight: el.naturalHeight || null,
    });
  });
  const stageBg = stage ? getComputedStyle(stage).backgroundColor : 'rgb(255,255,255)';
  return {stageRect: {x: sr.x, y: sr.y, width: sr.width, height: sr.height},
          stageBg: stageBg, nodes: nodes};
}"""


def _parse_rgb(css_rgb):
    """'rgb(255, 0, 0)' -> (255, 0, 0); transparent -> (255,255,255)."""
    if not css_rgb or 'transparent' in css_rgb:
        return (255, 255, 255)
    nums = [int(t) for t in ''.join(ch for ch in css_rgb if ch.isdigit() or ch == ',').split(',') if t.strip()]
    if len(nums) >= 3:
        return tuple(nums[:3])
    return (255, 255, 255)


def _expected_row(nid, manifest, ledger, marker_ids):
    """Build the IR expectation for one mapped nodeId so the browser measurement
    can be gated (not just dumped). Unknown -> 'unknown' is never a pass."""
    row = next((r for r in ledger if r.get('nodeId') == nid), {})
    disp = row.get('disposition')
    eff = row.get('effectiveVisible')
    op = row.get('effectiveOpacity')
    if eff is None:
        for coll in (manifest.get('assets', []), manifest.get('containerVisuals', [])):
            hit = next((a for a in coll if a.get('nodeId') == nid), None)
            if hit is not None:
                eff = hit.get('effectiveVisible', True)
                op = hit.get('effectiveOpacity', op)
                break
    if eff is None:
        eff = True
    if nid in marker_ids:
        disp = disp or 'empty-wrapper'
    if op is None:
        op = 1.0
    outside = disp == 'out-of-stage'
    clip_area = 0.0 if disp == 'fully-clipped' else 1.0
    return {'nodeId': nid, 'disposition': disp or 'rendered-pixels-visible',
            'ctx': {'effectiveVisible': eff, 'computedOpacity': float(op),
                    'computedStageIntersection': {'outsideStage': outside},
                    'computedClipPath': {'visibleAreaPx': clip_area}}}, \
        disp in vr.NON_PAINTED_DISPOSITIONS


def run_browser_audit(html_path, manifest, text_manifest,
                      stage_selector='#stage', viewport_width=1000,
                      viewport_height=800):
    """Drive a real headless browser over the built HTML and collect, per mapped
    data-n node: computed display/visibility/opacity, ancestor clip rect,
    mask presence, size, stage intersection, and visible-pixel evidence
    (alpha>threshold pixel count inside the effective visible rect).

    Returns the browser-audit summary embedded in the result JSON. Every gate
    is pass/fail/unknown; unknown is never a silent pass (DC-4).
    """
    import io
    import urllib.parse
    from PIL import Image
    import numpy as np
    from playwright.sync_api import sync_playwright

    html_file = pathlib.Path(html_path).resolve()
    ledger = manifest.get('contentLedger', [])
    marker_ids = set()  # recomputed from raw HTML text
    html_text = html_file.read_text(encoding='utf-8')
    marker_ids = extract_marker_ids(html_text)

    collected = []
    errors = []
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception:
            browser = p.chromium.launch(headless=True, channel='chrome')
        ctx = browser.new_context(viewport={'width': viewport_width,
                                           'height': viewport_height},
                                  device_scale_factor=1)
        page = ctx.new_page()
        page.goto('file://' + urllib.parse.quote(str(html_file)),
                  wait_until='load', timeout=60000)
        measured = page.evaluate(_BROWSER_JS, stage_selector)
        stage = measured['stageRect']
        stage_bg = _parse_rgb(measured.get('stageBg'))

        for node in measured['nodes']:
            # Effective visible rect = stage intersection ∩ ancestor clip rect.
            vis = node['stageIntersection']
            clip = node['clipRect']
            region = vis
            if region is not None:
                region = vr.rect_intersection(region, clip) or \
                    {'x': 0, 'y': 0, 'width': 0, 'height': 0}
            pixel_count = 0
            if region and region['width'] > 0 and region['height'] > 0:
                clip_box = {
                    'x': max(0, int(round(region['x']))),
                    'y': max(0, int(round(region['y']))),
                    'width': min(viewport_width - max(0, int(round(region['x']))),
                                 int(round(region['width']))),
                    'height': min(viewport_height - max(0, int(round(region['y']))),
                                  int(round(region['height']))),
                }
                if clip_box['width'] > 0 and clip_box['height'] > 0:
                    page.evaluate(_HIDE_OTHERS_JS, node['nodeId'])
                    try:
                        png = page.screenshot(clip=clip_box)
                    finally:
                        page.evaluate(_RESTORE_JS)
                    # Pixel evidence = pixels that DIFFER from the stage's own
                    # background color, with all siblings hidden: this is the
                    # node's own paint only. Raw alpha is useless on an opaque
                    # page, and the region mode color IS the node's own paint.
                    im = Image.open(io.BytesIO(png)).convert('RGB')
                    arr = np.asarray(im).reshape(-1, 3).astype(int)
                    dist = np.sqrt(((arr - np.array(stage_bg)) ** 2).sum(axis=1))
                    pixel_count = int((dist > 40).sum())
            node['visiblePixelCount'] = pixel_count
            node['effectiveVisibleRect'] = region

            ir_row, _non_painted = _expected_row(node['nodeId'], manifest,
                                                  ledger, marker_ids)
            gate_res = vr.evaluate_browser_sample(ir_row, {
                'display': node['display'],
                'visibility': node['visibility'],
                'opacity': node['opacity'],
                'rect': node['rect'],
                'stageRect': stage,
                'clipRect': clip,
                'visiblePixelCount': pixel_count,
                'naturalWidth': node['naturalWidth'],
                'naturalHeight': node['naturalHeight'],
            })
            collected.append({'nodeId': node['nodeId'],
                              'measured': {k: node[k] for k in
                                  ('display', 'visibility', 'opacity', 'rect',
                                   'stageIntersection', 'clipRect', 'hasMask',
                                   'naturalWidth', 'naturalHeight',
                                   'visiblePixelCount')},
                              'gates': gate_res['gates']})
        browser.close()

    all_gates = [g for c in collected for g in c['gates']]
    agg = vr.aggregate_gates(all_gates)
    return {
        'stageRect': stage,
        'nodeCount': len(collected),
        'nodes': collected,
        'gateSummary': {'pass': agg['pass'], 'fail': agg['fail'],
                        'unknown': agg['unknown']},
        'allPass': agg['allPass'],
        'strictBlocked': agg['strictBlocked'],
        'fails': agg['fails'],
        'unknowns': agg['unknowns'],
        'errors': errors,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--export-dir', required=True,
                    help='directory containing manifest.json and text-manifest.json')
    ap.add_argument('--html', required=True, help='built HTML file')
    ap.add_argument('--out', required=True)
    ap.add_argument('--strict', action='store_true',
                    help='non-zero exit when hardFailures > 0')
    ap.add_argument('--browser-audit', action='store_true',
                    help='drive headless Chromium and collect per-node computed '
                         'display/visibility/opacity, ancestor clip, mask, size, '
                         'stage intersection and visible-pixel evidence')
    args = ap.parse_args()

    ed = pathlib.Path(args.export_dir)
    manifest = json.loads((ed / 'manifest.json').read_text(encoding='utf-8'))
    text_manifest = json.loads((ed / 'text-manifest.json').read_text(encoding='utf-8'))
    html_text = pathlib.Path(args.html).read_text(encoding='utf-8')

    result = audit(manifest, text_manifest, html_text)
    if args.browser_audit:
        result['browserAudit'] = run_browser_audit(args.html, manifest,
                                                   text_manifest)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: result[k] for k in
                      ('htmlDataNCount', 'renderedAssets', 'renderedContainers',
                       'renderedTexts', 'skippedTexts', 'hardFailures', 'softWarnings')},
                     ensure_ascii=False))
    blocked = result['hardFailures'] > 0
    if args.browser_audit and not result['browserAudit']['allPass']:
        blocked = True
        print('browserAudit: NOT all-pass '
              + json.dumps(result['browserAudit']['gateSummary'],
                           ensure_ascii=False))
    if args.strict and blocked:
        sys.exit(2)


if __name__ == '__main__':
    main()
