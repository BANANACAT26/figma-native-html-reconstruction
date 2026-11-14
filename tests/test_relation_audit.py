#!/usr/bin/env python3
"""Relation measurement gate tests: build_from_plan + audit_relations.

Proves that when the PLAN INPUT changes (position / visibility / text length /
connector edge / nested rotation) the plan's expected relations move with it, and
that the measured DOM must follow -- an absolute-position match with the wrong
owner / gap / alignment still fails, and an incomparable (box-family mismatch)
pair is unknown and NOT a pass under strict.

Covers: nested -180 rotation cancellation, green-card relative offset, fixed
text, horizontal + vertical arrows, visibility reconciliation, deterministic
repair plan.

Run:  python3 tests/test_relation_audit.py
Exits non-zero on first failure.
"""
import json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

import build_fixed_html as bfh      # noqa: E402
import audit_relations as ar        # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(('  ok  ' if cond else ' FAIL ') + name +
          (('  -- ' + str(detail)) if detail and not cond else ''))


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
def green_plan(text_chars='玩VR为什么总是晕?', child_x=360.0, card_x=300.0):
    """Frame-03 style green chat bubble: parent row self-rotation -180, the green
    card and text cancel back to upright. Coordinates are artboard-space."""
    return {
        'ruleVersion': 'design-compiler/1.0.0', 'planHash': 'green-1',
        'artboard': {'id': 'art', 'name': 'frame03', 'width': 800, 'height': 400,
                     'rootFills': [{'type': 'SOLID', 'color': {'r': 1, 'g': 1, 'b': 1, 'a': 1}}]},
        'nodes': [
            {'nodeId': 'ROW', 'type': 'FRAME', 'parentId': None,
             'disposition': 'rendered-container', 'effectiveVisibility': True,
             'effectiveMatrix': [-1, 0, 0, -1, 871.153, 316.649],
             'renderAABB': {'x': card_x - 180, 'y': 150, 'w': 682, 'h': 92},
             'local': {'x': 0, 'y': 0, 'w': 682, 'h': 92},
             'fills': [], 'zIndexChain': [1]},
            {'nodeId': 'BUBBLE', 'type': 'FRAME', 'parentId': 'ROW',
             'disposition': 'rendered-container', 'effectiveVisibility': True,
             'effectiveMatrix': [-1, 0, 0, -1, 791.181, 316.649],
             'renderAABB': {'x': card_x, 'y': 154, 'w': 454, 'h': 110},
             'local': {'x': 0, 'y': 0, 'w': 454, 'h': 110},
             'fills': [{'type': 'SOLID', 'visible': True,
                        'color': {'r': 0.663, 'g': 0.910, 'b': 0.478, 'a': 1}}],
             'cornerRadius': 14, 'zIndexChain': [2]},
            {'nodeId': 'TAIL', 'type': 'VECTOR', 'parentId': 'BUBBLE',
             'disposition': 'rendered-asset', 'effectiveVisibility': True,
             'effectiveMatrix': [0, -1, 1, 0, 314.267, 302.652],
             'renderAABB': {'x': 314, 'y': 272, 'w': 21, 'h': 31},
             'local': {'x': 0, 'y': 0, 'w': 31, 'h': 21}, 'zIndexChain': [3]},
            {'nodeId': 'GHOST', 'type': 'FRAME', 'parentId': 'ROW',
             'disposition': 'hidden', 'effectiveVisibility': False,
             'renderAABB': {'x': 0, 'y': 0, 'w': 10, 'h': 10}, 'zIndexChain': [9]},
        ],
        'assets': [],
        'textPlans': [
            {'nodeId': 'BTEXT', 'type': 'TEXT', 'parentId': 'BUBBLE',
             'disposition': 'rendered-text', 'effectiveVisibility': True,
             'effectiveMatrix': [1, 0, 0, 1, child_x, 288.654],
             'renderAABB': {'x': child_x, 'y': 288.654, 'w': 200, 'h': 30},
             'local': {'x': 0, 'y': 0, 'w': 200, 'h': 30},
             'characters': text_chars,
             'fontName': {'family': 'Inter', 'style': 'Regular'}, 'fontSize': 16,
             'lineHeight': {'unit': 'PIXELS', 'value': 24},
             'letterSpacing': {'unit': 'PIXELS', 'value': 0},
             'textAlignHorizontal': 'LEFT', 'textAlignVertical': 'CENTER',
             'textAutoResize': 'NONE',
             'fills': [{'type': 'SOLID', 'visible': True, 'color': {'r': 0, 'g': 0, 'b': 0, 'a': 1}}],
             'zIndexChain': [4]},
        ],
        'connectorPlans': [],
        'relations': [], 'unresolved': [],
    }


def dom(plan, child_dx=0.0, child_dy=0.0, tail_rot=True, extra_boxes=None):
    """Build a measured DOM that exactly follows the plan (default), or drifts."""
    nodes = {}
    for n in plan['nodes']:
        if not n.get('effectiveVisibility', True):
            continue
        b = n['renderAABB']
        m = n.get('effectiveMatrix')
        nodes[n['nodeId']] = {
            'box': {'x': b['x'], 'y': b['y'], 'w': b['w'], 'h': b['h']},
            'matrix': m if tail_rot or n['nodeId'] != 'TAIL' else [1, 0, 0, 1, 0, 0],
        }
    for t in plan['textPlans']:
        if not t.get('effectiveVisibility', True):
            continue
        b = t['renderAABB']
        nodes[t['nodeId']] = {
            'box': {'x': b['x'] + child_dx, 'y': b['y'] + child_dy,
                    'w': b['w'], 'h': b['h']},
            'matrix': t.get('effectiveMatrix'),
            'lineBox': {'x': b['x'] + child_dx, 'y': b['y'] + child_dy,
                        'w': b['w'], 'h': 24},
            'baseline': b['y'] + child_dy + 19.2,
        }
    if extra_boxes:
        nodes.update(extra_boxes)
    order = [n['nodeId'] for n in plan['nodes'] if n.get('effectiveVisibility', True)]
    order += [t['nodeId'] for t in plan['textPlans'] if t.get('effectiveVisibility', True)]
    return {'nodes': nodes, 'domOrder': order}


# ---------------------------------------------------------------------------
# 1. builder: plan consumption, traceability, visibility
# ---------------------------------------------------------------------------
def test_builder_plan_traceable():
    print('[1] builder consumes plan, stamps nodeId/ruleVersion/disposition')
    plan = green_plan()
    with tempfile.TemporaryDirectory() as td:
        doc, rep = bfh.build_from_plan(plan, td, td)
        check('emits mappings ledger', rep['mappingCount'] == 5, rep['mappingCount'])
        check('renders visible only', rep['renderedCount'] == 4, rep['renderedCount'])
        check('hidden skipped from visibility', rep['skippedHidden'] == 1, rep['skippedHidden'])
        check('stamps nodeId', 'data-n="BUBBLE"' in doc)
        check('stamps ruleVersion', 'data-rule="design-compiler/1.0.0"' in doc)
        check('stamps disposition', 'data-disposition="rendered-container"' in doc)
        check('hidden ghost absent', 'GHOST' not in doc)
        # nested -180: bubble keeps its -180 wrapper; text cancels to upright box.
        check('bubble -180 matrix wrapper',
              'matrix(-1.0000,0.0000,-0.0000,-1.0000,791.1810,316.6490)' in doc
              or 'matrix(-1.0000,0.0000,0.0000,-1.0000,791.1810,316.6490)' in doc)
        check('text upright after cancellation (identity linear -> absolute box)',
              'left:%.4fpx' % plan['textPlans'][0]['renderAABB']['x'] in doc)


# ---------------------------------------------------------------------------
# 2. matched plan/DOM passes; position input moves expected relations
# ---------------------------------------------------------------------------
def test_position_change_follows():
    print('[2] changing planned position moves expected inset; DOM follows -> pass')
    plan = green_plan()
    a0, _ = ar.audit_relations(plan, dom(plan))
    check('baseline green bubble fully passes', a0['summary']['fail'] == 0 and a0['summary']['unknown'] == 0,
          [(e['kind'], e['status'], e['reason']) for e in a0['edges'] if e['status'] != 'pass'])

    # Move the planned bubble right by 40px; rebuild measured DOM to follow.
    plan2 = green_plan(child_x=400.0, card_x=340.0)
    a2, _ = ar.audit_relations(plan2, dom(plan2))
    check('after re-layout, fresh DOM still passes', a2['summary']['fail'] == 0,
          [(e['kind'], e['delta']) for e in a2['edges'] if e['status'] == 'fail'])

    # Stale DOM from the OLD position must now FAIL the inset relation even though
    # boxes are individually well-formed (absolute position "close-ish").
    stale = dom(plan)  # measured at old x
    a3, _ = ar.audit_relations(plan2, stale)
    inset_fails = [e for e in a3['edges']
                   if e['kind'] == 'parentChildInset' and e['status'] == 'fail']
    check('stale position fails parentChildInset', len(inset_fails) >= 1, a3['summary'])


# ---------------------------------------------------------------------------
# 3. green relative offset + fixed text length
# ---------------------------------------------------------------------------
def test_green_relative_and_fixed_text():
    print('[3] green card relative offset; fixed text box; text length change')
    plan = green_plan(text_chars='短')
    # fixed-box text: w is fixed regardless of characters. Short string still
    # occupies the planned 200px box.
    with tempfile.TemporaryDirectory() as td:
        doc, _ = bfh.build_from_plan(plan, td, td)
        check('fixed text box emitted at planned width',
          'width:200px' in doc or 'width:200.000px' in doc or 'width:200.0000px' in doc)

    a, _ = ar.audit_relations(plan, dom(plan))
    check('short fixed text passes', a['summary']['fail'] == 0,
          [(e['kind'], e['status'], e['reason']) for e in a['edges'] if e['status'] != 'pass'])

    # Lengthen characters: re-compiled plan grows the text box to 300px. DOM that
    # still carries the old 200px line box must fail textLineInset.
    plan_long = green_plan(text_chars='玩VR为什么总是晕?玩VR为什么总是晕?')
    plan_long['textPlans'][0]['renderAABB']['w'] = 300
    plan_long['textPlans'][0]['local']['w'] = 300
    dom_stale = dom(plan)  # old line box width 200
    a2, repair = ar.audit_relations(plan_long, dom_stale)
    tli = [e for e in a2['edges'] if e['kind'] == 'textLineInset' and e['status'] == 'fail']
    check('line box not grown -> textLineInset fail', len(tli) >= 1, a2['summary'])
    set_text = [x for x in repair['actions'] if x['kind'] == 'setTextLayout']
    check('repair proposes setTextLayout', len(set_text) >= 1)


# ---------------------------------------------------------------------------
# 4. nested rotation box family: rotated element must stay rotated
# ---------------------------------------------------------------------------
def test_nested_rotation_family():
    print('[4] nested -180 cancellation; rotated tail flattened -> unknown strict')
    plan = green_plan()
    a, _ = ar.audit_relations(plan, dom(plan, tail_rot=True))
    check('rotated tail measured as rotated -> passes',
          a['summary']['unknown'] == 0 and a['summary']['fail'] == 0,
          [(e['kind'], e['status']) for e in a['edges'] if e['status'] != 'pass'])
    a2, repair = ar.audit_relations(plan, dom(plan, tail_rot=False))
    check('flattened rotated element -> unknown', a2['summary']['unknown'] >= 1, a2['summary'])
    check('strict unknown is NOT a pass (strictFail>0)', a2['summary']['strictFail'] > 0)
    check('blocked, not auto-repaired', len(repair['blocked']) >= 1)


# ---------------------------------------------------------------------------
# 5. horizontal + vertical connector arrows
# ---------------------------------------------------------------------------
def _arrow_plan(axis='horizontal'):
    if axis == 'horizontal':
        src = {'x': 100, 'y': 100, 'w': 60, 'h': 40}
        tgt = {'x': 160, 'y': 100, 'w': 60, 'h': 40}   # touches source.right
        se, te = 'right', 'left'
    else:
        src = {'x': 100, 'y': 100, 'w': 60, 'h': 40}
        tgt = {'x': 100, 'y': 140, 'w': 60, 'h': 40}   # touches source.bottom
        se, te = 'bottom', 'top'
    plan = {
        'ruleVersion': 'design-compiler/1.0.0', 'planHash': 'arrow-' + axis,
        'artboard': {'id': 'a', 'name': 'arrows', 'width': 400, 'height': 300,
                     'rootFills': [{'type': 'SOLID', 'color': {'r': 1, 'g': 1, 'b': 1, 'a': 1}}]},
        'nodes': [
            {'nodeId': 'S', 'type': 'FRAME', 'parentId': None, 'disposition': 'rendered-container',
             'effectiveVisibility': True, 'effectiveMatrix': [1, 0, 0, 1, 0, 0],
             'renderAABB': src, 'zIndexChain': [1]},
            {'nodeId': 'T', 'type': 'FRAME', 'parentId': None, 'disposition': 'rendered-container',
             'effectiveVisibility': True, 'effectiveMatrix': [1, 0, 0, 1, 0, 0],
             'renderAABB': tgt, 'zIndexChain': [2]},
        ],
        'assets': [], 'textPlans': [],
        'connectorPlans': [{'id': 'c', 'sourceNodeId': 'S', 'targetNodeId': 'T',
                            'sourceEdge': se, 'targetEdge': te, 'arrowheadAt': 'to',
                            'uncertain': False}],
        'relations': [], 'unresolved': [],
    }
    return plan, src, tgt, se, te


def test_arrows():
    print('[5] horizontal + vertical connector edges')
    for axis in ('horizontal', 'vertical'):
        plan, src, tgt, se, te = _arrow_plan(axis)
        measured = {'nodes': {
            'S': {'box': dict(src), 'matrix': [1, 0, 0, 1, 0, 0]},
            'T': {'box': dict(tgt), 'matrix': [1, 0, 0, 1, 0, 0]},
        }, 'domOrder': ['S', 'T']}
        a, _ = ar.audit_relations(plan, measured)
        ce = [e for e in a['edges'] if e['kind'] == 'connectorEdge']
        check(f'{axis} arrow tips touch -> pass',
              bool(ce) and all(e['status'] == 'pass' for e in ce),
              [(e['status'], e.get('delta')) for e in ce])

        # push target 6px away along the connector axis -> tip gap fail
        drifted = json.loads(json.dumps(measured))
        if axis == 'horizontal':
            drifted['nodes']['T']['box']['x'] += 6
        else:
            drifted['nodes']['T']['box']['y'] += 6
        a2, repair = ar.audit_relations(plan, drifted)
        ce2 = [e for e in a2['edges'] if e['kind'] == 'connectorEdge' and e['status'] == 'fail']
        check(f'{axis} tip gap 6px -> fail', len(ce2) == 1, [(e.get('delta')) for e in ce2])
        rebuild = [x for x in repair['actions'] if x['kind'] == 'rebuildConnector']
        check(f'{axis} repair rebuilds connector', len(rebuild) >= 1)


# ---------------------------------------------------------------------------
# 6. visibility reconciliation: plan-hidden vs DOM-shown
# ---------------------------------------------------------------------------
def test_visibility_reconcile():
    print('[6] hidden plan node absent from DOM; extra DOM node is a breach')
    plan = green_plan()
    a, repair = ar.audit_relations(plan, dom(plan))
    check('plan-visible nodes present -> no visibility action',
          not [x for x in repair['actions'] if x['kind'] == 'setVisibility'])

    # DOM renders the node the plan hides (GHOST).
    m2 = dom(plan)
    m2['nodes']['GHOST'] = {'box': {'x': 0, 'y': 0, 'w': 10, 'h': 10},
                           'matrix': [1, 0, 0, 1, 0, 0]}
    a2, repair2 = ar.audit_relations(plan, m2)
    vis = [x for x in repair2['actions'] if x['kind'] == 'setVisibility' and x['nodeId'] == 'GHOST']
    check('extra DOM element -> setVisibility(hidden)', len(vis) == 1, vis)

    # Plan-visible node missing from DOM.
    m3 = dom(plan); m3['nodes'].pop('BTEXT')
    a3, repair3 = ar.audit_relations(plan, m3)
    vis3 = [x for x in repair3['actions'] if x['kind'] == 'setVisibility' and x['nodeId'] == 'BTEXT']
    check('missing DOM element -> setVisibility(visible)', len(vis3) == 1, vis3)


# ---------------------------------------------------------------------------
# 7. explicit alignment / baseline / iconHost edges round-trip
# ---------------------------------------------------------------------------
def test_alignment_baseline_icon():
    print('[7] alignmentAxis / baseline / iconHostCenter explicit edges')
    plan = green_plan()
    bub = next(n for n in plan['nodes'] if n['nodeId'] == 'BUBBLE')
    txt = plan['textPlans'][0]
    bc = bub['renderAABB']['x'] + bub['renderAABB']['w']/2.0
    tc = txt['renderAABB']['x'] + txt['renderAABB']['w']/2.0
    geo_delta = tc - bc
    plan['relations'] = [
        {'edgeId': 'e-align', 'kind': 'alignmentAxis', 'source': 'BUBBLE', 'target': 'BTEXT',
         'expected': {'axis': 'centerX', 'delta': geo_delta}, 'derivation': 'autoLayout'},
        {'edgeId': 'e-base', 'kind': 'baseline', 'source': 'BTEXT', 'target': 'BUBBLE',
         'expected': {'delta': 0.0}, 'derivation': 'autoLayout'},
    ]
    m = dom(plan)
    a, _ = ar.audit_relations(plan, m)
    align = [e for e in a['edges'] if e['kind'] == 'alignmentAxis']
    base = [e for e in a['edges'] if e['kind'] == 'baseline']
    check('alignment edge passes when measured center delta matches plan',
          bool(align) and all(e['status'] == 'pass' for e in align),
          [(e['status'], e.get('delta')) for e in align])
    # drift measured text center by 9px -> alignment fails, repair is geometry
    m2 = dom(plan); m2['nodes']['BTEXT']['box']['x'] += 9
    m2['nodes']['BTEXT']['lineBox']['x'] += 9
    a2, repair2 = ar.audit_relations(plan, m2)
    align2 = [e for e in a2['edges'] if e['kind'] == 'alignmentAxis' and e['status'] == 'fail']
    check('drifted center -> alignmentAxis fail', len(align2) == 1, a2['summary'])
    geom = [x for x in repair2['actions'] if x['kind'] == 'setRelationGeometry']
    check('alignment failure repairs via setRelationGeometry', len(geom) >= 1)
    check('baseline edge derived (Range-backed)', len(base) == 1)


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# I1: real compiler splits geometry (nodes[]) from styling (textPlans[])
# I2: expected={} unknown edges must not KeyError in diff_edge
# ---------------------------------------------------------------------------
def test_I1_split_geometry_merge():
    print('[8] I1: textPlans styling-only + nodes[] geometry merge')
    plan = {
        'ruleVersion': 'design-compiler/1.0.0', 'planHash': 'i1',
        'artboard': {'artboardId': 'a', 'artboardName': 'a', 'artboardSize': {'w': 400, 'h': 300},
                     'rootFills': []},
        'nodes': [
            {'nodeId': 'BOX', 'type': 'FRAME', 'parentId': None,
             'disposition': 'rendered-pixels-visible', 'effectiveVisibility': True,
             'effectiveMatrix': [1, 0, 0, 1, 50, 50],
             'renderAABB': {'x': 50, 'y': 50, 'w': 300, 'h': 100}, 'zIndexChain': [1]},
            {'nodeId': 'T1', 'type': 'TEXT', 'parentId': 'BOX',
             'disposition': 'rendered-pixels-visible', 'effectiveVisibility': True,
             'effectiveMatrix': [1, 0, 0, 1, 70, 80],
             'renderAABB': {'x': 70, 'y': 80, 'w': 120, 'h': 24}, 'zIndexChain': [2]},
        ],
        'assets': [],
        'textPlans': [
            # styling ONLY: no effectiveMatrix/renderAABB on this record.
            {'nodeId': 'T1', 'characters': '真实文本', 'fontName': {'family': 'PingFang SC', 'style': 'Regular'},
             'fontSize': 16, 'lineHeightPx': 24, 'letterSpacingPx': 0.0,
             'textAlignHorizontal': 'LEFT', 'textAlignVertical': 'TOP', 'whiteSpace': 'pre-wrap'},
        ],
        'connectorPlans': [], 'relations': [], 'unresolved': [],
    }
    with tempfile.TemporaryDirectory() as td:
        doc, rep = bfh.build_from_plan(plan, td, td)
        check('I1 builds without crash', rep['renderedCount'] >= 2, rep)
        check('I1 merged text rendered at planned box', 'left:70.0000px' in doc and 'top:80.0000px' in doc)
        check('I1 text characters emitted', '真实文本' in doc)


def test_I2_empty_expected_is_unknown():
    print('[9] I2: expected={} edge -> unknown, no KeyError')
    plan = {
        'ruleVersion': 'design-compiler/1.0.0', 'planHash': 'i2',
        'nodes': [], 'assets': [], 'textPlans': [],
        'relations': [
            {'edgeId': 'e1', 'kind': 'parentChildInset', 'source': 'P', 'target': 'C',
             'expected': {}, 'status': 'unknown', 'unknownReason': 'missing render box',
             'tolerance': 2.0, 'ruleVersion': 'design-compiler/1.0.0',
             'derivation': 'manifest-tree'},
        ], 'unresolved': [],
    }
    raised = None
    try:
        audit, repair = ar.audit_relations(plan, {'nodes': {}, 'domOrder': []}, strict=True)
    except Exception as e:  # noqa: BLE001
        raised = e
    check('I2 no exception on expected={}', raised is None, repr(raised))
    e0 = audit['edges'][0]
    check('I2 expected={} reported unknown', e0['status'] == 'unknown', e0['status'])
    check('I2 strict counts it as strictFail', audit['summary']['strictFail'] >= 1)


def test_I3_real_mode_to_whitespace():
    print('[10] real mode field -> white-space: content-width nowrap, fixed-box pre-wrap')
    def build(mode):
        plan = {
            'ruleVersion': 'design-compiler/1.0.0', 'planHash': 'i3',
            'artboard': {'artboardId': 'a', 'artboardName': 'a', 'artboardSize': {'w': 400, 'h': 200}, 'rootFills': []},
            'nodes': [
                {'nodeId': 'T', 'type': 'TEXT', 'parentId': None,
                 'disposition': 'rendered-pixels-visible', 'effectiveVisibility': True,
                 'effectiveMatrix': [1, 0, 0, 1, 10, 10],
                 'renderAABB': {'x': 10, 'y': 10, 'w': 398, 'h': 54}, 'zIndexChain': [1]},
            ],
            'assets': [],
            'textPlans': [
                {'nodeId': 'T', 'characters': '玩VR为什么总是晕?',
                 'fontName': {'family': 'Inter', 'style': 'Medium'}, 'fontSize': 44,
                 'lineHeightPx': 54, 'letterSpacingPx': 0.0, 'mode': mode,
                 'outerWidth': 'auto' if mode != 'fixed-box' else '398px',
                 'textAlignHorizontal': 'LEFT', 'textAlignVertical': 'TOP',
                 'whiteSpace': 'nowrap' if mode == 'content-width' else 'pre-wrap'},
            ],
            'connectorPlans': [], 'relations': [], 'unresolved': [],
        }
        with tempfile.TemporaryDirectory() as td:
            doc, _ = bfh.build_from_plan(plan, td, td)
        return doc
    doc_auto = build('content-width')
    doc_fixed = build('fixed-box')
    check('content-width (WIDTH_AND_HEIGHT) emits nowrap',
          'white-space:nowrap' in doc_auto and 'white-space:pre-wrap' not in doc_auto)
    check('fixed-box still pre-wrap',
          'white-space:pre-wrap' in doc_fixed and 'white-space:nowrap' not in doc_fixed)


def test_I4_image_fill_and_marker_policy():
    print('[11] IMAGE fill resolved -> bg url; no-fill container -> transparent box (no blanket visibility:hidden)')
    import tempfile, os
    plan = {
        'ruleVersion': 'design-compiler/1.0.0', 'planHash': 'i4',
        'imageAssetMap': {'img1': 'img1.png'},
        'artboard': {'artboardId': 'a', 'artboardName': 'a', 'artboardSize': {'w': 400, 'h': 200}, 'rootFills': []},
        'nodes': [
            {'nodeId': 'IMG', 'type': 'RECTANGLE', 'parentId': None,
             'disposition': 'rendered-pixels-visible', 'effectiveVisibility': True,
             'effectiveMatrix': [1, 0, 0, 1, 20, 20],
             'renderAABB': {'x': 20, 'y': 20, 'w': 100, 'h': 80}, 'zIndexChain': [1],
             'fills': [{'type': 'IMAGE', 'imageRef': 'img1', 'visible': True}]},
            {'nodeId': 'PLAIN', 'type': 'FRAME', 'parentId': None,
             'disposition': 'rendered-pixels-visible', 'effectiveVisibility': True,
             'effectiveMatrix': [1, 0, 0, 1, 150, 20],
             'renderAABB': {'x': 150, 'y': 20, 'w': 100, 'h': 80}, 'zIndexChain': [2]},
            {'nodeId': 'HID', 'type': 'FRAME', 'parentId': None, 'zeroVisual': True,
             'disposition': 'ledger-marker', 'effectiveVisibility': True,
             'effectiveMatrix': [1, 0, 0, 1, 280, 20],
             'renderAABB': {'x': 280, 'y': 20, 'w': 40, 'h': 40}, 'zIndexChain': [3]},
        ],
        'assets': [], 'textPlans': [], 'connectorPlans': [], 'relations': [], 'unresolved': [],
    }
    with tempfile.TemporaryDirectory() as td:
        open(os.path.join(td, 'img1.png'), 'wb').write(b'\x89PNG')
        doc, rep = bfh.build_from_plan(plan, td, td)
    # IMAGE fill with resolvable asset -> background-image url, not a fake solid
    check('IMAGE fill emits background image',
          'background-image' in doc.replace(' ', '') or 'url(' in doc)
    # no-fill container does NOT get visibility:hidden
    check('no-fill container stays measurable (no blanket visibility:hidden)',
          doc.count('visibility:hidden') == 1)  # only the zeroVisual HID node
    check('zeroVisual node hides', 'visibility:hidden' in doc)


def main():
    test_builder_plan_traceable()
    test_position_change_follows()
    test_green_relative_and_fixed_text()
    test_nested_rotation_family()
    test_arrows()
    test_visibility_reconcile()
    test_alignment_baseline_icon()
    test_I1_split_geometry_merge()
    test_I2_empty_expected_is_unknown()
    test_I3_real_mode_to_whitespace()
    test_I4_image_fill_and_marker_policy()
    print()
    print('=== RESULT ===')
    print('passed:', len(PASS), ' failed:', len(FAIL))
    if FAIL:
        print('FAILURES:', FAIL)
        sys.exit(1)


if __name__ == '__main__':
    main()
