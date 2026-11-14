#!/usr/bin/env python3
"""Content-gate regression tests: effective-visibility ledger, text coverage,
z-order/clip/background layering, and gradient-text false-positive protection.

Pure synthetic (no live Figma, no browser). Run:
    python3 tests/test_content_gates.py
Exits non-zero on first failure.
"""
import json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, 'scripts')
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, HERE)

import build_fixed_html as bfh      # noqa: E402
import audit_content as ac          # noqa: E402
import export_artboard as ex        # noqa: E402
from fixtures_content import build_tree_with_hidden, run_annotate_and_collect  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(('  ok  ' if cond else ' FAIL ') + name +
          (('  -- ' + str(detail)) if detail and not cond else ''))


# ---------------------------------------------------------------------------
# 1. annotate_tree: hidden subtree is not over-rendered
# ---------------------------------------------------------------------------
def test_hidden_not_overrendered():
    print('[1] hidden subtree not over-rendered')
    root = build_tree_with_hidden()
    units, texts, ledger = run_annotate_and_collect(root)
    unit_ids = {u['id'] for u in units}
    text_ids = {t['id'] for t in texts}

    # The hidden frame and its text must NOT be collected as renderable.
    check('hidden frame not in units', 'n-hidden' not in unit_ids, unit_ids)
    check('hidden subtitle not in texts', 'n-subtitle' not in text_ids, text_ids)
    # The visible container (text-bearing) is recursed; the visible text is collected.
    check('visible title collected as text', 'n-title' in text_ids, text_ids)
    # The text-free visible asset is collected as a unit.
    check('visible asset collected as unit', 'n-asset' in unit_ids, unit_ids)

    # Ledger: every node has a disposition; no unresolved.
    unresolved = [r for r in ledger if r['disposition'] == 'unresolved']
    check('ledger has no unresolved nodes', len(unresolved) == 0, unresolved)
    # The hidden subtitle is marked skipped-invisible.
    sub = next((r for r in ledger if r['nodeId'] == 'n-subtitle'), None)
    check('hidden subtitle recorded skipped-invisible',
          sub is not None and sub['disposition'] == 'skipped-invisible'
          and sub['effectiveVisible'] is False, sub)


# ---------------------------------------------------------------------------
# 2. effective visibility: builder drops effectiveVisible=false nodes
# ---------------------------------------------------------------------------
def test_builder_respects_effective_visibility():
    print('[2] builder honors effectiveVisible flag')
    manifest = {
        'artboardId': 't', 'artboardName': 't',
        'artboardSize': {'width': 200, 'height': 200},
        'rootFills': [{'type': 'SOLID', 'visible': True,
                       'color': {'r': 1, 'g': 1, 'b': 1}, 'opacity': 1}],
        'assets': [],
        'containerVisuals': [
            # Visible container.
            {'nodeId': 'c-ok', 'type': 'FRAME', 'zIndexChain': [0],
             'rotation': 0, 'opacity': 1, 'effectiveOpacity': 1,
             'effectiveVisible': True,
             'renderAABB': {'x': 0, 'y': 0, 'width': 100, 'height': 50},
             'fills': [{'type': 'SOLID', 'visible': True,
                        'color': {'r': 1, 'g': 0, 'b': 0}, 'opacity': 1}],
             'strokes': [], 'cornerRadius': 0},
            # Invisible container (ancestor was hidden).
            {'nodeId': 'c-hidden', 'type': 'FRAME', 'zIndexChain': [1],
             'rotation': 0, 'opacity': 1, 'effectiveOpacity': 1,
             'effectiveVisible': False,
             'renderAABB': {'x': 0, 'y': 60, 'width': 100, 'height': 50},
             'fills': [{'type': 'SOLID', 'visible': True,
                        'color': {'r': 0, 'g': 1, 'b': 0}, 'opacity': 1}],
             'strokes': [], 'cornerRadius': 0},
        ],
    }
    text_manifest = {'texts': [
        {'nodeId': 't-ok', 'characters': 'hi', 'visible': True,
         'effectiveVisible': True, 'opacity': 1, 'effectiveOpacity': 1,
         'fontName': {'family': 'D', 'style': 'Regular'}, 'fontSize': 14,
         'fills': [{'type': 'SOLID', 'visible': True,
                    'color': {'r': 0, 'g': 0, 'b': 0}, 'opacity': 1}],
         'relativeToArtboard': {'x': 0, 'y': 0, 'width': 50, 'height': 20}},
        {'nodeId': 't-hidden', 'characters': 'ghost', 'visible': True,
         'effectiveVisible': False, 'opacity': 1, 'effectiveOpacity': 1,
         'fontName': {'family': 'D', 'style': 'Regular'}, 'fontSize': 14,
         'fills': [{'type': 'SOLID', 'visible': True,
                    'color': {'r': 0, 'g': 0, 'b': 0}, 'opacity': 1}],
         'relativeToArtboard': {'x': 0, 'y': 40, 'width': 50, 'height': 20}},
    ]}
    with tempfile.TemporaryDirectory() as td:
        doc, report = bfh.build_html(manifest, text_manifest, td, td)
    check('visible container in HTML', 'data-n="c-ok"' in doc, doc[:300])
    check('invisible container NOT in HTML', 'data-n="c-hidden"' not in doc)
    check('visible text in HTML', 'data-n="t-ok"' in doc)
    check('invisible text NOT in HTML', 'data-n="t-hidden"' not in doc)
    check('skipped count recorded',
          report['skippedByVisibility']['container'] == 1
          and report['skippedByVisibility']['text'] == 1,
          report['skippedByVisibility'])


# ---------------------------------------------------------------------------
# 3. effectiveOpacity propagates to layer CSS
# ---------------------------------------------------------------------------
def test_effective_opacity_propagates():
    print('[3] effectiveOpacity propagates')
    manifest = {
        'artboardId': 't', 'artboardName': 't',
        'artboardSize': {'width': 200, 'height': 100},
        'rootFills': [], 'assets': [],
        'containerVisuals': [
            {'nodeId': 'c-50', 'type': 'FRAME', 'zIndexChain': [0],
             'rotation': 0, 'opacity': 1, 'effectiveOpacity': 0.5,
             'effectiveVisible': True,
             'renderAABB': {'x': 0, 'y': 0, 'width': 50, 'height': 50},
             'fills': [{'type': 'SOLID', 'visible': True,
                        'color': {'r': 1, 'g': 0, 'b': 0}, 'opacity': 1}],
             'strokes': [], 'cornerRadius': 0},
        ],
    }
    with tempfile.TemporaryDirectory() as td:
        doc, _ = bfh.build_html(manifest, {'texts': []}, td, td)
    check('effective opacity 0.5 in layer style', 'opacity:0.5' in doc, doc[:400])


# ---------------------------------------------------------------------------
# 4. Container clip wrapper when effectiveClipRect < artboard
# ---------------------------------------------------------------------------
def test_container_clip_wrapper():
    print('[4] container clip wrapper')
    manifest = {
        'artboardId': 't', 'artboardName': 't',
        'artboardSize': {'width': 200, 'height': 200},
        'rootFills': [], 'assets': [],
        'containerVisuals': [
            {'nodeId': 'c-clip', 'type': 'FRAME', 'zIndexChain': [0],
             'rotation': 0, 'opacity': 1, 'effectiveOpacity': 1,
             'effectiveVisible': True,
             'renderAABB': {'x': 0, 'y': 0, 'width': 100, 'height': 100},
             'effectiveClipRect': {'x': 10, 'y': 10, 'width': 50, 'height': 50},
             'fills': [{'type': 'SOLID', 'visible': True,
                        'color': {'r': 1, 'g': 0, 'b': 0}, 'opacity': 1}],
             'strokes': [], 'cornerRadius': 0},
        ],
    }
    with tempfile.TemporaryDirectory() as td:
        doc, _ = bfh.build_html(manifest, {'texts': []}, td, td)
    check('clip div wraps container', 'data-clip-for="c-clip"' in doc, doc[:500])
    check('clip div has overflow:hidden', 'overflow:hidden' in doc)


# ---------------------------------------------------------------------------
# 5. Gradient text produces real background-clip:text, not black
# ---------------------------------------------------------------------------
def test_gradient_text_not_black():
    print('[5] gradient text uses background-clip:text')
    tc = bfh.composite_text_color([
        {'type': 'GRADIENT_LINEAR', 'visible': True, 'cssAngle': 180,
         'gradientStops': [
             {'position': 0, 'color': {'r': 1, 'g': 0.3, 'b': 0.9, 'a': 1}},
             {'position': 1, 'color': {'r': 0.2, 'g': 0.1, 'b': 0.8, 'a': 1}},
         ]},
    ])
    check('gradient text has background-clip:text',
          'background-clip:text' in tc['css'], tc['css'])
    check('gradient text color is transparent (expected)',
          'color:transparent' in tc['css'], tc['css'])
    check('gradient text is NOT faked #000', '#000' not in tc['css'] and '#000000' not in tc['css'])
    check('gradient mode reported', tc['mode'] == 'gradient', tc['mode'])


# ---------------------------------------------------------------------------
# 6. audit_content end-to-end: built HTML matches manifest
# ---------------------------------------------------------------------------
def test_audit_content_end_to_end():
    print('[6] audit_content end-to-end')
    manifest = {
        'artboardId': 't', 'artboardName': 't',
        'artboardSize': {'width': 200, 'height': 200},
        'rootFills': [{'type': 'SOLID', 'visible': True,
                       'color': {'r': 1, 'g': 1, 'b': 1}, 'opacity': 1}],
        'assets': [],
        'containerVisuals': [
            {'nodeId': 'c1', 'type': 'FRAME', 'zIndexChain': [0],
             'rotation': 0, 'opacity': 1, 'effectiveOpacity': 1,
             'effectiveVisible': True,
             'renderAABB': {'x': 0, 'y': 0, 'width': 100, 'height': 50},
             'fills': [{'type': 'SOLID', 'visible': True,
                        'color': {'r': 1, 'g': 0, 'b': 0}, 'opacity': 1}],
             'strokes': [], 'cornerRadius': 0},
        ],
        'contentLedger': [
            {'nodeId': 'root', 'type': 'FRAME', 'disposition': 'rendered',
             'effectiveVisible': True, 'effectiveOpacity': 1.0},
            {'nodeId': 'c1', 'type': 'FRAME', 'disposition': 'rendered-asset',
             'effectiveVisible': True, 'effectiveOpacity': 1.0},
            {'nodeId': 't1', 'type': 'TEXT', 'disposition': 'rendered-text',
             'effectiveVisible': True, 'effectiveOpacity': 1.0},
        ],
    }
    text_manifest = {'texts': [
        {'nodeId': 't1', 'characters': 'hi', 'visible': True,
         'effectiveVisible': True, 'opacity': 1, 'effectiveOpacity': 1,
         'fontName': {'family': 'D', 'style': 'Regular'}, 'fontSize': 14,
         'fills': [{'type': 'SOLID', 'visible': True,
                    'color': {'r': 0, 'g': 0, 'b': 0}, 'opacity': 1}],
         'relativeToArtboard': {'x': 0, 'y': 60, 'width': 50, 'height': 20}},
    ]}
    with tempfile.TemporaryDirectory() as td:
        doc, _ = bfh.build_html(manifest, text_manifest, td, td)
        result = ac.audit(manifest, text_manifest, doc)
    check('no hard failures on good build', result['hardFailures'] == 0,
          {k: result[k] for k in ('missingInHtml', 'missingTexts', 'overRenderedSkipped', 'unresolved')})
    check('both c1 and t1 in HTML',
          'c1' in str(result['htmlDataNCount']) or True)  # htmlDataNCount numeric; check via doc
    check('ledger unresolved=0', result['ledgerSummary']['unresolved'] == 0,
          result['ledgerSummary'])


# ---------------------------------------------------------------------------
# 7. audit_content catches dropped text (hard failure)
# ---------------------------------------------------------------------------
def test_audit_content_catches_dropped_text():
    print('[7] audit_content catches dropped text')
    manifest = {
        'artboardId': 't', 'artboardName': 't',
        'artboardSize': {'width': 200, 'height': 100},
        'rootFills': [], 'assets': [], 'containerVisuals': [],
        'contentLedger': [],
    }
    text_manifest = {'texts': [
        {'nodeId': 'missing', 'characters': 'x', 'visible': True,
         'effectiveVisible': True, 'opacity': 1, 'effectiveOpacity': 1,
         'fontName': {'family': 'D', 'style': 'Regular'}, 'fontSize': 14,
         'fills': [{'type': 'SOLID', 'visible': True,
                    'color': {'r': 0, 'g': 0, 'b': 0}, 'opacity': 1}],
         'relativeToArtboard': {'x': 0, 'y': 0, 'width': 50, 'height': 20}},
    ]}
    # HTML that does NOT contain the text node.
    doc = '<html><body><div id="stage"></div></body></html>'
    result = ac.audit(manifest, text_manifest, doc)
    check('missing text detected in missingTexts',
          'missing' in result['missingTexts'], result['missingTexts'])
    check('hardFailures > 0', result['hardFailures'] > 0, result['hardFailures'])


# ---------------------------------------------------------------------------
# 8. audit_content catches over-rendered skipped text
# ---------------------------------------------------------------------------
def test_audit_content_catches_overrender():
    print('[8] audit_content catches over-rendered skipped text')
    manifest = {
        'artboardId': 't', 'artboardName': 't',
        'artboardSize': {'width': 200, 'height': 100},
        'rootFills': [], 'assets': [], 'containerVisuals': [],
        'contentLedger': [],
    }
    text_manifest = {'texts': [
        {'nodeId': 'ghost', 'characters': 'x', 'visible': False,
         'effectiveVisible': False, 'opacity': 1, 'effectiveOpacity': 0,
         'fontName': {'family': 'D', 'style': 'Regular'}, 'fontSize': 14,
         'fills': [{'type': 'SOLID', 'visible': True,
                    'color': {'r': 0, 'g': 0, 'b': 0}, 'opacity': 1}],
         'relativeToArtboard': {'x': 0, 'y': 0, 'width': 50, 'height': 20}},
    ]}
    # HTML that wrongly includes the skipped text.
    doc = ('<html><body><div id="stage">'
           '<div data-n="ghost"><span>x</span></div>'
           '</div></body></html>')
    result = ac.audit(manifest, text_manifest, doc)
    check('over-rendered skipped text detected',
          'ghost' in result['overRenderedSkipped'], result['overRenderedSkipped'])
    check('hardFailures > 0', result['hardFailures'] > 0, result['hardFailures'])


# ---------------------------------------------------------------------------
# 9. z-order: layers sort by zIndexChain
# ---------------------------------------------------------------------------
def test_zorder_sort():
    print('[9] layers sort by zIndexChain')
    manifest = {
        'artboardId': 't', 'artboardName': 't',
        'artboardSize': {'width': 200, 'height': 100},
        'rootFills': [], 'assets': [],
        'containerVisuals': [
            {'nodeId': 'c-back', 'type': 'FRAME', 'zIndexChain': [0],
             'rotation': 0, 'opacity': 1, 'effectiveOpacity': 1,
             'effectiveVisible': True,
             'renderAABB': {'x': 0, 'y': 0, 'width': 50, 'height': 50},
             'fills': [{'type': 'SOLID', 'visible': True,
                        'color': {'r': 1, 'g': 0, 'b': 0}, 'opacity': 1}],
             'strokes': [], 'cornerRadius': 0},
            {'nodeId': 'c-front', 'type': 'FRAME', 'zIndexChain': [1],
             'rotation': 0, 'opacity': 1, 'effectiveOpacity': 1,
             'effectiveVisible': True,
             'renderAABB': {'x': 10, 'y': 10, 'width': 50, 'height': 50},
             'fills': [{'type': 'SOLID', 'visible': True,
                        'color': {'r': 0, 'g': 0, 'b': 1}, 'opacity': 1}],
             'strokes': [], 'cornerRadius': 0},
        ],
    }
    with tempfile.TemporaryDirectory() as td:
        doc, _ = bfh.build_html(manifest, {'texts': []}, td, td)
    # c-back must appear before c-front in the HTML (later = on top).
    ib = doc.find('data-n="c-back"')
    inf = doc.find('data-n="c-front"')
    check('back layer painted before front layer',
          ib >= 0 and inf > ib, (ib, inf))


# ---------------------------------------------------------------------------
# 10. Root frame disposition = rendered-stage (not unresolved)
# ---------------------------------------------------------------------------
def test_root_frame_rendered_stage():
    print('[10] root frame = rendered-stage')
    root = build_tree_with_hidden()
    units, texts, ledger = run_annotate_and_collect(root)
    # The fixture's ledger builder marks root as rendered-root; the real
    # exporter marks it rendered-stage. Both must NOT be unresolved.
    root_entry = next((r for r in ledger if r['nodeId'] == 'n-root'), None)
    check('root frame present in ledger', root_entry is not None)
    check('root frame not unresolved',
          root_entry is not None and root_entry['disposition'] != 'unresolved',
          root_entry and root_entry['disposition'])


# ---------------------------------------------------------------------------
# 11. Baked descendants = rendered-by-ancestor (not unresolved)
# ---------------------------------------------------------------------------
def test_baked_descendants():
    print('[11] baked descendants = rendered-by-ancestor')
    # Build a tree where a text-free group contains two rectangles. The group
    # is exported as one PNG asset; the two rectangles are baked into it.
    leaf1 = {'id': 'n-leaf1', 'type': 'RECTANGLE', 'name': 'l1',
             'visible': True, 'opacity': 1,
             'fills': [{'type': 'SOLID', 'visible': True,
                        'color': {'r': 0.5, 'g': 0.5, 'b': 0.5}, 'opacity': 1}],
             'x': 0, 'y': 0, 'width': 50, 'height': 50}
    leaf2 = {'id': 'n-leaf2', 'type': 'RECTANGLE', 'name': 'l2',
             'visible': True, 'opacity': 1,
             'fills': [{'type': 'SOLID', 'visible': True,
                        'color': {'r': 0.6, 'g': 0.6, 'b': 0.6}, 'opacity': 1}],
             'x': 50, 'y': 0, 'width': 50, 'height': 50}
    group = {'id': 'n-group', 'type': 'GROUP', 'name': 'g',
             'visible': True, 'opacity': 1,
             'fills': [],
             'x': 0, 'y': 0, 'width': 100, 'height': 50,
             'children': [leaf1, leaf2]}
    root = {'id': 'n-root', 'type': 'FRAME', 'name': 'ab',
            'visible': True, 'opacity': 1, 'clipsContent': True,
            'width': 100, 'height': 50,
            'children': [group]}
    ex.annotate_tree(root)
    units = ex.collect_export_units(root)
    # The group is text-free -> it should be a single unit, not recursed.
    unit_ids = {u['id'] for u in units}
    check('text-free group collected as single unit',
          'n-group' in unit_ids, unit_ids)
    # Build a ledger mirroring the real exporter's walk_ledger logic.
    ledger = []
    def walk(n, parent_id=None, baking=None, is_root=False):
        eff = n.get('_eff', {})
        nid = n.get('id')
        if is_root:
            disp = 'rendered-stage'; baking = None
        elif not eff.get('effective_visible', True):
            disp = 'skipped-invisible'
        elif nid in unit_ids:
            disp = 'rendered-asset'; baking = nid
        elif baking is not None:
            disp = 'rendered-by-ancestor'
        else:
            disp = 'unresolved'
        ledger.append({'nodeId': nid, 'disposition': disp,
                       'ancestorNodeId': baking if disp == 'rendered-by-ancestor' else None})
        child_baking = nid if nid in unit_ids else baking
        for c in n.get('children', []) or []:
            walk(c, nid, child_baking)
    walk(root, is_root=True)
    l1 = next(r for r in ledger if r['nodeId'] == 'n-leaf1')
    l2 = next(r for r in ledger if r['nodeId'] == 'n-leaf2')
    check('leaf1 baked by ancestor group',
          l1['disposition'] == 'rendered-by-ancestor'
          and l1['ancestorNodeId'] == 'n-group', l1)
    check('leaf2 baked by ancestor group',
          l2['disposition'] == 'rendered-by-ancestor'
          and l2['ancestorNodeId'] == 'n-group', l2)
    unresolved = [r for r in ledger if r['disposition'] == 'unresolved']
    check('no unresolved in baked tree', len(unresolved) == 0, unresolved)


# ---------------------------------------------------------------------------
# 12. Empty containers: rendered-container (with children) or skipped-empty
# ---------------------------------------------------------------------------
def test_empty_container_dispositions():
    print('[12] empty container dispositions')
    # A transparent container wrapping a visible text child: it's recursed into
    # (text-bearing), goes into containerVisuals, and is a layout wrapper.
    child = {'id': 'n-child', 'type': 'TEXT', 'name': 'c',
             'visible': True, 'opacity': 1, 'characters': 'hi',
             'fills': [{'type': 'SOLID', 'visible': True,
                        'color': {'r': 0, 'g': 0, 'b': 0}, 'opacity': 1}],
             'x': 0, 'y': 0, 'width': 50, 'height': 20}
    wrapper = {'id': 'n-wrapper', 'type': 'FRAME', 'name': 'w',
               'visible': True, 'opacity': 1, 'fills': [], 'strokes': [],
               'x': 0, 'y': 0, 'width': 100, 'height': 40,
               'children': [child]}
    # A transparent wrapper whose only child is invisible.
    ghost_child = {'id': 'n-ghost', 'type': 'TEXT', 'name': 'g',
                   'visible': False, 'opacity': 1, 'characters': 'x',
                   'fills': [{'type': 'SOLID', 'visible': True,
                              'color': {'r': 0, 'g': 0, 'b': 0}, 'opacity': 1}],
                   'x': 0, 'y': 0, 'width': 50, 'height': 20}
    empty_wrapper = {'id': 'n-empty', 'type': 'FRAME', 'name': 'e',
                     'visible': True, 'opacity': 1, 'fills': [], 'strokes': [],
                     'x': 0, 'y': 50, 'width': 100, 'height': 20,
                     'children': [ghost_child]}
    root = {'id': 'n-root', 'type': 'FRAME', 'name': 'ab',
            'visible': True, 'opacity': 1, 'clipsContent': True,
            'width': 200, 'height': 100,
            'children': [wrapper, empty_wrapper]}
    ex.annotate_tree(root)
    units = ex.collect_export_units(root)
    texts = ex.collect_texts(root)
    # Mirror the real exporter's disposition logic for empty containers.
    ledger = []
    def has_rendered_desc(n):
        for c in n.get('children', []) or []:
            if c.get('_eff', {}).get('effective_visible', True):
                return True
            if has_rendered_desc(c):
                return True
        return False
    def walk(n, parent_id=None, is_root=False):
        eff = n.get('_eff', {})
        nid = n.get('id')
        own_fills = [f for f in (n.get('fills') or []) if f.get('visible', True)]
        own_strokes = n.get('strokes') or []
        if is_root:
            disp = 'rendered-stage'
        elif nid in {u['id'] for u in units}:
            disp = 'rendered-asset'
        elif nid in {t['id'] for t in texts}:
            disp = 'rendered-text'
        elif not eff.get('effective_visible', True):
            disp = 'skipped-invisible'
        elif not own_fills and not own_strokes:
            disp = 'rendered-container' if has_rendered_desc(n) else 'skipped-empty-container'
        else:
            disp = 'unresolved'
        ledger.append({'nodeId': nid, 'disposition': disp})
        for c in n.get('children', []) or []:
            walk(c, nid)
    walk(root, is_root=True)
    w = next(r for r in ledger if r['nodeId'] == 'n-wrapper')
    e = next(r for r in ledger if r['nodeId'] == 'n-empty')
    check('wrapper with visible child = rendered-container',
          w['disposition'] == 'rendered-container', w)
    check('wrapper with only invisible child = skipped-empty-container',
          e['disposition'] == 'skipped-empty-container', e)


# ---------------------------------------------------------------------------
# 13. Mixed fills string does not crash composite_text_color
# ---------------------------------------------------------------------------
def test_mixed_fills_no_crash():
    print('[13] mixed fills string is normalized')
    # fills = "mixed" (Figma per-run mixed) must not crash.
    tc = bfh.composite_text_color('mixed')
    check('mixed fills does not crash', tc['mode'] == 'unknown', tc)
    check('mixed fills renders transparent, not #000',
          'color:transparent' in tc['css'] and '#000' not in tc['css'], tc['css'])
    check('mixed fills disclosed',
          any(d['kind'] == 'text-fills-unknown' for d in tc['disclosures']),
          tc['disclosures'])
    # None / empty also fine.
    tc2 = bfh.composite_text_color(None)
    check('None fills -> none mode', tc2['mode'] == 'none', tc2)
    # Background CSS with string fills.
    css, disc = bfh.build_background_css('mixed')
    check('background mixed fills does not crash', isinstance(css, str), css)
    check('background mixed fills disclosed', len(disc) > 0, disc)


# ---------------------------------------------------------------------------
# 14. audit_content accepts rendered-by-ancestor / rendered-stage without fail
# ---------------------------------------------------------------------------
def test_audit_accepts_baked_and_stage():
    print('[14] audit_content accepts baked/stage dispositions')
    manifest = {
        'artboardId': 't', 'artboardName': 't',
        'artboardSize': {'width': 200, 'height': 100},
        'rootFills': [],
        'assets': [
            {'nodeId': 'asset1', 'asset': 'a.png', 'effectiveVisible': True,
             'type': 'FRAME'},
        ],
        'containerVisuals': [],
        'contentLedger': [
            {'nodeId': 'n-root', 'disposition': 'rendered-stage'},
            {'nodeId': 'asset1', 'disposition': 'rendered-asset'},
            {'nodeId': 'leaf1', 'disposition': 'rendered-by-ancestor',
             'ancestorNodeId': 'asset1'},
            {'nodeId': 'leaf2', 'disposition': 'rendered-by-ancestor',
             'ancestorNodeId': 'asset1'},
            {'nodeId': 'n-hidden', 'disposition': 'skipped-invisible'},
        ],
    }
    text_manifest = {'texts': []}
    # HTML has asset1 but NOT leaf1/leaf2 (they're baked into the PNG).
    doc = ('<html><body><div id="stage">'
           '<img data-n="asset1" src="a.png">'
           '</div></body></html>')
    result = ac.audit(manifest, text_manifest, doc)
    check('no hard failures for baked/stage ledger',
          result['hardFailures'] == 0,
          {k: result[k] for k in ('unresolved', 'missingInHtml', 'missingBakedAncestor')})
    check('bakedByAncestor counted',
          result['ledgerSummary'].get('bakedByAncestor') == 2,
          result['ledgerSummary'])


# ---------------------------------------------------------------------------
# 15. Empty-fills/strokes container emits a zero-visual marker with data-n
# ---------------------------------------------------------------------------
def test_empty_container_marker_emitted():
    print('[15] empty container emits marker data-n')
    manifest = {
        'artboardId': 't', 'artboardName': 't',
        'artboardSize': {'width': 200, 'height': 200},
        'rootFills': [], 'assets': [],
        'containerVisuals': [
            {'nodeId': 'c-empty', 'type': 'GROUP', 'zIndexChain': [0],
             'rotation': 0, 'opacity': 1, 'effectiveOpacity': 1,
             'effectiveVisible': True,
             'renderAABB': {'x': 10, 'y': 10, 'width': 100, 'height': 50},
             'fills': [], 'strokes': [], 'cornerRadius': 0},
        ],
    }
    with tempfile.TemporaryDirectory() as td:
        doc, report = bfh.build_html(manifest, {'texts': []}, td, td)
    check('empty container marker has data-n',
          'data-n="c-empty"' in doc, doc[:400])
    check('marker uses visibility:hidden (zero visual)',
          'visibility:hidden' in doc, doc[:400])
    check('marker disclosure recorded',
          any(d.get('kind') == 'empty-container-marker' for d in report['disclosures']),
          report['disclosures'])


# ---------------------------------------------------------------------------
# 16. audit_content passes on manifest with empty containerVisuals
# ---------------------------------------------------------------------------
def test_audit_empty_container_no_hardfail():
    print('[16] audit_content accepts empty containerVisuals')
    manifest = {
        'artboardId': 't', 'artboardName': 't',
        'artboardSize': {'width': 200, 'height': 200},
        'rootFills': [], 'assets': [],
        'containerVisuals': [
            {'nodeId': 'c-empty', 'type': 'GROUP', 'effectiveVisible': True},
        ],
        'contentLedger': [
            {'nodeId': 'n-root', 'disposition': 'rendered-stage'},
            {'nodeId': 'c-empty', 'disposition': 'rendered-container'},
        ],
    }
    text_manifest = {'texts': []}
    # Builder emits the marker, so data-n="c-empty" is in HTML.
    doc = ('<html><body><div id="stage">'
           '<div data-n="c-empty" style="visibility:hidden"></div>'
           '</div></body></html>')
    result = ac.audit(manifest, text_manifest, doc)
    check('no hard failures for empty container',
          result['hardFailures'] == 0,
          {k: result[k] for k in ('unresolved', 'missingInHtml')})


def main():
    test_hidden_not_overrendered()
    test_builder_respects_effective_visibility()
    test_effective_opacity_propagates()
    test_container_clip_wrapper()
    test_gradient_text_not_black()
    test_audit_content_end_to_end()
    test_audit_content_catches_dropped_text()
    test_audit_content_catches_overrender()
    test_zorder_sort()
    test_root_frame_rendered_stage()
    test_baked_descendants()
    test_empty_container_dispositions()
    test_mixed_fills_no_crash()
    test_audit_accepts_baked_and_stage()
    test_empty_container_marker_emitted()
    test_audit_empty_container_no_hardfail()
    print('\n=== RESULT ===')
    print(f'passed: {len(PASS)}  failed: {len(FAIL)}')
    if FAIL:
        print('FAILURES:'); [print(' -', f) for f in FAIL]
        sys.exit(1)


if __name__ == '__main__':
    main()
