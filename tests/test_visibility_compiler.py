#!/usr/bin/env python3
"""Design Compiler visibility-module regression tests (pure synthetic).

Exercises scripts/visibility_rules.py and the audit_content integration:
self-hidden / ancestor-hidden / opacity-below-threshold / fully-clipped /
hidden text / visible sibling preserved / baked-visibility-conflict /
version_conflict / unknown-not-pass / marker accounting / full subtree
coverage. No live Figma, no browser.

Run:
    python3 tests/test_visibility_compiler.py
Exits non-zero on first failure.
"""
import os, sys, tempfile, pathlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
sys.path.insert(0, HERE)

import visibility_rules as vr          # noqa: E402
import audit_content as ac              # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(('  ok  ' if cond else ' FAIL ') + name +
          (('  -- ' + str(detail)) if detail and not cond else ''))


SOLID = [{'type': 'SOLID', 'visible': True,
          'color': {'r': 1, 'g': 0, 'b': 0}, 'opacity': 1}]


def rect(nid, x, y, w, h, visible=True, opacity=1.0, fills=None, **kw):
    node = {'id': nid, 'type': 'RECTANGLE', 'name': nid,
            'visible': visible, 'opacity': opacity,
            'fills': SOLID if fills is None else fills,
            'strokes': [], 'x': x, 'y': y, 'width': w, 'height': h}
    node.update(kw)
    return node


def text(nid, ch, visible=True, opacity=1.0, **kw):
    node = {'id': nid, 'type': 'TEXT', 'name': nid,
            'visible': visible, 'opacity': opacity, 'characters': ch,
            'fills': SOLID, 'strokes': [],
            'x': 0, 'y': 0, 'width': 40, 'height': 16}
    node.update(kw)
    return node


# ---------------------------------------------------------------------------
# 1. self hidden child of a visible parent
# ---------------------------------------------------------------------------
def test_self_hidden():
    print('[1] self-hidden child')
    root = {'id': 'ab', 'type': 'FRAME', 'visible': True, 'opacity': 1,
            'clipsContent': False, 'width': 200, 'height': 200,
            'children': [
                rect('vis', 0, 0, 50, 50, visible=True),
                rect('hid', 60, 0, 50, 50, visible=False),
            ]}
    cov = vr.annotate_visibility_tree(root)
    hid = cov['rows']['hid']['disposition']
    vis = cov['rows']['vis']['disposition']
    check('hidden child = self-invisible', hid == 'self-invisible', hid)
    check('hidden child effectiveVisible false',
          cov['rows']['hid']['ctx']['effectiveVisible'] is False)
    check('visible child paints', vis == 'rendered-pixels-visible', vis)


# ---------------------------------------------------------------------------
# 2. ancestor hidden: whole subtree still covered (DC-1)
# ---------------------------------------------------------------------------
def test_ancestor_hidden():
    print('[2] ancestor-hidden subtree')
    root = {'id': 'ab', 'type': 'FRAME', 'visible': True, 'opacity': 1,
            'width': 200, 'height': 200,
            'children': [
                {'id': 'grp', 'type': 'GROUP', 'visible': False,
                 'opacity': 1, 'fills': [], 'strokes': [],
                 'x': 0, 'y': 0, 'width': 50, 'height': 50,
                 'children': [rect('deep', 0, 0, 50, 50, visible=True)]},
            ]}
    cov = vr.annotate_visibility_tree(root)
    check('subtree fully covered (3 rows)', len(cov['rows']) == 3,
          list(cov['rows']))
    check('group = self-invisible',
          cov['rows']['grp']['disposition'] == 'self-invisible')
    check('deep child = ancestor-invisible',
          cov['rows']['deep']['disposition'] == 'ancestor-invisible',
          cov['rows']['deep']['disposition'])
    check('deep child not painted',
          cov['rows']['deep']['ctx']['paintsPixels'] is False)


# ---------------------------------------------------------------------------
# 3. opacity < 0.01 symmetric skip (DC-6)
# ---------------------------------------------------------------------------
def test_opacity_threshold():
    print('[3] opacity 0.005 -> transparent skip at all layers')
    root = {'id': 'ab', 'type': 'FRAME', 'visible': True, 'opacity': 1,
            'width': 100, 'height': 100,
            'children': [rect('faint', 0, 0, 50, 50, opacity=0.005)]}
    cov = vr.annotate_visibility_tree(root)
    row = cov['rows']['faint']
    check('faint node = transparent',
          row['disposition'] == 'transparent', row['disposition'])
    check('below opacity threshold', row['ctx']['belowOpacityThreshold'])
    check('must not be exported', vr.should_export_unit(row['ctx']) is False)


# ---------------------------------------------------------------------------
# 4. fully clipped by ancestor clip chain
# ---------------------------------------------------------------------------
def test_fully_clipped():
    print('[4] fully-clipped child')
    # Big stage; a grandparent clips to a 100x100 box; child sits at 200,200
    # (inside the stage, but completely outside the ancestor clip rect).
    root = {'id': 'ab', 'type': 'FRAME', 'visible': True, 'opacity': 1,
            'width': 400, 'height': 400,
            'children': [
                {'id': 'clip', 'type': 'FRAME', 'visible': True,
                 'opacity': 1, 'clipsContent': True, 'fills': [],
                 'strokes': [], 'x': 0, 'y': 0, 'width': 100, 'height': 100,
                 'children': [rect('out', 200, 200, 50, 50)]},
            ]}
    cov = vr.annotate_visibility_tree(root)
    row = cov['rows']['out']
    check('child inside stage but outside clip = fully-clipped',
          row['disposition'] == 'fully-clipped', row['disposition'])
    check('clip visible area = 0',
          row['ctx']['computedClipPath']['visibleAreaPx'] == 0)


# ---------------------------------------------------------------------------
# 5. hidden text node
# ---------------------------------------------------------------------------
def test_hidden_text():
    print('[5] hidden text')
    root = {'id': 'ab', 'type': 'FRAME', 'visible': True, 'opacity': 1,
            'width': 100, 'height': 100,
            'children': [text('t-hidden', 'boo', visible=False)]}
    cov = vr.annotate_visibility_tree(root)
    row = cov['rows']['t-hidden']
    check('hidden text = self-invisible',
          row['disposition'] == 'self-invisible')
    check('hidden text effectiveVisible false',
          row['ctx']['effectiveVisible'] is False)


# ---------------------------------------------------------------------------
# 6. visible sibling is not deleted when its sibling is hidden
# ---------------------------------------------------------------------------
def test_visible_sibling_preserved():
    print('[6] visible sibling preserved; hidden not baked')
    root = {'id': 'ab', 'type': 'FRAME', 'visible': True, 'opacity': 1,
            'width': 200, 'height': 100,
            'children': [
                rect('keep', 0, 0, 50, 50),
                rect('drop', 60, 0, 50, 50, visible=False),
            ]}
    cov = vr.annotate_visibility_tree(root)
    cov, baked_map = vr.propagate_baking(cov, unit_node_ids={'ab'})
    check('visible sibling baked into stage PNG',
          cov['rows']['keep']['disposition'] == 'baked-into-ancestor-png',
          cov['rows']['keep']['disposition'])
    check('hidden sibling NOT in baked list',
          'drop' not in baked_map.get('ab', []), baked_map)


# ---------------------------------------------------------------------------
# 7. hidden content baked into visible parent PNG -> strict block
# ---------------------------------------------------------------------------
def test_baked_visibility_conflict():
    print('[7] baked-visibility-conflict strict block')
    conflicts = vr.find_hidden_baked_conflicts(
        skipped_node_ids={'ghost'},
        baked_map={'asset1': ['leaf-a', 'ghost']})
    check('conflict detected', len(conflicts) == 1, conflicts)
    c = conflicts[0]
    check('conflict kind baked-visibility-conflict',
          c['kind'] == vr.CONFLICT_BAKED_VISIBILITY, c['kind'])
    check('conflict references ancestor asset', c['ancestorAssetId'] == 'asset1')
    agg = vr.aggregate_gates([{'gate': 'g', 'status': 'fail'}])
    check('strict blocked when conflict fails', agg['strictBlocked'] is True)


# ---------------------------------------------------------------------------
# 8. live vs reference disagreement -> version_conflict, never delete live
# ---------------------------------------------------------------------------
def test_version_conflict():
    print('[8] live/reference version_conflict')
    live = {'id': 'n1', 'visible': True, 'opacity': 1.0}
    reference = {'id': 'n1', 'visible': False, 'opacity': 1.0}
    vc = vr.detect_version_conflict(live, reference)
    check('version_conflict detected',
          vc is not None and vc['kind'] == vr.CONFLICT_VERSION, vc)
    check('action = block and request finer export',
          vc['action'] == 'block-and-request-finer-export', vc['action'])
    check('policy forbids deleting live content',
          'must NOT be deleted' in vc['detail'], vc['detail'])
    agree = vr.detect_version_conflict(
        {'id': 'n2', 'visible': True, 'opacity': 1},
        {'id': 'n2', 'visible': True, 'opacity': 1})
    check('agreeing versions = no conflict', agree is None)


# ---------------------------------------------------------------------------
# 9. unknown is not pass (DC-4)
# ---------------------------------------------------------------------------
def test_unknown_not_pass():
    print('[9] unknown never silently passes')
    row = {'nodeId': 'x', 'disposition': 'rendered-pixels-visible',
           'ctx': {'effectiveVisible': True, 'computedOpacity': 1.0}}
    res = vr.evaluate_browser_sample(row, measured={})
    statuses = {g['status'] for g in res['gates']}
    check('missing measurement -> unknown gates',
          'unknown' in statuses and 'fail' not in statuses, statuses)
    agg = vr.aggregate_gates(res['gates'])
    check('allPass False with unknowns', agg['allPass'] is False, agg)


# ---------------------------------------------------------------------------
# 10. browser evaluator: false must not paint; true must not vanish
# ---------------------------------------------------------------------------
def test_browser_evaluator_pixel_truth():
    print('[10] measured pixel truth')
    invis_row = {'nodeId': 'h', 'disposition': 'self-invisible',
                 'ctx': {'effectiveVisible': False, 'computedOpacity': 0.0,
                         'computedStageIntersection': {'outsideStage': False}}}
    bad = vr.evaluate_browser_sample(invis_row, {
        'display': 'block', 'visibility': 'hidden', 'opacity': 1.0,
        'visiblePixelCount': 42,
        'rect': {'x': 0, 'y': 0, 'width': 10, 'height': 10},
        'stageRect': {'x': 0, 'y': 0, 'width': 100, 'height': 100},
        'clipRect': {'x': 0, 'y': 0, 'width': 10, 'height': 10}})
    fails = [g['gate'] for g in bad['gates'] if g['status'] == 'fail']
    check('invisible node painting -> fail',
          'G-no-paint-if-invisible' in fails, fails)

    vis_row = {'nodeId': 'v', 'disposition': 'rendered-pixels-visible',
               'ctx': {'effectiveVisible': True, 'computedOpacity': 1.0,
                       'computedStageIntersection': {'outsideStage': False}}}
    empty = vr.evaluate_browser_sample(vis_row, {
        'display': 'block', 'visibility': 'visible', 'opacity': 1.0,
        'visiblePixelCount': 0,
        'rect': {'x': 0, 'y': 0, 'width': 10, 'height': 10},
        'stageRect': {'x': 0, 'y': 0, 'width': 100, 'height': 100},
        'clipRect': {'x': 0, 'y': 0, 'width': 10, 'height': 10}})
    efails = [g for g in empty['gates'] if g['status'] == 'fail']
    check('visible node zero pixels -> fail', len(efails) > 0, efails)

    good = vr.evaluate_browser_sample(vis_row, {
        'display': 'block', 'visibility': 'visible', 'opacity': 1.0,
        'visiblePixelCount': 99,
        'rect': {'x': 0, 'y': 0, 'width': 10, 'height': 10},
        'stageRect': {'x': 0, 'y': 0, 'width': 100, 'height': 100},
        'clipRect': {'x': 0, 'y': 0, 'width': 10, 'height': 10},
        'naturalWidth': 10, 'naturalHeight': 10})
    gagg = vr.aggregate_gates(good['gates'])
    check('honest measurement passes',
          gagg['fail'] == 0 and gagg['unknown'] == 0, gagg)


# ---------------------------------------------------------------------------
# 11. double exposure (BC-2) and coverage (BC-1)
# ---------------------------------------------------------------------------
def test_double_exposure_and_coverage():
    print('[11] BC-2 double exposure; BC-1 coverage')
    double = vr.check_double_exposure({'a1', 'a2'}, {'leaf', 'a1'})
    check('double exposure flagged', double == ['a1'], double)

    inside = vr.check_baked_coverage(
        descendant_box={'x': 5, 'y': 5, 'width': 10, 'height': 10},
        ancestor_aabb={'x': 0, 'y': 0, 'width': 50, 'height': 50})
    outside = vr.check_baked_coverage(
        descendant_box={'x': 100, 'y': 100, 'width': 10, 'height': 10},
        ancestor_aabb={'x': 0, 'y': 0, 'width': 50, 'height': 50})
    check('covered descendant passes', inside is True)
    check('uncovered descendant fails', outside is False)


# ---------------------------------------------------------------------------
# 12. empty visual marker does not count as visible content
# ---------------------------------------------------------------------------
def test_empty_marker_not_content():
    print('[12] empty marker = ledger anchor only')
    root = {'id': 'ab', 'type': 'FRAME', 'visible': True, 'opacity': 1,
            'width': 100, 'height': 100,
            'children': [rect('wrapper', 0, 0, 50, 50,
                              fills=[], strokes=[])]}
    cov = vr.annotate_visibility_tree(root)
    check('paintless node = empty-wrapper',
          cov['rows']['wrapper']['disposition'] == 'empty-wrapper',
          cov['rows']['wrapper']['disposition'])
    frag = ('<div class="container-visual marker" data-n="wrapper" '
            'style="visibility:hidden;"></div>')
    check('builder marker fragment detected', vr.is_marker_fragment(frag))
    plain = '<img data-n="real" src="r.png">'
    check('real layer not mistaken for marker',
          not vr.is_marker_fragment(plain))


# ---------------------------------------------------------------------------
# 13. out-of-stage / zero-size
# ---------------------------------------------------------------------------
def test_out_of_stage_and_zero_size():
    print('[13] out-of-stage; zero-size')
    root = {'id': 'ab', 'type': 'FRAME', 'visible': True, 'opacity': 1,
            'width': 6735, 'height': 2381,
            'children': [rect('off', 8000, 10, 50, 50),
                        rect('flat', 10, 10, 0, 20)]}
    cov = vr.annotate_visibility_tree(root)
    check('off-artboard node = out-of-stage',
          cov['rows']['off']['disposition'] == 'out-of-stage',
          cov['rows']['off']['disposition'])
    check('zero-width node = zero-size',
          cov['rows']['flat']['disposition'] == 'zero-size',
          cov['rows']['flat']['disposition'])


# ---------------------------------------------------------------------------
# 14. audit_content integration: baking conflicts hard-fail; markers excluded
# ---------------------------------------------------------------------------
def test_audit_integration():
    print('[14] audit_content integration')
    manifest = {
        'artboardId': 't', 'artboardSize': {'width': 200, 'height': 200},
        'rootFills': [],
        # ghost wrongly exported BOTH standalone AND baked into asset1 (BC-2).
        'assets': [{'nodeId': 'asset1', 'asset': 'a.png',
                    'effectiveVisible': True},
                   {'nodeId': 'ghost', 'asset': 'g.png',
                    'effectiveVisible': True}],
        'containerVisuals': [
            {'nodeId': 'c-empty', 'type': 'FRAME', 'effectiveVisible': True,
             'renderAABB': {'x': 0, 'y': 0, 'width': 50, 'height': 20},
             'fills': [], 'strokes': []},
        ],
        'contentLedger': [
            {'nodeId': 'ab', 'disposition': 'rendered-stage'},
            {'nodeId': 'asset1', 'disposition': 'rendered-asset'},
            {'nodeId': 'leaf', 'disposition': 'rendered-by-ancestor',
             'ancestorNodeId': 'asset1'},
            {'nodeId': 'ghost', 'disposition': 'rendered-by-ancestor',
             'ancestorNodeId': 'asset1'},
            {'nodeId': 'over', 'disposition': 'skipped-invisible'},
        ],
    }
    # over: a skipped node that still gets a data-n in HTML (over-render).
    # c-empty: zero-visual marker anchor (must not count as painted).
    bad_doc = ('<div id="stage">'
               '<img data-n="asset1" src="a.png">'
               '<img data-n="ghost" src="g.png">'
               '<div class="container-visual marker" data-n="c-empty" '
               'style="visibility:hidden;"></div>'
               '<span data-n="over">x</span>'
               '</div>')
    res = ac.audit(manifest, {'texts': []}, bad_doc)
    check('double-exposure baking conflict recorded',
          len(res['bakingConflicts']) == 1
          and res['bakingConflicts'][0]['nodeId'] == 'ghost',
          res['bakingConflicts'])
    check('baking conflict hard-fails', res['hardFailures'] > 0,
          res['hardFailures'])
    check('marker excluded from painted containers',
          res['renderedPaintedContainers'] == 0,
          res['renderedPaintedContainers'])
    check('marker anchor tracked separately',
          res['markerAnchors'] == ['c-empty'], res['markerAnchors'])
    check('over-rendered skipped ledger node flagged',
          'over' in res['overRenderedSkippedLedger'],
          res['overRenderedSkippedLedger'])

    # Clean build stays green (no double exposure, no over-render).
    clean = {
        'artboardId': 't', 'artboardSize': {'width': 200, 'height': 200},
        'rootFills': [],
        'assets': [{'nodeId': 'asset1', 'asset': 'a.png',
                    'effectiveVisible': True}],
        'containerVisuals': [
            {'nodeId': 'c-empty', 'type': 'FRAME', 'effectiveVisible': True,
             'renderAABB': {'x': 0, 'y': 0, 'width': 50, 'height': 20},
             'fills': [], 'strokes': []},
        ],
        'contentLedger': [
            {'nodeId': 'ab', 'disposition': 'rendered-stage'},
            {'nodeId': 'asset1', 'disposition': 'rendered-asset'},
            {'nodeId': 'leaf', 'disposition': 'rendered-by-ancestor',
             'ancestorNodeId': 'asset1'},
            {'nodeId': 'c-empty', 'disposition': 'rendered-container'},
        ],
    }
    good_doc = ('<div id="stage">'
                '<img data-n="asset1" src="a.png">'
                '<div class="container-visual marker" data-n="c-empty" '
                'style="visibility:hidden;"></div>'
                '</div>')
    good = ac.audit(clean, {'texts': []}, good_doc)
    check('clean manifest hardFailures == 0', good['hardFailures'] == 0,
          good)
    check('clean manifest no baking conflicts',
          len(good['bakingConflicts']) == 0, good['bakingConflicts'])


# ---------------------------------------------------------------------------
# 15. REAL browser: Playwright collects computed style / clip / mask /
#     stage intersection / visible pixels per mapped node.
# ---------------------------------------------------------------------------
def test_real_browser_audit():
    print('[15] real headless Chromium browser audit')
    html = """<html><body style="margin:0">
<div id="stage" style="position:relative;width:800px;height:600px;
  background:#ffffff;overflow:hidden">
  <div data-n="v1" style="position:absolute;left:50px;top:50px;width:120px;
    height:60px;background:rgb(255,0,0)"></div>
  <div data-n="m1" class="container-visual marker" style="position:absolute;
    left:200px;top:50px;width:60px;height:30px;visibility:hidden;
    background:rgb(0,255,0)"></div>
  <div data-n="off" style="position:absolute;left:-9999px;top:10px;width:40px;
    height:40px;background:rgb(0,0,255)"></div>
  <div style="position:absolute;left:200px;top:200px;width:100px;height:100px;
    overflow:hidden">
    <div data-n="clip" style="position:absolute;left:200px;top:200px;
      width:80px;height:40px;background:rgb(255,255,0)"></div>
  </div>
  <div style="visibility:hidden;position:absolute;left:350px;top:50px;
    width:100px;height:50px">
    <div data-n="anc-hid" style="position:absolute;left:0;top:0;width:100px;
      height:50px;background:rgb(0,255,0)"></div>
  </div>
</div></body></html>"""
    manifest = {'contentLedger': [
        {'nodeId': 'v1', 'disposition': 'rendered-pixels-visible',
         'effectiveVisible': True, 'effectiveOpacity': 1.0},
        {'nodeId': 'm1', 'disposition': 'empty-wrapper',
         'effectiveVisible': True, 'effectiveOpacity': 1.0},
        {'nodeId': 'off', 'disposition': 'out-of-stage',
         'effectiveVisible': True, 'effectiveOpacity': 1.0},
        {'nodeId': 'clip', 'disposition': 'fully-clipped',
         'effectiveVisible': True, 'effectiveOpacity': 1.0},
        {'nodeId': 'anc-hid', 'disposition': 'rendered-pixels-visible',
         'effectiveVisible': True, 'effectiveOpacity': 1.0},
    ]}
    with tempfile.TemporaryDirectory() as td:
        hp = pathlib.Path(td) / 'fixture.html'
        hp.write_text(html, encoding='utf-8')
        summary = ac.run_browser_audit(str(hp), manifest, {'texts': []},
                                       viewport_width=1000, viewport_height=800)
    by_id = {n['nodeId']: n for n in summary['nodes']}
    check('browser mapped all 5 nodes', len(summary['nodes']) == 5,
          summary['nodeCount'])

    v1 = by_id['v1']['measured']
    check('visible node really paints pixels',
          v1['visiblePixelCount'] > 100, v1['visiblePixelCount'])
    check('visible node display=block opacity=1',
          v1['display'] == 'block' and abs(v1['opacity'] - 1.0) < 0.01, v1)
    check('visible node intersects stage', v1['stageIntersection'] is not None)

    m1 = by_id['m1']['measured']
    check('marker computed visibility=hidden',
          m1['visibility'] == 'hidden', m1['visibility'])
    check('marker has zero visible pixels',
          m1['visiblePixelCount'] == 0, m1['visiblePixelCount'])

    off = by_id['off']['measured']
    check('off-stage node has no stage intersection',
          off['stageIntersection'] is None, off['stageIntersection'])
    check('off-stage node zero visible pixels',
          off['visiblePixelCount'] == 0, off['visiblePixelCount'])

    clip = by_id['clip']['measured']
    check('fully-clipped ancestor clip rect zero area',
          clip['clipRect']['width'] == 0 and clip['clipRect']['height'] == 0,
          clip['clipRect'])
    check('fully-clipped node zero visible pixels',
          clip['visiblePixelCount'] == 0, clip['visiblePixelCount'])

    # QA recheck: ancestor visibility:hidden must be caught, and sibling paint
    # must NOT be counted as this node's pixels.
    anc = by_id['anc-hid']['measured']
    check('ancestor-hidden node inherits computed visibility=hidden',
          anc['visibility'] == 'hidden', anc['visibility'])
    check('ancestor-hidden node contributes zero own pixels',
          anc['visiblePixelCount'] == 0, anc['visiblePixelCount'])
    vis_fails = [f for f in summary['fails']
                 if f['gate'] == 'G-visibility'
                 and f['nodeId'] == 'anc-hid']
    check('B gate FAILS ancestor-hidden painted node', len(vis_fails) == 1,
          summary['fails'])
    check('unknown is never pass (summary not all-pass)',
          summary['allPass'] is False and summary['gateSummary']['unknown'] == 0,
          summary['gateSummary'])


def main():
    test_self_hidden()
    test_ancestor_hidden()
    test_opacity_threshold()
    test_fully_clipped()
    test_hidden_text()
    test_visible_sibling_preserved()
    test_baked_visibility_conflict()
    test_version_conflict()
    test_unknown_not_pass()
    test_browser_evaluator_pixel_truth()
    test_double_exposure_and_coverage()
    test_empty_marker_not_content()
    test_out_of_stage_and_zero_size()
    test_audit_integration()
    test_real_browser_audit()
    print('\n=== RESULT ===')
    print(f'passed: {len(PASS)}  failed: {len(FAIL)}')
    if FAIL:
        print('FAILURES:'); [print(' -', f) for f in FAIL]
        sys.exit(1)


if __name__ == '__main__':
    main()
