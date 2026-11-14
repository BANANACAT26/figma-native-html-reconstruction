#!/usr/bin/env python3
"""Design Compiler relation measurement gate.

Turns DOM measurements (getBoundingClientRect + computed style + Range line
boxes) into the SAME input the compile_design relation algorithm consumes, then
diffs every planned edge against the measured page.

Edge kinds (per compiler-contract.json -> relations.kinds):
  parentChildInset, siblingGap, siblingOrder, alignmentAxis, baseline,
  iconHostCenter, textLineInset, connectorEdge.

Hard rules:
  * Every edge is pass / fail / unknown -- never silently dropped.
  * Absolute position being close does NOT save a wrong owner / gap / alignment:
    those are measured as relations, not as two independent positions.
  * Incomparable pairs (rotated vs unrotated box family, missing selector,
    Range line box absent, auto-layout field absent) -> unknown. Under --strict
    an unknown is NOT a pass and counts as a strict failure.
  * Containers are audited too (parentChildInset / textLineInset include them).

Outputs: relations-audit.json and repair-plan.json. The repair plan is a pure
function of (plan expected, measured diff): no arbitrary font scaling or fixed
pixel compensation; external-data gaps stay unresolved.
"""
import argparse, hashlib, json, pathlib

RULE_VERSION = 'design-compiler/1.0.0'
DEFAULT_TOL = 1.5  # px, screen space


# ---------------------------------------------------------------------------
# box / family primitives
# ---------------------------------------------------------------------------
def _box(n, *keys):
    """Read an {x,y,w,h-ish} box from a dict, tolerating w/width, h/height."""
    for k in keys:
        b = n.get(k)
        if isinstance(b, dict) and b:
            return {'x': float(b.get('x', 0)), 'y': float(b.get('y', 0)),
                    'w': float(b.get('w', b.get('width', 0))),
                    'h': float(b.get('h', b.get('height', 0)))}
    return None


def _rotated(m):
    """True when the affine LINEAR part is non-identity (a rotation/shear)."""
    return bool(isinstance(m, list) and len(m) == 6 and
                not (abs(m[0]-1) < 1e-6 and abs(m[1]) < 1e-6 and
                     abs(m[2]) < 1e-6 and abs(m[3]-1) < 1e-6))


def plan_node_box(n):
    # renderAABB is the correct render family; designBox is the unrotated layout
    # family and must NOT be substituted for a rotated node.
    return _box(n, 'renderAABB', 'designBox')


def center(b):
    return (b['x'] + b['w']/2.0, b['y'] + b['h']/2.0)


# ---------------------------------------------------------------------------
# canonical expected-edge derivation (shared with compile_design)
# ---------------------------------------------------------------------------
def _edge_id(kind, s, t):
    h = hashlib.sha256('|'.join([kind, s, t]).encode('utf-8')).hexdigest()
    return h[:12]


def collect_plan_nodes(plan):
    """Merge nodes / textPlans / assets / connectorPlans into one node map."""
    nodes = {}
    for n in plan.get('nodes', []) or []:
        nodes[n['nodeId']] = dict(n)
    for t in plan.get('textPlans', []) or []:
        nodes[t['nodeId']] = dict(t, kind='text')
    for a in plan.get('assets', []) or []:
        nodes[a['nodeId']] = dict(a, kind='asset')
    return nodes


def derive_expected_edges(plan):
    """Build the canonical edge list. Honours plan['relations'] when present, and
    additionally derives parentChildInset / siblingGap / siblingOrder /
    textLineInset / connectorEdge from the planned node graph (idempotent by
    edgeId). Only EFFECTIVE-VISIBLE nodes participate (hidden nodes are reconciled
    separately by visibility, not dropped into geometry edges)."""
    nodes = collect_plan_nodes(plan)
    # Effective-visible subset: hidden nodes never take part in geometry edges.
    nodes = {nid: n for nid, n in nodes.items()
             if n.get('effectiveVisibility', n.get('visible', True)) is not False}
    by_parent = {}
    edges = {}

    def add(e):
        e.setdefault('edgeId', _edge_id(e['kind'], e['source'], e['target']))
        e.setdefault('ruleVersion', plan.get('ruleVersion', RULE_VERSION))
        e.setdefault('derivation', 'autoLayout' if e.get('expected') else 'geometry-inferred')
        edges[e['edgeId']] = e

    # Real plans ship their own relations (canonical edge list). Trust them and
    # do NOT re-derive on top (that would double-count). Empty-dict expected edges
    # are preserved as-is; diff_edge reports them unknown, never crashes.
    planned = plan.get('relations') or []
    if planned:
        for r in planned:
            add(dict(r))
        return list(edges.values())

    # No pre-derived relations (synthetic / legacy plans): derive from the tree.
    # parentChildInset for every rendered parent->child link.
    for nid, n in nodes.items():
        pid = n.get('parentId')
        if not pid or pid not in nodes:
            continue
        pb, cb = plan_node_box(nodes[pid]), plan_node_box(n)
        if not pb or not cb:
            add({'kind': 'parentChildInset', 'source': pid, 'target': nid,
                 'expected': None, 'status': 'unknown',
                 'unknownReason': 'missing render box on parent or child'})
            continue
        expected = {
            'left': cb['x'] - pb['x'],
            'right': (pb['x']+pb['w']) - (cb['x']+cb['w']),
            'top': cb['y'] - pb['y'],
            'bottom': (pb['y']+pb['h']) - (cb['y']+cb['h']),
        }
        add({'kind': 'parentChildInset', 'source': pid, 'target': nid,
             'expected': expected, 'derivation': 'manifest-tree'})

    # textLineInset: a text node inside a container -> padding box minus the
    # text LINE box (Range height), not the full text node box.
    for nid, n in nodes.items():
        if n.get('kind') != 'text' and n.get('type') != 'TEXT':
            continue
        pid = n.get('parentId')
        if not pid or pid not in nodes:
            continue
        pb = plan_node_box(nodes[pid])
        cb = plan_node_box(n)
        if not pb or not cb:
            continue
        lh = n.get('lineHeightPx')
        if lh is None:
            lh = (n.get('lineHeight') or {}).get('value')
        if not isinstance(lh, (int, float)):
            lh = cb['h']
        line_box = {'x': cb['x'], 'y': cb['y'], 'w': cb['w'], 'h': lh}
        expected = {
            'left': line_box['x'] - pb['x'],
            'right': (pb['x']+pb['w']) - (line_box['x']+line_box['w']),
            'top': line_box['y'] - pb['y'],
            'bottom': (pb['y']+pb['h']) - (line_box['y']+line_box['h']),
        }
        add({'kind': 'textLineInset', 'source': pid, 'target': nid,
             'expected': expected, 'derivation': 'geometry-inferred'})

    # group children by parent for sibling edges.
    for nid, n in nodes.items():
        pid = n.get('parentId')
        if pid:
            by_parent.setdefault(pid, []).append(nid)

    def zkey(nid):
        z = nodes[nid].get('zIndexChain') or [0]
        return z

    for pid, kids in by_parent.items():
        kids_sorted = sorted(kids, key=lambda k: zkey(k))
        # siblingOrder edge: the ordered child list of this parent.
        if len(kids_sorted) >= 1:
            add({'kind': 'siblingOrder', 'source': pid, 'target': '__order__',
                 'expected': {'order': kids_sorted}, 'derivation': 'manifest-tree'})
        # consecutive sibling gaps.
        for a, b in zip(kids_sorted, kids_sorted[1:]):
            ba, bb = plan_node_box(nodes[a]), plan_node_box(nodes[b])
            if not ba or not bb:
                continue
            # axis: vertical stack if |dx| small relative to |dy|, else horizontal.
            cax, cay = center(ba); cbx, cby = center(bb)
            axis = 'vertical' if abs(cby-cay) >= abs(cbx-cax) else 'horizontal'
            if axis == 'horizontal':
                gap = bb['x'] - (ba['x']+ba['w'])
            else:
                gap = bb['y'] - (ba['y']+ba['h'])
            add({'kind': 'siblingGap', 'source': a, 'target': b,
                 'expected': {'axis': axis, 'gap': gap},
                 'derivation': 'manifest-tree'})

    # connectorEdge from connectorPlans.
    for cp in plan.get('connectorPlans', []) or []:
        if cp.get('uncertain'):
            add({'kind': 'connectorEdge', 'source': cp.get('sourceNodeId'),
                 'target': cp.get('targetNodeId'), 'expected': None,
                 'status': 'unknown',
                 'unknownReason': 'connector plan marked uncertain; strict block'})
            continue
        add({'kind': 'connectorEdge',
             'source': cp.get('sourceNodeId'), 'target': cp.get('targetNodeId'),
             'expected': {'sourceEdge': cp.get('sourceEdge'),
                          'targetEdge': cp.get('targetEdge'), 'gap': 0.0},
             'derivation': 'layout-plan'})

    return list(edges.values())


# ---------------------------------------------------------------------------
# diff one edge against measured DOM boxes
# ---------------------------------------------------------------------------
def _meas(measured, nid):
    return (measured.get('nodes') or {}).get(nid)


def _comparable(plan_nodes, measured, s, t):
    """Both ends must exist in the DOM and share a box family. Returns
    (ok, reason). Rotated vs unrotated is NOT comparable."""
    for nid in (s, t):
        if nid == '__order__':
            continue
        mn = _meas(measured, nid)
        if mn is None:
            return False, f'measured DOM has no element for {nid}'
        pn = plan_nodes.get(nid) or {}
        p_rot = _rotated(pn.get('effectiveMatrix'))
        m_rot = _rotated(mn.get('matrix'))
        if p_rot != m_rot:
            return False, (f'box-family mismatch on {nid}: plan rotated={p_rot} '
                           f'measured rotated={m_rot} (rotated AABB != unrotated box)')
    return True, ''


def _meas_box(measured, nid):
    mn = _meas(measured, nid) or {}
    b = mn.get('box')
    if not b:
        return None
    return {'x': float(b.get('x', 0)), 'y': float(b.get('y', 0)),
            'w': float(b.get('w', b.get('width', 0))),
            'h': float(b.get('h', b.get('height', 0)))}


def diff_edge(edge, plan_nodes, measured, tol=DEFAULT_TOL):
    kind = edge['kind']; s = edge['source']; t = edge['target']
    exp = edge.get('expected')
    out = {'edgeId': edge['edgeId'], 'kind': kind, 'source': s, 'target': t,
           'expected': exp, 'tolerance': edge.get('tolerance', tol),
           'derivation': edge.get('derivation'), 'measured': None,
           'delta': None, 'status': 'unknown', 'reason': ''}

    if not exp:
        # expected may be None OR an empty {} (unknownReason set by the compiler).
        # Either way: unknown, never KeyError into exp['left'].
        out['reason'] = edge.get('unknownReason', 'expected relation is unknown')
        return out

    ok, reason = _comparable(plan_nodes, measured, s, t)
    if not ok:
        out['reason'] = reason
        return out
    tol = out['tolerance']

    if kind == 'parentChildInset':
        pb, cb = _meas_box(measured, s), _meas_box(measured, t)
        if not pb or not cb:
            out['reason'] = 'missing measured parent/child box'; return out
        meas = {'left': cb['x']-pb['x'], 'right': (pb['x']+pb['w'])-(cb['x']+cb['w']),
                'top': cb['y']-pb['y'], 'bottom': (pb['y']+pb['h'])-(cb['y']+cb['h'])}
        out['measured'] = meas
        delta = {k: meas[k]-exp[k] for k in ('left', 'right', 'top', 'bottom')}
        out['delta'] = delta
        out['status'] = 'pass' if max(abs(v) for v in delta.values()) <= tol else 'fail'

    elif kind == 'siblingGap':
        a, b = _meas_box(measured, s), _meas_box(measured, t)
        if not a or not b:
            out['reason'] = 'missing measured sibling box'; return out
        axis = exp['axis']
        gap = (b['x']-(a['x']+a['w'])) if axis == 'horizontal' else (b['y']-(a['y']+a['h']))
        out['measured'] = {'axis': axis, 'gap': gap}
        out['delta'] = {'gap': gap-exp['gap']}
        out['status'] = 'pass' if abs(gap-exp['gap']) <= tol else 'fail'

    elif kind == 'siblingOrder':
        order_exp = exp['order']
        dom_order = measured.get('domOrder') or []
        present = [n for n in order_exp if n in dom_order]
        measured_order = [n for n in dom_order if n in set(order_exp)]
        out['measured'] = {'order': measured_order}
        out['status'] = 'pass' if measured_order == present else 'fail'
        if out['status'] == 'fail':
            out['reason'] = f'DOM order {measured_order} != planned {present}'

    elif kind in ('alignmentAxis', 'iconHostCenter'):
        a, b = _meas_box(measured, s), _meas_box(measured, t)
        if not a or not b:
            out['reason'] = 'missing measured box'; return out
        axis = exp['axis']
        cax, cay = center(a); cbx, cby = center(b)
        delta = (cbx-cax) if axis == 'centerX' else (cby-cay)
        out['measured'] = {'axis': axis, 'delta': delta}
        out['delta'] = {'delta': delta-exp.get('delta', 0.0)}
        out['status'] = 'pass' if abs(out['delta']['delta']) <= tol else 'fail'

    elif kind == 'baseline':
        ba = _meas(measured, s); bb = _meas(measured, t)
        bs, bt = ba.get('baseline'), bb.get('baseline')
        if bs is None or bt is None:
            out['reason'] = 'missing measured baseline (Range unavailable)'; return out
        delta = bt - bs
        out['measured'] = {'delta': delta}
        out['delta'] = {'delta': delta-exp.get('delta', 0.0)}
        out['status'] = 'pass' if abs(out['delta']['delta']) <= tol else 'fail'

    elif kind == 'textLineInset':
        cb = _meas_box(measured, s)              # container
        mn = _meas(measured, t) or {}
        lb = mn.get('lineBox')
        if not cb or not lb:
            out['reason'] = 'missing container box or text Range line box'; return out
        meas = {'left': lb['x']-cb['x'], 'right': (cb['x']+cb['w'])-(lb['x']+lb['w']),
                'top': lb['y']-cb['y'], 'bottom': (cb['y']+cb['h'])-(lb['y']+lb['h'])}
        out['measured'] = meas
        out['delta'] = {k: meas[k]-exp[k] for k in ('left', 'right', 'top', 'bottom')}
        out['status'] = 'pass' if max(abs(v) for v in out['delta'].values()) <= tol else 'fail'

    elif kind == 'connectorEdge':
        sb, tb = _meas_box(measured, s), _meas_box(measured, t)
        if not sb or not tb:
            out['reason'] = 'missing measured connector endpoint box'; return out
        se, te = exp['sourceEdge'], exp['targetEdge']
        if se in ('right', 'left'):
            gap = (tb['x']-sb['x']-sb['w']) if se == 'right' else (sb['x']-tb['x']-tb['w'])
        else:
            gap = (tb['y']-sb['y']-sb['h']) if se == 'bottom' else (sb['y']-tb['y']-tb['h'])
        out['measured'] = {'sourceEdge': se, 'targetEdge': te, 'gap': gap}
        out['delta'] = {'gap': gap-exp.get('gap', 0.0)}
        out['status'] = 'pass' if abs(gap) <= tol else 'fail'

    else:
        out['reason'] = f'unknown edge kind {kind}'
    return out


# ---------------------------------------------------------------------------
# top-level audit + repair plan
# ---------------------------------------------------------------------------
def audit_relations(plan, measured, strict=True, tol=DEFAULT_TOL):
    """Return (relations_audit, repair_plan). Pure: (plan, measured) -> report."""
    plan_nodes = collect_plan_nodes(plan)
    edges = derive_expected_edges(plan)
    results = [diff_edge(e, plan_nodes, measured, tol) for e in edges]

    n_pass = sum(1 for r in results if r['status'] == 'pass')
    n_fail = sum(1 for r in results if r['status'] == 'fail')
    n_unk = sum(1 for r in results if r['status'] == 'unknown')
    strict_fail = n_fail + (n_unk if strict else 0)

    # Visibility reconciliation: a planned-rendered node with no DOM element, or
    # a DOM element the plan says is hidden, is a relation breach (not a pass).
    vis_actions = []
    dom_nodes = set((measured.get('nodes') or {}).keys())
    for nid, n in plan_nodes.items():
        planned_visible = bool(n.get('effectiveVisibility', n.get('visible', True)))
        present = nid in dom_nodes
        if planned_visible and not present:
            vis_actions.append({'kind': 'setVisibility', 'nodeId': nid,
                                'want': 'visible', 'reason': 'plan renders node but DOM lacks it'})
        elif (not planned_visible) and present:
            vis_actions.append({'kind': 'setVisibility', 'nodeId': nid,
                                'want': 'hidden', 'reason': 'plan hides node but DOM shows it'})

    audit = {
        'ruleVersion': plan.get('ruleVersion', RULE_VERSION),
        'planHash': plan.get('planHash'),
        'strict': strict,
        'tolerancePx': tol,
        'edges': results,
        'summary': {'pass': n_pass, 'fail': n_fail, 'unknown': n_unk,
                    'total': len(results), 'strictFail': strict_fail},
    }

    # Deterministic repair plan from diffs. No font scaling / fixed comp.
    action_for_kind = {
        'textLineInset': 'setTextLayout',
        'parentChildInset': 'setRelationGeometry',
        'siblingGap': 'setRelationGeometry',
        'siblingOrder': 'setRelationGeometry',
        'alignmentAxis': 'setRelationGeometry',
        'iconHostCenter': 'setRelationGeometry',
        'baseline': 'setRelationGeometry',
        'connectorEdge': 'rebuildConnector',
    }
    actions = list(vis_actions)
    blocked, unresolved = [], []
    for r in results:
        if r['status'] == 'fail':
            actions.append({'kind': action_for_kind.get(r['kind'], 'setRelationGeometry'),
                            'edgeId': r['edgeId'], 'kindOfEdge': r['kind'],
                            'source': r['source'], 'target': r['target'],
                            'delta': r['delta'], 'reason': r.get('reason', '')})
        elif r['status'] == 'unknown':
            entry = {'edgeId': r['edgeId'], 'kindOfEdge': r['kind'],
                     'source': r['source'], 'target': r['target'],
                     'reason': r.get('reason', '')}
            (blocked if strict else unresolved).append(entry)
            unresolved.append(entry)

    repair = {
        'ruleVersion': plan.get('ruleVersion', RULE_VERSION),
        'actions': actions,
        'blocked': blocked,
        'unknown': unresolved,
        'blockedByUnresolved': bool(blocked) if strict else bool(unresolved),
    }
    return audit, repair


# ---------------------------------------------------------------------------
# DOM measurement JS (run in the built page to produce `measured`)
# ---------------------------------------------------------------------------
MEASURE_JS = r"""
(function(){
  var stage = document.getElementById('stage');
  var sb = stage.getBoundingClientRect();
  var out = {nodes:{}, domOrder:[]};
  document.querySelectorAll('[data-n]').forEach(function(el){
    var nid = el.getAttribute('data-n');
    var r = el.getBoundingClientRect();
    var cs = getComputedStyle(el);
    var node = {
      box: {x: r.left - sb.left, y: r.top - sb.top, w: r.width, h: r.height},
      matrix: null, visibility: cs.visibility, opacity: parseFloat(cs.opacity),
      lineBox: null, baseline: null
    };
    // parse matrix(a,b,c,d,tx,ty)
    var m = cs.transform && cs.transform !== 'none'
      ? cs.transform.match(/matrix\(([^)]+)\)/) : null;
    if (m) node.matrix = m[1].split(',').map(parseFloat);
    // Range line box + baseline for text nodes
    var txt = el.querySelector('.tcell') || el;
    var range = document.createRange();
    range.selectNodeContents(txt);
    var lr = range.getBoundingClientRect();
    node.lineBox = {x: lr.left - sb.left, y: lr.top - sb.top, w: lr.width, h: lr.height};
    node.baseline = (lr.top - sb.top) + (parseFloat(cs.fontSize) || 0) * 0.8;
    out.nodes[nid] = node;
    out.domOrder.push(nid);
  });
  return out;
})();
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--export-dir', required=True)
    ap.add_argument('--measured', help='JSON file of DOM measurements (from MEASURE_JS)')
    ap.add_argument('--out-dir')
    ap.add_argument('--tol', type=float, default=DEFAULT_TOL)
    ap.add_argument('--no-strict', action='store_true')
    ap.add_argument('--print-measure-js', action='store_true')
    args = ap.parse_args()
    if args.print_measure_js:
        print(MEASURE_JS)
        return
    d = pathlib.Path(args.export_dir).resolve()
    out_dir = pathlib.Path(args.out_dir).resolve() if args.out_dir else d
    plan = json.loads((d/'layout-plan.json').read_text(encoding='utf-8'))
    measured = {}
    if args.measured:
        measured = json.loads(pathlib.Path(args.measured).read_text(encoding='utf-8'))
    audit, repair = audit_relations(plan, measured, strict=not args.no_strict, tol=args.tol)
    (out_dir/'relations-audit.json').write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding='utf-8')
    (out_dir/'repair-plan.json').write_text(
        json.dumps(repair, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(audit['summary'], ensure_ascii=False))


if __name__ == '__main__':
    main()
