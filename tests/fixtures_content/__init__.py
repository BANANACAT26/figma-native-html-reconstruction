"""Generic synthetic fixture builders for content-gate tests.

No node IDs, no real text, no specific colors, no fixed compensation or
artboard dimensions tied to a real Figma frame. Everything is built from
relative geometry so the tests exercise generic rules only.
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'scripts'))
import export_artboard as ex  # noqa: E402


def build_tree_with_hidden():
    """Artboard with: visible title, hidden subtitle, container with transparent
    child, and a clipped group. Returns (tree, units, texts, containers, ledger).

    Tree shape (indices are the z-order chain):
      root (0)
       ├─ child 0: visible frame (container) with solid fill, contains visible TEXT title
       ├─ child 1: hidden frame (visible=false) with TEXT subtitle inside
       └─ child 2: visible image asset (text-free subtree)
    """
    title = {
        'id': 'n-title', 'type': 'TEXT', 'name': 'title',
        'visible': True, 'opacity': 1,
        'characters': 'A',
        'fontName': {'family': 'Demo', 'style': 'Regular'}, 'fontSize': 16,
        'fills': [{'type': 'SOLID', 'visible': True, 'color': {'r': 0.2, 'g': 0.2, 'b': 0.2}, 'opacity': 1}],
        'x': 10, 'y': 10, 'width': 120, 'height': 24,
    }
    container = {
        'id': 'n-frame', 'type': 'FRAME', 'name': 'frame',
        'visible': True, 'opacity': 1, 'clipsContent': True,
        'fills': [{'type': 'SOLID', 'visible': True, 'color': {'r': 0.9, 'g': 0.9, 'b': 0.9}, 'opacity': 1}],
        'x': 0, 'y': 0, 'width': 200, 'height': 80,
        'children': [title],
    }
    hidden_text = {
        'id': 'n-subtitle', 'type': 'TEXT', 'name': 'subtitle',
        'visible': False, 'opacity': 1,
        'characters': 'B',
        'fills': [{'type': 'SOLID', 'visible': True, 'color': {'r': 0, 'g': 0, 'b': 0}, 'opacity': 1}],
        'x': 10, 'y': 40, 'width': 120, 'height': 20,
    }
    hidden_frame = {
        'id': 'n-hidden', 'type': 'FRAME', 'name': 'hidden',
        'visible': False, 'opacity': 1,
        'fills': [{'type': 'SOLID', 'visible': True, 'color': {'r': 1, 'g': 0, 'b': 0}, 'opacity': 1}],
        'x': 0, 'y': 100, 'width': 200, 'height': 40,
        'children': [hidden_text],
    }
    asset = {
        'id': 'n-asset', 'type': 'RECTANGLE', 'name': 'asset',
        'visible': True, 'opacity': 1,
        'fills': [{'type': 'SOLID', 'visible': True, 'color': {'r': 0.5, 'g': 0.5, 'b': 0.5}, 'opacity': 1}],
        'x': 0, 'y': 160, 'width': 200, 'height': 30,
    }
    root = {
        'id': 'n-root', 'type': 'FRAME', 'name': 'artboard',
        'visible': True, 'opacity': 1, 'clipsContent': True,
        'width': 200, 'height': 200,
        'children': [container, hidden_frame, asset],
    }
    return root


def run_annotate_and_collect(root):
    """Run the exporter's annotation + collect pass on a synthetic tree.

    Returns (units, texts, ledger) using the same functions export_artboard uses.
    """
    ex.annotate_tree(root)
    units = ex.collect_export_units(root)
    texts = ex.collect_texts(root)
    # Text-bearing containers are collected as containerVisuals by the real
    # exporter; mirror that here so the ledger doesn't flag them as unresolved.
    container_ids = set()
    def find_containers(n):
        for c in n.get('children', []) or []:
            if (c.get('type') in ('FRAME', 'GROUP', 'INSTANCE')
                    and c.get('_eff', {}).get('effective_visible', True)
                    and ex.has_text(c)):
                container_ids.add(c['id'])
            find_containers(c)
    find_containers(root)
    # Build a simple ledger from _eff + _disposition.
    ledger = []
    rendered = ({u['id'] for u in units} | {t['id'] for t in texts}
                | container_ids | {root['id']})
    def walk(n, parent_id=None):
        eff = n.get('_eff', {})
        if n['id'] in {u['id'] for u in units}:
            disp = 'rendered-asset'
        elif n['id'] in {t['id'] for t in texts}:
            disp = 'rendered-text'
        elif n['id'] in container_ids:
            disp = 'rendered-container'
        elif n['id'] == root['id']:
            disp = 'rendered-root'
        elif not eff.get('effective_visible', True):
            disp = n.get('_disposition', 'skipped-invisible')
        else:
            disp = 'unresolved'
        ledger.append({
            'nodeId': n.get('id'), 'parentId': parent_id,
            'type': n.get('type'),
            'selfVisible': eff.get('self_visible', True),
            'effectiveVisible': eff.get('effective_visible', True),
            'effectiveOpacity': round(eff.get('effective_opacity', 1.0), 4),
            'zIndexChain': n.get('_zchain'),
            'disposition': disp,
        })
        for c in n.get('children', []) or []:
            walk(c, n.get('id'))
    walk(root)
    return units, texts, ledger
