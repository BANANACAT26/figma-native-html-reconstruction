#!/usr/bin/env python3
"""Generic unit / synthetic regressions for the builder + geometry.

No artboard IDs, no node offsets, no fixed angles are hardcoded here. The
rotation fixture builds a generic translate->rotate nested tree, derives the
affine matrices with the same math the exporter ships, and asserts the builder
reproduces that matrix verbatim (no local-box + CSS-rotate drift).

Run:  python3 tests/test_builder_regressions.py
Exits non-zero on first failure.
"""
import json, math, os, sys, tempfile, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, 'scripts')
sys.path.insert(0, SCRIPTS)

import build_fixed_html as bfh  # noqa: E402
import export_artboard as ex     # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(('  ok  ' if cond else ' FAIL ') + name + (('  -- ' + str(detail)) if detail and not cond else ''))


# ---------------------------------------------------------------------------
# 1. Porter-Duff over compositing
# ---------------------------------------------------------------------------
def _top_level_commas(s):
    """Count commas that separate CSS background layers (outside parens)."""
    depth = 0; n = 0
    for ch in s:
        if ch == '(': depth += 1
        elif ch == ')': depth -= 1
        elif ch == ',' and depth == 0: n += 1
    return n


def test_composite():
    print('[1] color compositing')
    # opaque black over opaque white -> black
    c = bfh.over((0, 0, 0, 1), (1, 1, 1, 1))
    check('black-over-white=black', abs(c[0]-0) < 1e-6 and abs(c[3]-1) < 1e-6, c)
    # 50% blue over red
    c = bfh.over((0, 0, 1, 0.5), (1, 0, 0, 1))
    check('0.5blue-over-red', abs(c[0]-0.5) < 1e-6 and abs(c[2]-0.5) < 1e-6, c)
    # top transparent -> bottom
    c = bfh.over((1, 0, 0, 0.0), (0, 1, 0, 1))
    check('transparent-top-keeps-bottom', abs(c[1]-1) < 1e-6, c)


# ---------------------------------------------------------------------------
# 2. Background CSS: deterministic, valid, no invalid comma-color pair
# ---------------------------------------------------------------------------
def test_background():
    print('[2] background stack')
    # F02 root: white bottom, black top -> single opaque black, not two colors
    css, disc = bfh.build_background_css([
        {'type': 'SOLID', 'color': {'r': 1, 'g': 1, 'b': 1}, 'opacity': 1},
        {'type': 'SOLID', 'color': {'r': 0, 'g': 0, 'b': 0}, 'opacity': 1},
    ])
    check('root multi-solid composites to black', css.startswith('rgba(0,0,0'), css)
    check('root has no disclosures', len(disc) == 0, disc)
    # a valid CSS background may have at most the trailing color; two bare colors
    # separated by a top-level comma is invalid. Our output must be one layer.
    check('no comma-separated two-color background', _top_level_commas(css) == 0, css)

    # F03 root: black, IMAGE(unresolved), purple 0.6 tint -> disclose, no white
    css, disc = bfh.build_background_css([
        {'type': 'SOLID', 'color': {'r': 0, 'g': 0, 'b': 0}, 'opacity': 1},
        {'type': 'IMAGE', 'scaleMode': 'FILL'},
        {'type': 'SOLID', 'color': {'r': 0.13, 'g': 0.01, 'b': 0.19}, 'opacity': 0.6},
    ])
    check('unresolved IMAGE disclosed', any(d['kind'] == 'image-fill-unresolved' for d in disc), disc)
    check('no whitewash (no rgb(255,255,255))', '255,255,255' not in css, css)
    check('purple tint emitted as flat gradient layer', 'linear-gradient(rgba(33,' in css, css)

    # resolved image layers correctly
    css2, disc2 = bfh.build_background_css([
        {'type': 'SOLID', 'color': {'r': 0, 'g': 0, 'b': 0}, 'opacity': 1},
        {'type': 'IMAGE', 'scaleMode': 'FILL'},
    ], resolve_image=lambda f: 'bg.png')
    check('resolved IMAGE becomes url() layer', 'url("bg.png")' in css2, css2)
    check('resolved image has no disclosure', len(disc2) == 0, disc2)


# ---------------------------------------------------------------------------
# 3. Text fill classification: never fake #000
# ---------------------------------------------------------------------------
def test_text_fills():
    print('[3] text fill classification')
    grad = bfh.composite_text_color([{'type': 'GRADIENT_LINEAR', 'visible': True, 'cssAngle': 90,
                                      'gradientStops': [{'position': 0, 'color': {'r': 1, 'g': 0.3, 'b': 0.9, 'a': 1}},
                                                        {'position': 1, 'color': {'r': 0.5, 'g': 0, 'b': 0.8, 'a': 1}}]}])
    check('gradient text uses background-clip', 'background-clip:text' in grad['css'] and '#000' not in grad['css'], grad['css'])
    none = bfh.composite_text_color([])
    check('no fill -> transparent (not #000)',
          none['css'] == 'color:transparent;' and any(d['kind'] == 'text-no-fill' for d in none['disclosures']),
          none)
    img = bfh.composite_text_color([{'type': 'IMAGE', 'visible': True}])
    check('image text disclosed not faked', img['mode'] == 'image' and any(d['kind'] == 'text-image-unsupported' for d in img['disclosures']), img)
    sol = bfh.composite_text_color([{'type': 'SOLID', 'visible': True, 'color': {'r': 0.2, 'g': 0.2, 'b': 0.2, 'a': 1}}])
    check('solid text composites', sol['css'].startswith('color:rgba(51,'), sol['css'])

    # gradient + solid must DISCLOSE, not silently swallow the solid
    gs = bfh.composite_text_color([
        {'type': 'GRADIENT_LINEAR', 'visible': True, 'cssAngle': 90,
         'gradientStops': [{'position': 0, 'color': {'r': 1, 'g': .3, 'b': .9, 'a': 1}}]},
        {'type': 'SOLID', 'visible': True, 'color': {'r': 1, 'g': 0, 'b': 0, 'a': 1}}])
    check('gradient+solid disclosed', any(d['kind'] == 'text-gradient-with-solid' for d in gs['disclosures']), gs['disclosures'])


# ---------------------------------------------------------------------------
# 4b. rotated container uses the same affine path, not axis-aligned rect
# ---------------------------------------------------------------------------
def test_rotated_container():
    print('[4b] rotated container affine')
    # parent translate (120,90) rotate 30; container local (40,25) 220x44
    parent_m = ex.node_matrix(120, 90, 360, 360, 30)
    cabs = ex.mat_mul(parent_m, ex.node_matrix(40, 25, 220, 44, 0))
    rb = ex.render_bounds(cabs, 220, 44)
    c = {'nodeId': 'fixture-cont-rot', 'zIndexChain': [1], 'rotation': 30, 'opacity': 1,
         'renderAABB': {k: round(v, 3) for k, v in rb.items()},
         'absoluteTransform': [round(v, 4) for v in cabs],
         'local': {'x': 40, 'y': 25, 'width': 220, 'height': 44},
         'fills': [{'type': 'SOLID', 'visible': True, 'color': {'r': 1, 'g': 0, 'b': 0}, 'opacity': 1}],
         'strokes': [], 'cornerRadius': 4}
    out = bfh.container_layer(c, stage_size=(1000, 600))
    html = out['html']
    check('rotated container uses matrix transform', 'transform:matrix(' in html, html[:160])
    check('does NOT place by axis-aligned renderAABB left/top',
          f'left:{rb["x"]:.4f}px' not in html and f'top:{rb["y"]:.4f}px' not in html, html[:200])
    check('inner box sized to local w/h', 'width:220.000px;height:44.000px' in html, html)
    check('no rotation-unverified disclosure', not any(d['kind'] == 'rotation-unverified' for d in out['disclosures']), out['disclosures'])


# ---------------------------------------------------------------------------
# 4. zIndexChain=null must not crash
# ---------------------------------------------------------------------------
def test_zchain_null():
    print('[4] zIndexChain=null safety')
    check('null chain -> empty tuple', bfh._zchain({'zIndexChain': None}) == ())
    check('missing chain -> empty tuple', bfh._zchain({}) == ())
    check('real chain preserved', bfh._zchain({'zIndexChain': [2, 0]}) == (2, 0))


# ---------------------------------------------------------------------------
# 5. Generic nested translate+rotate affine (the core geometry regression)
# ---------------------------------------------------------------------------
def test_nested_rotate():
    print('[5] nested translate+rotate affine')
    # Build a generic tree:
    #   artboard 1000x600 (identity root)
    #   parent frame local (120, 90), size 360x360, rotation 30deg
    #   child text local (40, 25) inside parent, size 220x44, rotation 0
    root = [1, 0, 0, 1, 0, 0]
    parent_m = ex.node_matrix(120, 90, 360, 360, 30)
    child_local = ex.node_matrix(40, 25, 220, 44, 0)
    child_abs = ex.mat_mul(parent_m, child_local)

    # where does the text local-origin land in artboard coords?
    ox, oy = ex.apply(child_abs, 0, 0)
    rb = ex.render_bounds(child_abs, 220, 44)

    t = {
        'nodeId': 'fixture-text-1', 'characters': 'hello rotated', 'visible': True,
        'opacity': 1, 'fontName': {'family': 'Demo', 'style': 'Regular'}, 'fontSize': 16,
        'fills': [{'type': 'SOLID', 'visible': True, 'color': {'r': 1, 'g': 1, 'b': 1}, 'opacity': 1}],
        'relativeToArtboard': {k: round(v, 3) for k, v in rb.items()},
        'local': {'x': 40, 'y': 25, 'width': 220, 'height': 44},
        'absoluteTransform': [round(x, 4) for x in child_abs],
        'rotation': 0, 'zIndexChain': [1],
    }
    out = bfh.text_layer(t, stage_size=(1000, 600))
    html = out['html']
    check('rotated/nested text uses matrix transform', 'transform:matrix(' in html, html[:200])
    # matrix values must equal child_abs (order a,b,c,d,tx,ty)
    expected = 'matrix(' + ','.join(f'{v:.4f}' for v in child_abs) + ')'
    check('emitted matrix equals accumulated affine', expected in html, (expected, html[:260]))
    # The local-origin must map to (ox,oy): tx,ty are child_abs[4],child_abs[5]
    check('matrix translation is the artboard origin',
          abs(child_abs[4] - ox) < 1e-6 and abs(child_abs[5] - oy) < 1e-6, (ox, oy, child_abs[4:6]))

    # A -180 text like the bubble case: parent at (300,200), text local (426,28),
    # rotation -180. The accumulated matrix must reproduce a flipped rect, and the
    # builder must NOT use local x/y as artboard coords.
    pm = ex.node_matrix(300, 200, 500, 300, 0)
    tm = ex.node_matrix(426, 28, 398, 54, -180)
    tab = ex.mat_mul(pm, tm)
    rb2 = ex.render_bounds(tab, 398, 54)
    t2 = {'nodeId': 'fixture-text-2', 'characters': 'bubble', 'visible': True, 'opacity': 1,
          'fontName': {'family': 'Demo', 'style': 'Regular'}, 'fontSize': 14,
          'fills': [{'type': 'SOLID', 'visible': True, 'color': {'r': .18, 'g': .18, 'b': .18}, 'opacity': 1}],
          'relativeToArtboard': {k: round(v, 3) for k, v in rb2.items()},
          'local': {'x': 426, 'y': 28, 'width': 398, 'height': 54},
          'absoluteTransform': [round(x, 4) for x in tab], 'rotation': -180, 'zIndexChain': [2]}
    out2 = bfh.text_layer(t2, stage_size=(1000, 600))
    check('-180 bubble text uses matrix path', 'transform:matrix(' in out2['html'], out2['html'][:200])
    # local (426,28) must NOT appear as left/top artboard coords
    check('does not naively place at local coords', 'left:426.0000px' not in out2['html'], out2['html'][:200])


# ---------------------------------------------------------------------------
# 6. End-to-end build from the on-disk fixture (no crash, disclosures collected)
# ---------------------------------------------------------------------------
def test_fixture_build():
    print('[6] end-to-end fixture build')
    man = json.load(open(os.path.join(HERE, 'fixtures_builder', 'manifest_multisolid.json')))
    tm = {'texts': []}
    with tempfile.TemporaryDirectory() as td:
        doc, report = bfh.build_html(man, tm, td, os.path.join(HERE, 'fixtures_builder'),
                                     font_css_text='', title='t')
    check('build returns html', '<div id="stage">' in doc)
    check('root background composited black', 'background:rgba(0,0,0,1.0000)' in doc or 'background:rgb(0, 0, 0)' in doc or 'background:rgba(0,0,0,1.0' in doc, report['rootBackgroundCss'])
    # container had z=null -> must have survived sort
    check('container with z=null rendered', 'container-visual' in doc, doc[:400])
    # container two solids: red 1 over blue .5 -> composite, one background value
    import re
    m = re.search(r'container-visual[^>]*style="([^"]*)"', doc)
    check('container layer present', m is not None)
    if m:
        style = m.group(1)
        bg = style.split('background:')[1].split(';')[0]
        check('container bg is single color (not two layers)', _top_level_commas(bg) == 0, bg)


# ---------------------------------------------------------------------------
# 7. Optional Playwright check: CSS matrix lands where math predicts
# ---------------------------------------------------------------------------
def test_browser_matrix():
    print('[7] browser matrix placement (optional)')
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print('  skip playwright:', e)
        return
    # parent translate (120,90) rotate 30, child local (40,25) 220x44
    parent_m = ex.node_matrix(120, 90, 360, 360, 30)
    child_abs = ex.mat_mul(parent_m, ex.node_matrix(40, 25, 220, 44, 0))
    corners = [ex.apply(child_abs, 0, 0), ex.apply(child_abs, 220, 0),
               ex.apply(child_abs, 220, 44), ex.apply(child_abs, 0, 44)]
    minx = min(c[0] for c in corners); miny = min(c[1] for c in corners)
    maxx = max(c[0] for c in corners); maxy = max(c[1] for c in corners)
    mat = 'matrix(' + ','.join(f'{v:.4f}' for v in child_abs) + ')'
    html = f'''<html><body style="margin:0">
    <div id="stage" style="position:relative;width:1000px;height:600px">
      <div id="t" style="position:absolute;left:0;top:0;width:1000px;height:600px;transform:{mat};transform-origin:0 0">
        <div style="position:absolute;left:0;top:0;width:220px;height:44px;background:red">x</div>
      </div>
    </div></body></html>'''
    with tempfile.NamedTemporaryFile('w', suffix='.html', delete=False) as f:
        f.write(html); path = f.name
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(channel='chrome', headless=True,
                                  args=['--force-device-scale-factor=1'])
            pg = b.new_page(viewport={'width': 1000, 'height': 600}, device_scale_factor=1)
            pg.goto('file://' + path); pg.wait_for_load_state('networkidle')
            box = pg.locator('#t div').bounding_box()
            b.close()
        check('browser AABB matches math',
              abs(box['x']-minx) < 1.5 and abs(box['y']-miny) < 1.5 and
              abs((box['x']+box['width'])-maxx) < 1.5 and abs((box['y']+box['height'])-maxy) < 1.5,
              (box, (minx, miny, maxx, maxy)))
    except Exception as e:
        print('  skip browser check (chrome unavailable):', e)


def test_connector_strict_gate():
    print('[8] connector strict gate (missing-asset counted, non-zero exit)')
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception as e:
        print('  skip (no playwright):', e); return
    import subprocess
    html = '''<html><body style="margin:0">
    <div id="stage" style="position:relative;width:800px;height:600px">
      <div id="src" style="position:absolute;left:0;top:200px;width:100px;height:100px;background:#888"></div>
      <div id="tgt" style="position:absolute;left:600px;top:200px;width:100px;height:100px;background:#888"></div>
      <img id="conn-ok" style="position:absolute;left:100px;top:230px;width:500px;height:40px" src="">
      <img id="conn-bad" style="position:absolute;left:100px;top:300px;width:300px;height:40px" src="">
    </div></body></html>'''
    spec = {
        'width': 800, 'height': 600,
        'nodes': [],
        'connectors': [
            {'id': 'good', 'selector': '#conn-ok', 'sourceSelector': '#src',
             'targetSelector': '#tgt', 'sourceEdge': 'right', 'targetEdge': 'left', 'tolerancePx': 2},
            {'id': 'short', 'selector': '#conn-bad', 'sourceSelector': '#src',
             'targetSelector': '#tgt', 'sourceEdge': 'right', 'targetEdge': 'left', 'tolerancePx': 2},
            {'id': 'ghost', 'selector': '#nope', 'sourceSelector': '#src',
             'targetSelector': '#tgt', 'sourceEdge': 'right', 'targetEdge': 'left', 'tolerancePx': 2},
        ]}
    with tempfile.TemporaryDirectory() as td:
        hp = os.path.join(td, 'p.html'); sp = os.path.join(td, 'spec.json'); op = os.path.join(td, 'out.json')
        open(hp, 'w').write(html); open(sp, 'w').write(json.dumps(spec))
        audit = os.path.join(SCRIPTS, 'audit_geometry.py')
        # non-strict: exits 0 regardless
        r0 = subprocess.run([sys.executable, audit, '--html', hp, '--spec', sp, '--out', op],
                            capture_output=True, text=True)
        check('non-strict exits 0', r0.returncode == 0, r0.returncode)
        out = json.load(open(op))
        cs = out['connectorSummary']
        check('good connector passes', cs['pass'] == 1, cs)
        check('short connector is edge-gap', cs['edgeGap'] == 1, cs)
        check('ghost connector is missing-asset (counted)', cs['missingAsset'] == 1, cs)
        # strict: must exit non-zero (edge-gap + missing-asset)
        r1 = subprocess.run([sys.executable, audit, '--html', hp, '--spec', sp, '--out', op, '--strict'],
                            capture_output=True, text=True)
        check('strict exits non-zero when edge-gap/missing-asset', r1.returncode != 0, r1.returncode)


# ---------------------------------------------------------------------------
# 9. Generic IMAGE fill parsing: imageRef / imageHash / imageTransform
# ---------------------------------------------------------------------------
def test_image_fill_spec():
    print('[9] generic IMAGE fill identity')
    k, kind = ex.image_fill_spec({'type': 'IMAGE', 'imageRef': 'abc-123'})
    check('imageRef recognized', k == 'abc-123' and kind == 'imageRef', (k, kind))
    k, kind = ex.image_fill_spec({'type': 'IMAGE', 'imageHash': 'deadbeef'})
    check('imageHash recognized (plugin paints)', k == 'deadbeef' and kind == 'imageHash', (k, kind))
    # imageRef wins when both are present (REST key takes precedence)
    k, kind = ex.image_fill_spec({'type': 'IMAGE', 'imageRef': 'r', 'imageHash': 'h'})
    check('imageRef preferred over imageHash', k == 'r' and kind == 'imageRef', (k, kind))
    # an IMAGE paint with no key must be unresolvable -- disclosed, not faked
    k, kind = ex.image_fill_spec({'type': 'IMAGE', 'scaleMode': 'FILL'})
    check('key-less IMAGE -> (None, None)', k is None and kind is None, (k, kind))
    k, kind = ex.image_fill_spec({'type': 'SOLID', 'color': {}})
    check('non-IMAGE paint -> (None, None)', k is None and kind is None, (k, kind))
    k, kind = ex.image_fill_spec(None)
    check('non-dict -> (None, None)', k is None and kind is None, (k, kind))


def test_resolver_factory():
    print('[9b] generic image resolver factory')
    man = {'imageAssetMap': {'ref-A': 'anon-bg-0001.png'},
           'rootBackgroundAsset': None}
    here = os.path.join(HERE, 'fixtures_builder')
    res = bfh.resolve_image_factory(man, here, here)
    url = res({'type': 'IMAGE', 'imageRef': 'ref-A'})
    check('resolves by imageRef key', url is not None and 'anon-bg-0001.png' in url, url)
    # imageHash also binds
    man2 = {'imageAssetMap': {'hash-B': 'anon-bg-0001.png'},
            'rootBackgroundAsset': None}
    res2 = bfh.resolve_image_factory(man2, here, here)
    url2 = res2({'type': 'IMAGE', 'imageHash': 'hash-B'})
    check('resolves by imageHash key', url2 is not None and 'anon-bg-0001.png' in url2, url2)
    # no key, no map entry -> None -> caller discloses, never white
    url3 = res2({'type': 'IMAGE', 'scaleMode': 'FILL'})
    check('key-less paint resolves to None (disclosed)', url3 is None, url3)
    # per-paint asset override still wins generically
    man3 = {'imageAssetMap': {}, 'rootBackgroundAsset': None}
    res3 = bfh.resolve_image_factory(man3, here, here)
    url4 = res3({'type': 'IMAGE', 'asset': 'anon-bg-0001.png'})
    check('per-paint asset override resolves', url4 is not None and 'anon-bg-0001.png' in url4, url4)


def test_root_image_fixture():
    print('[9c] end-to-end root-IMAGE fixture (resolved + unresolved)')
    man = json.load(open(os.path.join(HERE, 'fixtures_builder', 'manifest_root_image.json')))
    tm = {'texts': []}
    fx = os.path.join(HERE, 'fixtures_builder')
    with tempfile.TemporaryDirectory() as td:
        doc, report = bfh.build_html(man, tm, td, fx, font_css_text='', title='img')
    css = report['rootBackgroundCss']
    # Builder emits a temp-dir-relative path; the resolved fixture file must
    # appear inside a url(...) layer (proof the key binding resolved, not disclosed).
    check('root imageRef-bound paint emits url()',
          'url(' in css and 'anon-bg-0001.png' in css, css)
    # The 4th root IMAGE fill has no key -> must be disclosed, not painted
    kinds = {d['kind'] for d in report['disclosures']}
    check('key-less root IMAGE disclosed', 'image-fill-unresolved' in kinds, report['disclosures'])
    check('no whitewash (no pure white layer)', '255,255,255' not in css, css)
    # Container IMAGE bound by imageHash must also bind (generic, not root-only)
    check('container imageHash paint emits url()',
          'url(' in doc and 'anon-bg-0001.png' in doc, doc[:1200])


def test_blend_mode():
    """Layer blendMode must round-trip to CSS mix-blend-mode.

    Regression guard: exportAsync rasterizes a layer in isolation, so COLOR_BURN
    (and similar) is NOT baked into the PNG. Dropping the mode made a real
    artboard paste a stripe texture with normal alpha and wash out the gradient
    beneath it. Modes without a CSS equivalent must be disclosed, not dropped.
    """
    for figma_mode, css in (('COLOR_BURN', 'color-burn'),
                            ('MULTIPLY', 'multiply'),
                            ('SCREEN', 'screen'),
                            ('SOFT_LIGHT', 'soft-light'),
                            ('LUMINOSITY', 'luminosity')):
        s, d = bfh._blend_css({'nodeId': 'x', 'blendMode': figma_mode})
        check(f'{figma_mode} -> mix-blend-mode:{css}',
              s == f'mix-blend-mode:{css};' and d is None, s)
    s, d = bfh._blend_css({'nodeId': 'x', 'blendMode': 'LINEAR_BURN'})
    check('LINEAR_BURN approximated + disclosed',
          'mix-blend-mode' in s and d and d['kind'] == 'blend-mode-approximated', s)
    s, d = bfh._blend_css({'nodeId': 'x', 'blendMode': 'NORMAL'})
    check('NORMAL emits no mix-blend-mode', s == '' and d is None, s)
    s, d = bfh._blend_css({'nodeId': 'x'})
    check('missing blendMode defaults to normal', s == '' and d is None, s)

    plan = {
        'ruleVersion': 't',
        'artboard': {'artboardSize': {'w': 10, 'h': 10},
                     'rootFills': [], 'artboardId': 'root'},
        'nodes': [{'nodeId': 'a1', 'parentId': 'root', 'blendMode': 'COLOR_BURN',
                   'disposition': 'rendered-pixels-visible', 'zIndexChain': [0],
                   'renderAABB': {'x': 0, 'y': 0, 'w': 4, 'h': 4},
                   'pngPlacement': {'x': 0, 'y': 0},
                   'exportSize': {'w': 4, 'h': 4},
                   'asset': 'a1.png', 'effectiveOpacity': 1}],
        'assets': [{'nodeId': 'a1', 'parentId': 'root', 'blendMode': 'COLOR_BURN',
                    'disposition': 'rendered-pixels-visible', 'zIndexChain': [0],
                    'pngPlacement': {'x': 0, 'y': 0},
                    'exportSize': {'w': 4, 'h': 4},
                    'asset': 'a1.png', 'effectiveOpacity': 1}],
        'textPlans': [], 'connectorPlans': [], 'relations': [],
    }
    with tempfile.TemporaryDirectory() as td:
        open(os.path.join(td, 'a1.png'), 'wb').close()
        doc, report = bfh.build_from_plan(plan, td, td)
    check('planned asset keeps COLOR_BURN',
          'mix-blend-mode:color-burn' in doc, doc[:400])
    check('stage isolates blend context', 'isolation:isolate' in doc)


def main():
    test_composite()
    test_background()
    test_text_fills()
    test_rotated_container()
    test_zchain_null()
    test_nested_rotate()
    test_fixture_build()
    test_image_fill_spec()
    test_resolver_factory()
    test_root_image_fixture()
    test_browser_matrix()
    test_connector_strict_gate()
    test_blend_mode()
    print('\n=== RESULT ===')
    print(f'passed: {len(PASS)}  failed: {len(FAIL)}')
    if FAIL:
        print('FAILURES:'); [print(' -', f) for f in FAIL]
        sys.exit(1)


if __name__ == '__main__':
    main()
