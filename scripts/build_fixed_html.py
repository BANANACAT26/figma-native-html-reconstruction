#!/usr/bin/env python3
"""Build fixed-artboard HTML from export_artboard.py manifests.

Generic baseline builder: no node-specific offsets, font paths or dimensions.
Complex cases must be resolved through the audit loop, not hidden overrides.

Design rules (generic, no artboard/node constants):
  * Multiple SOLID fills are composited with the Porter-Duff over operator in
    Figma order before being emitted as ONE color layer -- never a comma list of
    two colors (that is invalid CSS and silently drops the whole background).
  * IMAGE / gradient layers are emitted as real <bg-layer>s. When an IMAGE fill
    has no resolvable asset, it is *disclosed*, not whitewashed.
  * zIndexChain may be null/absent -> treated as an empty chain, never tuple(None).
  * Text fills are classified (none / solid / gradient / image). Unsupported or
    missing fills are disclosed and rendered transparent -- never faked #000.
  * Rotated / nested text uses the node's accumulated affine matrix
    (absoluteTransform) so the whole ancestor tree is reproduced in one generic
    CSS transform; no local-box + CSS-rotate drift, no angle special-casing.
"""
import argparse, html, json, os, pathlib, re

WEIGHT = {'Thin': 100, 'Extralight': 200, 'ExtraLight': 200, 'Light': 300,
          'Regular': 400, 'Medium': 500, 'Semibold': 600, 'SemiBold': 600,
          'Bold': 700, 'Heavy': 800, 'Black': 900}

# ---------------------------------------------------------------------------
# color primitives
# ---------------------------------------------------------------------------
def _f3(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _normalize_fills(fills):
    """Normalize a fills value into a list of dicts.

    Figma can return fills as the string ``"mixed"`` (per-run fills differ) or
    as ``None``. Neither may crash iteration. A non-list value is treated as an
    unknown fill that must be disclosed, never faked as #000.
    """
    if not isinstance(fills, list):
        if fills in (None, "", []):
            return []
        # e.g. fills="mixed" -> unknown, disclose.
        return [{'type': 'UNKNOWN', 'visible': True,
                 '_raw': str(fills),
                 'reason': f'fills value is non-list: {fills!r}; per-run mixed '
                           'fills cannot be resolved without per-segment data'}]
    out = []
    for f in fills:
        if isinstance(f, dict):
            out.append(f)
        # skip non-dict entries defensively
    return out


def solid_tuple(f):
    """Normalize a SOLID fill to (r,g,b,a) in 0..1. Missing -> transparent."""
    c = f.get('color', {}) or {}
    a = f.get('opacity', c.get('a', 1))
    return (_f3(c.get('r')), _f3(c.get('g')), _f3(c.get('b')), _f3(a, 1.0))


def over(top, bottom):
    """Porter-Duff source-over for two (r,g,b,a) tuples."""
    ta = top[3]
    ba = bottom[3]
    out_a = ta + ba * (1.0 - ta)
    if out_a <= 1e-9:
        return (0.0, 0.0, 0.0, 0.0)
    out_r = (top[0] * ta + bottom[0] * ba * (1.0 - ta)) / out_a
    out_g = (top[1] * ta + bottom[1] * ba * (1.0 - ta)) / out_a
    out_b = (top[2] * ta + bottom[2] * ba * (1.0 - ta)) / out_a
    return (out_r, out_g, out_b, out_a)


def _clamp01(v):
    return 0.0 if v < 0 else (1.0 if v > 1 else v)


def rgba_css(c, alpha_scale=1.0):
    r, g, b, a = c
    a = _clamp01(a * alpha_scale)
    return (f"rgba({round(_clamp01(r)*255)},{round(_clamp01(g)*255)},"
            f"{round(_clamp01(b)*255)},{a:.4f})")


def _flat_tint_css(c):
    """A translucent solid over a non-solid layer must be a <bg-image> layer
    (a bare color is only legal as the final layer). A flat gradient works."""
    col = rgba_css(c)
    return f"linear-gradient({col},{col})"


def _gradient_css(f):
    stops = []
    for s in f.get('gradientStops', []):
        c = s.get('color', {}) or {}
        stops.append(
            f"rgba({round(_f3(c.get('r'))*255)},{round(_f3(c.get('g'))*255)},"
            f"{round(_f3(c.get('b'))*255)},{_f3(c.get('a'),1.0):.4f}) "
            f"{100*_f3(s.get('position')):.3f}%")
    return f"linear-gradient({f.get('cssAngle',180)}deg,{','.join(stops)})"


_SCALE_OBJFIT = {'FILL': 'cover', 'FIT': 'contain', 'STRETCH': 'fill',
                 'CROP': 'cover', 'TILE': 'repeat'}


def _image_css(f, url):
    sm = f.get('scaleMode', 'FILL')
    fit = _SCALE_OBJFIT.get(sm, 'cover')
    if fit == 'repeat':
        return f'url("{html.escape(url,quote=True)}") repeat'
    return (f'url("{html.escape(url,quote=True)}") center/{fit} no-repeat')


def build_background_css(fills, resolve_image=None):
    """Compose a Figma fills list (bottom->top) into ONE valid CSS background.

    Returns (css_string, disclosures). Deterministic; never emits two bare
    colors; never whitewashes an unresolved IMAGE.
    """
    disclosures = []
    bottom = (0.0, 0.0, 0.0, 0.0)
    upper = []          # css layer strings, collected bottom->top
    below_non_solid = False
    fills = _normalize_fills(fills)
    for f in fills:
        if not isinstance(f, dict):
            continue
        if not f.get('visible', True):
            continue
        t = f.get('type')
        if t == 'SOLID':
            col = solid_tuple(f)
            if below_non_solid:
                upper.append(_flat_tint_css(col))
            else:
                bottom = over(col, bottom)
        elif t == 'GRADIENT_LINEAR':
            upper.append(_gradient_css(f))
            below_non_solid = True
        elif t == 'IMAGE':
            url = resolve_image(f) if resolve_image else None
            if url:
                upper.append(_image_css(f, url))
            else:
                disclosures.append({
                    'kind': 'image-fill-unresolved', 'type': 'IMAGE',
                    'scaleMode': f.get('scaleMode'),
                    'reason': 'IMAGE fill present but no resolvable image asset; '
                              'layer omitted explicitly (not whitewashed)'})
            below_non_solid = True
        else:
            disclosures.append({
                'kind': 'unsupported-fill', 'type': t,
                'reason': 'fill type not rendered; layer omitted explicitly'})
            below_non_solid = True
    layers = list(reversed(upper))
    if bottom[3] > 1e-3:
        layers.append(rgba_css(bottom))
    elif not layers:
        layers.append('transparent')
    return ', '.join(layers), disclosures


def composite_text_color(fills):
    """Classify visible text fills.

    Returns dict: {mode: 'none'|'solid'|'gradient'|'image'|'unknown',
                   css: str (color or background-clip rule), disclosures: [..]}
    Never returns '#000' as a guess.
    """
    fills = _normalize_fills(fills)
    vis = [f for f in fills if f.get('visible', True)]
    kinds = {f.get('type') for f in vis}
    disclosures = []
    if not vis:
        return {'mode': 'none', 'css': 'color:transparent;',
                'disclosures': [{'kind': 'text-no-fill',
                                  'reason': 'no visible text fill; rendered '
                                            'transparent (not faked #000)'}]}
    if 'UNKNOWN' in kinds:
        u = next(f for f in vis if f.get('type') == 'UNKNOWN')
        disclosures.append({'kind': 'text-fills-unknown',
                            'reason': u.get('reason', 'fills value unparseable')})
        return {'mode': 'unknown', 'css': 'color:transparent;',
                'disclosures': disclosures}
    if 'IMAGE' in kinds:
        disclosures.append({'kind': 'text-image-unsupported',
                            'reason': 'image-filled text cannot be rebuilt as '
                                      'real DOM text; disclosed, not faked'})
        return {'mode': 'image', 'css': 'color:transparent;',
                'disclosures': disclosures}
    if 'GRADIENT_LINEAR' in kinds:
        grad = next(f for f in vis if f.get('type') == 'GRADIENT_LINEAR')
        bg = _gradient_css(grad)
        # Mixed gradient + solid: the solids are NOT silently swallowed; disclose.
        solids = [f for f in vis if f.get('type') == 'SOLID']
        if solids:
            disclosures.append({
                'kind': 'text-gradient-with-solid',
                'reason': f'{len(solids)} solid fill(s) coexist with a gradient; '
                          'gradient wins (background-clip:text); solids not composited'})
        return {'mode': 'gradient',
                'css': (f'background:{bg};-webkit-background-clip:text;'
                        f'background-clip:text;color:transparent;'),
                'disclosures': disclosures}
    # all solids -> composite
    col = (0.0, 0.0, 0.0, 0.0)
    for f in vis:
        col = over(solid_tuple(f), col)
    if col[3] <= 1e-3:
        return {'mode': 'none', 'css': 'color:transparent;',
                'disclosures': [{'kind': 'text-no-fill',
                                  'reason': 'solid fills all transparent'}]}
    return {'mode': 'solid', 'css': f'color:{rgba_css(col)};',
            'disclosures': disclosures}


def weight(fn):
    return WEIGHT.get((fn or {}).get('style'), 400)


def esc(s):
    return html.escape(str(s or ''), quote=False)


# ---------------------------------------------------------------------------
# layers
# ---------------------------------------------------------------------------
def _zchain(node):
    z = node.get('zIndexChain')
    return tuple(z) if isinstance(z, list) else ()


def span(t):
    fn = t.get('fontName') or {}
    fs = t.get('fontSize', 16)
    fs = fs if isinstance(fs, (int, float)) else 16
    tc = composite_text_color(t.get('fills'))
    return (f"<span style=\"font-family:'{esc(fn.get('family','sans-serif'))}';"
            f"font-weight:{weight(fn)};font-size:{fs:.3f}px;{tc['css']}\">"
            f"{esc(t.get('characters'))}</span>"), tc['disclosures']


def text_layer(t, stage_size=None):
    """Return {'z','html','disclosures'}.

    Uses absoluteTransform (full accumulated affine) when present so rotated /
    nested text is reproduced generically. Falls back to the render AABB for
    identity transforms or legacy manifests.
    """
    disclosures = []
    rt = t['relativeToArtboard']
    segs = t.get('segments') or []
    if segs:
        parts = []
        for s in segs:
            h, d = span(s)
            parts.append(h)
            disclosures.extend(d)
        content = ''.join(parts)
    else:
        content, d = span(t)
        disclosures.extend(d)

    fn = t.get('fontName') or {}
    fs = t.get('fontSize', 16)
    fs = fs if isinstance(fs, (int, float)) else 16
    lh = t.get('lineHeight') or {}
    unit = lh.get('unit', 'AUTO')
    line = (f"{lh.get('value',100)/100:.5f}" if unit == 'PERCENT'
            else (f"{lh.get('value',fs):.3f}px" if unit == 'PIXELS' else 'normal'))
    ls = t.get('letterSpacing') or {}
    letter = ''
    if ls.get('unit') == 'PERCENT' and ls.get('value') is not None:
        letter = f"letter-spacing:{ls['value']/100:.6f}em;"
    elif ls.get('unit') == 'PIXELS' and ls.get('value') is not None:
        letter = f"letter-spacing:{ls['value']:.3f}px;"
    ar = t.get('textAutoResize', 'NONE')
    chars = t.get('characters', '')
    ws = 'pre' if '\n' in chars else ('nowrap' if ar == 'WIDTH_AND_HEIGHT' else 'pre-wrap')
    align = (t.get('textAlignHorizontal') or 'LEFT').lower().replace('justified', 'justify')
    va = {'TOP': 'top', 'CENTER': 'middle', 'BOTTOM': 'bottom'}.get(
        t.get('textAlignVertical'), 'top')
    opacity = t.get('effectiveOpacity', t.get('opacity', 1))

    m = t.get('absoluteTransform')
    use_matrix = (isinstance(m, list) and len(m) == 6 and
                  not (abs(m[0]-1) < 1e-6 and abs(m[1]) < 1e-6 and
                       abs(m[2]) < 1e-6 and abs(m[3]-1) < 1e-6))
    if use_matrix:
        # Outer div spans the stage; transform-origin 0 0 makes CSS matrix map
        # local (0,0) -> (tx,ty), exactly Figma's local-to-artboard matrix.
        W, H = stage_size if stage_size else (rt['x']+rt['width'], rt['y']+rt['height'])
        lo = t.get('local') or {}
        cw = lo.get('width', rt['width'])
        ch = lo.get('height', rt['height'])
        mat = 'matrix(' + ','.join(f'{_f3(v):.4f}' for v in m) + ')'
        outer = (f'left:0;top:0;width:{W:.3f}px;height:{H:.3f}px;'
                 f'transform:{mat};transform-origin:0 0;opacity:{opacity};')
        inner = (f'position:absolute;left:0;top:0;width:{cw:.3f}px;height:{ch:.3f}px;'
                 f'display:table;table-layout:fixed;margin:0;')
        cell = (f'display:table-cell;vertical-align:{va};text-align:{align};'
                f'white-space:{ws};line-height:{line};{letter}')
        html_out = (f'<div class="tlayer" data-n="{esc(t["nodeId"])}" style="{outer}">'
                    f'<div style="{inner}"><div class="tcell" style="{cell}">{content}</div>'
                    f'</div></div>')
    else:
        x, y, w, h = rt['x'], rt['y'], rt['width'], rt['height']
        if abs(_f3(t.get('rotation'))) > 0.001 and not m:
            disclosures.append({
                'kind': 'rotation-unverified', 'node': t['nodeId'],
                'reason': 'rotated text without absoluteTransform in manifest; '
                          'placed by render AABB (may drift)'})
        style = (f'left:{x:.4f}px;top:{y:.4f}px;width:{w:.4f}px;height:{h:.4f}px;'
                 f'opacity:{opacity};')
        inner = (f'display:table-cell;vertical-align:{va};text-align:{align};'
                 f'white-space:{ws};line-height:{line};{letter}')
        html_out = (f'<div class="tlayer" data-n="{esc(t["nodeId"])}" style="{style}">'
                    f'<div class="tcell" style="{inner}">{content}</div></div>')
    return {'z': _zchain(t), 'html': html_out, 'disclosures': disclosures}


def _blend_css(rec):
    """Map a Figma layer blendMode to CSS mix-blend-mode.

    exportAsync rasterizes a layer in isolation, so a composite blend mode
    (e.g. COLOR_BURN over a gradient) is NOT baked into the PNG. The builder
    must re-apply it at composite time; otherwise a COLOR_BURN background layer
    is pasted with normal alpha and washes the backdrop out. Figma modes with no
    exact CSS equivalent fall back to the nearest CSS mode and are disclosed.
    """
    mode = (rec.get('blendMode') or 'NORMAL').upper()
    if mode in ('NORMAL', 'PASS_THROUGH', ''):
        return '', None
    exact = {
        'MULTIPLY': 'multiply', 'SCREEN': 'screen', 'OVERLAY': 'overlay',
        'DARKEN': 'darken', 'LIGHTEN': 'lighten',
        'COLOR_DODGE': 'color-dodge', 'COLOR_BURN': 'color-burn',
        'HARD_LIGHT': 'hard-light', 'SOFT_LIGHT': 'soft-light',
        'DIFFERENCE': 'difference', 'EXCLUSION': 'exclusion',
        'HUE': 'hue', 'SATURATION': 'saturation',
        'COLOR': 'color', 'LUMINOSITY': 'luminosity',
    }
    if mode in exact:
        return f'mix-blend-mode:{exact[mode]};', None
    approx = {'LINEAR_BURN': 'color-burn', 'LINEAR_DODGE': 'color-dodge'}
    if mode in approx:
        return f'mix-blend-mode:{approx[mode]};', {
            'kind': 'blend-mode-approximated',
            'node': rec.get('nodeId'),
            'reason': f'Figma {mode} has no CSS equivalent; emitted '
                      f"{approx[mode]} (approximation, disclosed)"}
    return '', {'kind': 'blend-mode-unmapped', 'node': rec.get('nodeId'),
                'reason': f'Figma {mode} not mapped to CSS; layer rendered normal'}


def asset_layer(a, export_dir, out_dir):
    pp = a['pngPlacement']
    size = a.get('exportSize') or {'width': pp['width'], 'height': pp['height']}
    src = os.path.relpath(os.path.join(export_dir, a['asset']), out_dir).replace(os.sep, '/')
    op = a.get('effectiveOpacity', a.get('opacity', 1))
    blend_css, blend_disc = _blend_css(a)
    img = (f'<img class="layer" data-n="{esc(a["nodeId"])}" src="{html.escape(src,quote=True)}" alt="" '
           f'style="left:{pp["x"]:.4f}px;top:{pp["y"]:.4f}px;width:{size["width"]:.4f}px;'
           f'height:{size["height"]:.4f}px;opacity:{op};{blend_css}">')
    eff = a.get('effectiveClipRect')
    art = a.get('_artboardSize', {})
    if eff and (eff['width'] < art.get('width', eff['width'])-1 or
                eff['height'] < art.get('height', eff['height'])-1):
        inner = img.replace(
            f'left:{pp["x"]:.4f}px;top:{pp["y"]:.4f}px',
            f'left:{pp["x"]-eff["x"]:.4f}px;top:{pp["y"]-eff["y"]:.4f}px')
        img = (f'<div class="clip" data-clip-for="{esc(a["nodeId"])}" '
               f'style="left:{eff["x"]:.4f}px;top:{eff["y"]:.4f}px;'
               f'width:{eff["width"]:.4f}px;height:{eff["height"]:.4f}px;">{inner}</div>')
    return {'z': _zchain(a), 'html': img,
            'disclosures': ([blend_disc] if blend_disc else [])}


def _is_non_identity_matrix(m):
    return (isinstance(m, list) and len(m) == 6 and
            not (abs(m[0]-1) < 1e-6 and abs(m[1]) < 1e-6 and
                 abs(m[2]) < 1e-6 and abs(m[3]-1) < 1e-6))


def container_layer(c, stage_size=None, artboard_size=None, resolve_image=None):
    disclosures = []
    bg_css, d = build_background_css(c.get('fills'), resolve_image=resolve_image)
    disclosures.extend(d)
    border = ''
    if c.get('strokes'):
        col = composite_text_color(c.get('strokes'))
        border = (f'border:{c.get("strokeWeight",1)}px solid {col["css"].replace("color:","").rstrip(";")};'
                  f'box-sizing:border-box;')
    radius = c.get('cornerRadius')
    radius = f'border-radius:{radius}px;' if isinstance(radius, (int, float)) else ''
    op = c.get('effectiveOpacity', c.get('opacity', 1))
    m = c.get('absoluteTransform')

    # Zero-visual marker: a container with no fills and no strokes still appears
    # in containerVisuals (because it wraps text). Emit a transparent positioned
    # marker div carrying data-n so the content ledger stays consistent and
    # audit_content does not hard-fail on a "missing" node. The marker paints
    # nothing (no background, no border) -- it is purely a ledger anchor.
    if bg_css == 'transparent' and not border:
        if _is_non_identity_matrix(m):
            W, H = stage_size if stage_size else (0, 0)
            lo = c.get('local') or {}
            r = c['renderAABB']
            cw = lo.get('width', r['width'])
            ch = lo.get('height', r['height'])
            mat = 'matrix(' + ','.join(f'{_f3(v):.4f}' for v in m) + ')'
            html_out = (f'<div class="container-visual marker" data-n="{esc(c["nodeId"])}" '
                        f'style="left:0;top:0;width:{W:.3f}px;height:{H:.3f}px;'
                        f'transform:{mat};transform-origin:0 0;'
                        f'opacity:{op};visibility:hidden;">'
                        f'<div style="position:absolute;left:0;top:0;width:{cw:.3f}px;height:{ch:.3f}px;"></div>'
                        f'</div>')
        else:
            r = c['renderAABB']
            html_out = (f'<div class="container-visual marker" data-n="{esc(c["nodeId"])}" '
                        f'style="position:absolute;left:{r["x"]:.4f}px;top:{r["y"]:.4f}px;'
                        f'width:{r["width"]:.4f}px;height:{r["height"]:.4f}px;'
                        f'opacity:{op};visibility:hidden;"></div>')
        return {'z': _zchain(c), 'html': html_out,
                'disclosures': disclosures + [{
                    'kind': 'empty-container-marker',
                    'node': c['nodeId'],
                    'reason': 'no fills/strokes; emitted zero-visibility marker to keep content ledger consistent'}]}

    blend_css, blend_disc = _blend_css(c)
    if blend_disc:
        disclosures.append(blend_disc)
    visual = (f'background:{bg_css};{border}{radius}opacity:{op};{blend_css}')
    m = c.get('absoluteTransform')
    if _is_non_identity_matrix(m):
        # Generic affine placement, identical to rotated text: outer stage-sized
        # wrapper carries the accumulated matrix; the visual box sits at local
        # (0,0). A rotated container is NEVER flattened to an axis-aligned rect.
        W, H = stage_size if stage_size else (0, 0)
        lo = c.get('local') or {}
        r = c['renderAABB']
        cw = lo.get('width', r['width'])
        ch = lo.get('height', r['height'])
        mat = 'matrix(' + ','.join(f'{_f3(v):.4f}' for v in m) + ')'
        html_out = (f'<div class="container-visual" data-n="{esc(c["nodeId"])}" '
                    f'style="left:0;top:0;width:{W:.3f}px;height:{H:.3f}px;'
                    f'transform:{mat};transform-origin:0 0;">'
                    f'<div style="position:absolute;left:0;top:0;width:{cw:.3f}px;height:{ch:.3f}px;{visual}"></div>'
                    f'</div>')
    else:
        r = c['renderAABB']
        if abs(_f3(c.get('rotation'))) > 0.001 and not m:
            disclosures.append({
                'kind': 'rotation-unverified', 'node': c['nodeId'],
                'reason': 'rotated container without absoluteTransform in manifest; '
                          'rendered by axis-aligned render AABB (may drift)'})
        inner = (f'<div class="container-visual" data-n="{esc(c["nodeId"])}" '
                 f'style="position:absolute;left:{r["x"]:.4f}px;top:{r["y"]:.4f}px;'
                 f'width:{r["width"]:.4f}px;height:{r["height"]:.4f}px;{visual}"></div>')
        # Wrap in a clip div when an ancestor clip rect is smaller than the artboard.
        eff = c.get('effectiveClipRect')
        art = artboard_size or {}
        if eff and (eff['width'] < art.get('width', eff['width'])-1 or
                    eff['height'] < art.get('height', eff['height'])-1):
            inner = inner.replace(
                f'left:{r["x"]:.4f}px;top:{r["y"]:.4f}px',
                f'left:{r["x"]-eff["x"]:.4f}px;top:{r["y"]-eff["y"]:.4f}px')
            html_out = (f'<div class="clip" data-clip-for="{esc(c["nodeId"])}" '
                        f'style="position:absolute;left:{eff["x"]:.4f}px;top:{eff["y"]:.4f}px;'
                        f'width:{eff["width"]:.4f}px;height:{eff["height"]:.4f}px;overflow:hidden">'
                        f'{inner}</div>')
        else:
            html_out = inner
    return {'z': _zchain(c), 'html': html_out, 'disclosures': disclosures}


def resolve_image_factory(m, export_dir, out_dir):
    """Generic IMAGE-paint -> url resolver, shared by root and container fills.

    Resolution order (generic, no hardcoded names / keys):
      1. per-paint ``asset`` override (operator-supplied file next to the manifest);
      2. the paint's own key -- ``imageRef`` then ``imageHash`` -- looked up in
         the manifest's ``imageAssetMap`` (bytes exported by the exporter);
      3. the legacy single global ``rootBackgroundAsset``.

    Returns ``None`` when nothing resolves, so the caller discloses the paint
    (``image-fill-unresolved``) instead of painting white.
    """
    asset_map = m.get('imageAssetMap') or {}
    declared = m.get('rootBackgroundAsset')

    def _resolve(rel):
        if not rel:
            return None
        p = os.path.join(export_dir, rel)
        if os.path.exists(p):
            return os.path.relpath(p, out_dir).replace(os.sep, '/')
        return None

    def _resolver(f):
        rel = f.get('asset')
        if not rel:
            key = f.get('imageRef') or f.get('imageHash')
            if key:
                rel = asset_map.get(key)
        if not rel:
            rel = declared
        return _resolve(rel)

    return _resolver


def _effectively_visible(node):
    """A node renders iff it is self-visible AND (when the manifest carries the
    annotation) effectively visible. Legacy manifests without effectiveVisible
    fall back to self `visible`."""
    if node.get('effectiveVisible') is False:
        return False
    return bool(node.get('visible', True))


# ---------------------------------------------------------------------------
# Connector inline-SVG replacement (generic, no node ids / colors baked in)
# ---------------------------------------------------------------------------
_CONNECTOR_MANIFEST_NAMES = ('connectors.manifest.json',)


def load_connector_map(export_dir):
    """Locate connectors.manifest.json next to the export dir (or in a
    ``connectors/`` subdir). Return {nodeId: {svgPath, designBox}}.

    A missing manifest is not an error; connectors are then emitted as PNG and
    the auditor reports degraded-bbox.
    """
    for sub in ('connectors', ''):
        for name in _CONNECTOR_MANIFEST_NAMES:
            p = pathlib.Path(export_dir) / sub / name
            if p.exists():
                try:
                    data = json.loads(p.read_text(encoding='utf-8'))
                except Exception:
                    return {}
                out = {}
                for row in data.get('exported', []) or []:
                    nid = row.get('nodeId')
                    saved = (row.get('saved') or {}).get('saved') or []
                    if not nid or not saved:
                        continue
                    svg_path = saved[0].get('path')
                    if svg_path and os.path.exists(svg_path):
                        out[nid] = {
                            'svgPath': svg_path,
                            'designBox': row.get('designBox') or {},
                            'refused': False,
                        }
                for row in data.get('refused', []) or []:
                    if row.get('nodeId'):
                        out[row['nodeId']] = {'refused': True,
                                              'reason': row.get('reason')}
                return out
    return {}


_SVG_OPEN_RE = re.compile(r'^<svg\b[^>]*>', re.IGNORECASE)
_SVG_CLOSE_RE = re.compile(r'</svg>\s*$', re.IGNORECASE)


def _svg_inner_content(svg_text):
    """Strip the outer <svg>...</svg> wrapper. Returns inner markup or None
    if the text is not a standalone SVG."""
    m = _SVG_OPEN_RE.search(svg_text.lstrip())
    if not m:
        return None
    start = m.end()
    end_m = None
    for em in _SVG_CLOSE_RE.finditer(svg_text):
        end_m = em
    if not end_m:
        return None
    return svg_text[start:end_m.start()].strip()


def _svg_has_text(svg_text):
    """Refuse SVG that bakes text (connector vector must be text-free)."""
    return bool(re.search(r'<text\b|<tspan\b', svg_text, re.IGNORECASE))


def connector_layer(a, export_dir, out_dir, conn_map):
    """If the asset is a connector with a standalone SVG, emit inline <svg>
    in the same CSS box the PNG would have used. Otherwise return None so the
    caller falls back to asset_layer (PNG).

    Guarantees:
      * data-n preserved on the inline <svg> (auditor selector still matches).
      * Same CSS box (left/top/width/height) as the PNG layer; SVG scales to
        fill via preserveAspectRatio="none".
      * No double exposure: the <img> is NOT emitted when the SVG is used.
      * Text-bearing SVG is refused (fall back to PNG + disclosure).
      * Missing SVG falls back to PNG + disclosure.
    """
    nid = a.get('nodeId')
    info = conn_map.get(nid) or {}
    if info.get('refused') or not info.get('svgPath'):
        # No usable SVG -> caller emits PNG. Disclose at build_html level.
        return None
    try:
        svg_text = pathlib.Path(info['svgPath']).read_text(encoding='utf-8')
    except Exception:
        return None
    if _svg_has_text(svg_text):
        return None  # safety net; export_connector_svg should already refuse
    inner = _svg_inner_content(svg_text)
    if inner is None:
        return None
    pp = a['pngPlacement']
    size = a.get('exportSize') or {'width': pp['width'], 'height': pp['height']}
    op = a.get('effectiveOpacity', a.get('opacity', 1))
    # Same CSS box as the PNG would have; preserveAspectRatio="none" lets the
    # vector stretch to the design box (matches Figma's render frame).
    svg = (f'<svg class="layer" data-n="{esc(nid)}" fill="none" '
           f'preserveAspectRatio="none" '
           f'style="position:absolute;left:{pp["x"]:.4f}px;top:{pp["y"]:.4f}px;'
           f'width:{size["width"]:.4f}px;height:{size["height"]:.4f}px;'
           f'opacity:{op};">{inner}</svg>')
    eff = a.get('effectiveClipRect')
    art = a.get('_artboardSize', {})
    if eff and (eff['width'] < art.get('width', eff['width'])-1 or
                eff['height'] < art.get('height', eff['height'])-1):
        inner_html = svg.replace(
            f'left:{pp["x"]:.4f}px;top:{pp["y"]:.4f}px',
            f'left:{pp["x"]-eff["x"]:.4f}px;top:{pp["y"]-eff["y"]:.4f}px')
        svg = (f'<div class="clip" data-clip-for="{esc(nid)}" '
               f'style="left:{eff["x"]:.4f}px;top:{eff["y"]:.4f}px;'
               f'width:{eff["width"]:.4f}px;height:{eff["height"]:.4f}px;'
               f'overflow:hidden">{inner_html}</div>')
    return {'z': _zchain(a), 'html': svg, 'disclosures': [],
            'replacedBySvg': True}


def build_html(m, tm, out_dir, export_dir, font_css_text='', title=None,
               conn_map=None):
    """Pure build: returns (html_string, report_dict). No filesystem writes."""
    W, H = m['artboardSize']['width'], m['artboardSize']['height']
    layers = []
    all_disclosures = []
    resolver = resolve_image_factory(m, export_dir, out_dir)
    conn_map = conn_map or {}
    skipped = {'asset': 0, 'container': 0, 'text': 0}
    connector_replaced = 0
    connector_fallback_png = 0
    for a in m['assets']:
        if not _effectively_visible(a):
            skipped['asset'] += 1
            continue
        if not a.get('asset'):
            continue
        a['_artboardSize'] = m['artboardSize']
        nid = a.get('nodeId')
        conn_info = conn_map.get(nid)
        layer = None
        if conn_info:
            if conn_info.get('refused'):
                connector_fallback_png += 1
                all_disclosures.append({
                    'kind': 'connector-svg-refused', 'node': nid,
                    'reason': conn_info.get('reason',
                        'connector SVG export refused; falling back to PNG '
                        '(auditor will mark degraded-bbox)')})
            else:
                layer = connector_layer(a, export_dir, out_dir, conn_map)
                if layer is not None:
                    connector_replaced += 1
                else:
                    connector_fallback_png += 1
                    all_disclosures.append({
                        'kind': 'connector-svg-unavailable', 'node': nid,
                        'reason': 'standalone connector SVG not usable; '
                                  'falling back to PNG (auditor will mark '
                                  'degraded-bbox, not tip-verified)'})
        if layer is None:
            layer = asset_layer(a, export_dir, out_dir)
        layers.append(layer)
    for c in m.get('containerVisuals', []):
        if not _effectively_visible(c):
            skipped['container'] += 1
            continue
        layer = container_layer(c, stage_size=(W, H), artboard_size=m['artboardSize'],
                                resolve_image=resolver)
        if layer:
            layers.append(layer)
    for t in tm['texts']:
        if not _effectively_visible(t):
            skipped['text'] += 1
            continue
        layers.append(text_layer(t, stage_size=(W, H)))
    for n in layers:
        all_disclosures.extend(n.get('disclosures', []))
    layers.sort(key=lambda n: n['z'])

    root_bg, root_disc = build_background_css(m.get('rootFills'), resolve_image=resolver)
    all_disclosures.extend(root_disc)

    doc = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width={W},initial-scale=1"><title>{esc(title or m.get('artboardName') or 'Figma artboard')}</title><style>
html,body{{margin:0;padding:0;width:{W}px;height:{H}px;overflow:hidden}}#stage{{position:relative;width:{W}px;height:{H}px;overflow:hidden;background:{root_bg};transform-origin:0 0;isolation:isolate}}
.layer,.tlayer,.clip,.container-visual{{position:absolute;display:block;max-width:none}}.clip{{overflow:hidden}}.tcell{{padding:0;border:0}}
{font_css_text}</style></head><body><div id="stage">{''.join(n['html'] for n in layers)}</div></body></html>'''
    report = {
        'artboardSize': [W, H],
        'asset_layers': len(m['assets']),
        'text_layers': len(tm['texts']),
        'rootBackgroundCss': root_bg,
        'disclosures': all_disclosures,
        'disclosureCount': len(all_disclosures),
        'skippedByVisibility': skipped,
        'connectorSvgReplaced': connector_replaced,
        'connectorPngFallback': connector_fallback_png,
        'note': 'Generic baseline; run geometry/text/pixel/visual verification before delivery.',
    }
    return doc, report


# ---------------------------------------------------------------------------
# Design Compiler plan path (layout-plan.json)
# ---------------------------------------------------------------------------
# The builder MUST consume compile_design output (layout-plan + asset manifest
# + connector plan). The Agent never re-reads the raw SDK to lay things out.
# Every emitted mapping stamps data-n (nodeId) + data-rule (ruleVersion) +
# data-disposition, so every pixel is traceable to a plan row. Visibility is
# taken ONLY from the plan's effectiveVisibility; there is no parallel guess.
RULE_VERSION = 'design-compiler/1.0.0'

# design_text is the parallel text-CSS compiler module. When present we MUST
# route text through it; when absent (e.g. legacy unit tests) we fall back to a
# local, identical-decision-table implementation so the builder still builds.
try:  # pragma: no cover - import guard
    from design_text import build_text_css as _dt_build_text_css  # type: ignore
except Exception:  # pragma: no cover
    _dt_build_text_css = None


def _is_identity_matrix(m):
    """True when the affine matrix is the identity (emit an axis-aligned box)."""
    return bool(isinstance(m, list) and len(m) == 6 and
                abs(m[0]-1) < 1e-6 and abs(m[1]) < 1e-6 and
                abs(m[2]) < 1e-6 and abs(m[3]-1) < 1e-6)


def _text_clip_css(tp, rb, m, stage_size):
    """Compile the plan's inherited artboard clip without clamping the text's
    own fixed box. Negative insets are intentional: Figma text may overflow its
    own box while remaining visible up to an ancestor frame boundary."""
    cr = tp.get('clipRect')
    if not cr:
        return ''
    W, H = stage_size
    if (abs(cr['x']) < 1e-6 and abs(cr['y']) < 1e-6 and
            abs(cr['w'] - W) < 1e-6 and abs(cr['h'] - H) < 1e-6):
        return ''
    if _is_identity_matrix(m):
        top = cr['y'] - rb['y']
        right = rb['x'] + rb['w'] - (cr['x'] + cr['w'])
        bottom = rb['y'] + rb['h'] - (cr['y'] + cr['h'])
        left = cr['x'] - rb['x']
    else:
        a, b, c, d, e, f = m
        det = a*d - b*c
        if abs(det) < 1e-12:
            return ''
        inv = [d/det, -b/det, -c/det, a/det,
               (c*f-d*e)/det, (b*e-a*f)/det]
        pts = [(cr['x'], cr['y']), (cr['x']+cr['w'], cr['y']),
               (cr['x'], cr['y']+cr['h']),
               (cr['x']+cr['w'], cr['y']+cr['h'])]
        q = [(inv[0]*x+inv[2]*y+inv[4], inv[1]*x+inv[3]*y+inv[5])
             for x, y in pts]
        x0, x1 = min(x for x, y in q), max(x for x, y in q)
        y0, y1 = min(y for x, y in q), max(y for x, y in q)
        W, H = stage_size
        top, right, bottom, left = y0, W-x1, H-y1, x0
    return (f'clip-path:inset({top:.4f}px {right:.4f}px '
            f'{bottom:.4f}px {left:.4f}px);')


def _plan_box(n):
    """Pick the correct box family for a planned node.

    A node whose effectiveMatrix is non-identity renders as a rotated box; its
    axis-aligned <getBoundingClientRect> / renderAABB is the ROTATED family and
    must NEVER be compared against the unrotated designBox (that fabricates
    width/height error). Axis-aligned nodes use renderAABB directly.
    """
    rb = n.get('renderAABB') or n.get('designBox') or {}
    db = n.get('designBox') or {}
    local = n.get('local') or rb or db
    return {
        'renderAABB': {'x': rb.get('x', 0), 'y': rb.get('y', 0),
                       'w': rb.get('w', rb.get('width', 0)),
                       'h': rb.get('h', rb.get('height', 0))},
        'local': {'x': local.get('x', 0), 'y': local.get('y', 0),
                  'w': local.get('w', local.get('width', 0)),
                  'h': local.get('h', local.get('height', 0))},
        'designBox': {'x': db.get('x', 0), 'y': db.get('y', 0),
                      'w': db.get('w', db.get('width', 0)),
                      'h': db.get('h', db.get('height', 0))},
    }


def _wire_text_ir(tp):
    """Normalize a (possibly styling-only) textPlan into the wire IR the parallel
    design_text compiler consumes. Real plans ship flat lineHeightPx /
    letterSpacingPx / mode / whiteSpace and pre-derived outerWidth; older manifests
    ship nested {unit,value} + textAutoResize. We only RESHAPE fields so the
    text module's own decision table (which we must not change) reproduces the
    compiler's choice -- we never override whiteSpace here.
      * mode content-size -> WIDTH_AND_HEIGHT (no soft wrap)
      * mode content-width -> HEIGHT_AND_WIDTH (no soft wrap)
      * mode fixed-box    -> NONE (fixed box, pre-wrap)
    """
    t = dict(tp or {})
    if t.get('lineHeight') is None and isinstance(t.get('lineHeightPx'), (int, float)):
        t['lineHeight'] = {'unit': 'PIXELS', 'value': t['lineHeightPx'],
                           'resolvedPx': t['lineHeightPx']}
    elif t.get('lineHeight') is None:
        # lineHeightPx is None => unknown AUTO; keep unit AUTO, resolvedPx null.
        t['lineHeight'] = {'unit': 'AUTO', 'resolvedPx': None}
    # Per-run style: the compiler carries runs under `runs`; design_text reads
    # `segments`. Map them verbatim so mixed fontSize/fills reach the spans.
    if not t.get('segments') and t.get('runs'):
        t['segments'] = t['runs']
    if t.get('letterSpacing') is None:
        ls = t.get('letterSpacingPx')
        t['letterSpacing'] = {'unit': 'PIXELS',
                             'value': ls if isinstance(ls, (int, float)) else 0}
    # Map compiler mode -> Figma textAutoResize so decide_text_layout reproduces
    # nowrap for auto-width/height and pre-wrap for fixed-box.
    if not t.get('textAutoResize'):
        mode = t.get('mode')
        t['textAutoResize'] = {'content-size': 'WIDTH_AND_HEIGHT',
                               'content-width': 'HEIGHT_AND_WIDTH'}.get(mode, 'NONE')
    # Hand the layout table the local box width when we know it (merged node).
    # Normalize w/h keys -> width/height (design_text reads local.width).
    rb = t.get('renderAABB') or t.get('designBox') or {}
    local = dict(t.get('local') or {})
    if 'width' not in local:
        local['width'] = local.get('width', local.get('w', rb.get('w', rb.get('width'))))
    if 'height' not in local:
        local['height'] = local.get('height', local.get('h', rb.get('h', rb.get('height'))))
    if local.get('width') is not None:
        t['local'] = local
    return t


def _runs_html_from_runs(runs, justify_hard_lines=False):
    """Render design_text normalized runs to styled <span>s. Per-run font-size,
    font-family, style, weight and color come from each run; missing fills are
    disclosed transparent, never #000. For JUSTIFIED text, hard-break paragraphs
    become independent blocks so every non-final paragraph can use
    text-align-last:justify without stretching the design's actual last line."""
    disc = []
    groups = [[]]
    for idx, r in enumerate(runs or []):
        chars = r.get('characters', '') or ''
        size = r.get('font_size_px')
        if not isinstance(size, (int, float)) or size <= 0:
            raise ValueError(
                f'text run #{idx} has no numeric font_size_px; refusing to '
                f'impersonate with a default. characters={chars[:20]!r}')
        col = composite_text_color(r.get('fills'))
        disc.extend(col['disclosures'])
        family = r.get('font_family') or 'sans-serif'
        font_style = r.get('font_style') or 'normal'
        font_weight = r.get('font_weight') or 400
        style = (f"font-family:'{esc(family)}';font-style:{font_style};"
                 f'font-weight:{int(font_weight)};font-size:{size:.3f}px;{col["css"]}')
        for part in re.split(r'([\n\u2028\u2029])', chars):
            if not part:
                continue
            if part in ('\n', '\u2028', '\u2029'):
                if justify_hard_lines:
                    groups.append([])
                else:
                    groups[-1].append('<br>' if part != '\n' else '\n')
                continue
            # data-run-index lets audit_text compare each span's COMPUTED style
            # to its expected run style (mixed nodes must not inherit parent weight).
            groups[-1].append(
                f'<span data-run-index="{idx}" style="{style}">{esc(part)}</span>')
    if justify_hard_lines:
        rendered = []
        for i, group in enumerate(groups):
            body = ''.join(group) or '<br>'
            last = 'justify' if i < len(groups) - 1 else 'auto'
            rendered.append(
                f'<span style="display:block;text-align-last:{last}">{body}</span>')
        return ''.join(rendered), disc
    return ''.join(''.join(g) for g in groups), disc


def _runs_html_from_calibrated_lines(runs, line_plan, justified=False):
    """Execute a measured Figma line plan without letting CSS re-break it.

    Each native PDF line becomes a real DOM block containing styled run slices.
    Text remains selectable/auditable; no glyph images or per-node coordinates
    are introduced. The compiler has already verified exact character parity.
    """
    lines = (line_plan or {}).get('lines') or []
    pos = 0
    blocks = []
    disclosures = []
    for line_index, line in enumerate(lines):
        end = pos + len(line)
        slices = []
        cursor = 0
        for r in runs or []:
            chars = r.get('characters', '') or ''
            run_end = cursor + len(chars)
            a, b = max(pos, cursor), min(end, run_end)
            if b > a:
                piece = dict(r)
                piece['characters'] = chars[a - cursor:b - cursor]
                slices.append(piece)
            cursor = run_end
        content, disc = _runs_html_from_runs(slices)
        disclosures.extend(disc)
        style = 'display:block;white-space:pre;'
        if justified and line_index < len(lines) - 1:
            style += 'text-align-last:justify;'
        blocks.append(f'<span class="cal-line" style="{style}">{content}</span>')
        pos = end
    if pos != len(''.join(r.get('characters', '') or '' for r in runs or [])):
        raise ValueError('calibrated line plan does not cover all text runs')
    return ''.join(blocks), disclosures


def _local_text_css(tp):
    """Fallback text-CSS decision table (used only when design_text is absent).
    Tolerates both nested {unit,value} and flat lineHeightPx/letterSpacingPx."""
    disclosures = []
    fn = tp.get('fontName') or {}
    fs = tp.get('fontSize')
    if not isinstance(fs, (int, float)) or fs <= 0:
        raise ValueError(
            f'_local_text_css: fontSize={fs!r} is not numeric; refusing to '
            f'impersonate with a 16px default. node={tp.get("nodeId")!r}')
    lh = tp.get('lineHeight') or {}
    if not lh and isinstance(tp.get('lineHeightPx'), (int, float)):
        lh = {'unit': 'PIXELS', 'value': tp['lineHeightPx']}
    unit = lh.get('unit', 'AUTO')
    if unit == 'PERCENT':
        line = f"{lh.get('value', 100)/100:.5f}"
        line_px = fs * lh.get('value', 100) / 100.0
    elif unit == 'PIXELS':
        line = f"{lh.get('value', fs):.3f}px"
        line_px = lh.get('value', fs)
    else:
        # AUTO: trust resolvedPx only if export measured it; else normal.
        line = 'normal'
        line_px = lh.get('resolvedPx')
    ls = tp.get('letterSpacing') or {}
    if not ls and isinstance(tp.get('letterSpacingPx'), (int, float)):
        ls = {'unit': 'PIXELS', 'value': tp['letterSpacingPx']}
    letter = ''
    if ls.get('unit') == 'PERCENT' and ls.get('value') is not None:
        letter = f"letter-spacing:{ls['value']/100:.6f}em;"
    elif ls.get('unit') == 'PIXELS' and ls.get('value') is not None:
        letter = f"letter-spacing:{ls['value']:.3f}px;"
    ws = tp.get('whiteSpace')
    if not ws:
        ar = tp.get('textAutoResize', 'NONE')
        chars = tp.get('characters', '')
        ws = 'pre' if '\n' in chars else ('nowrap' if ar == 'WIDTH_AND_HEIGHT' else 'pre-wrap')
    align = (tp.get('textAlignHorizontal') or 'LEFT').lower().replace('justified', 'justify')
    va = {'TOP': 'top', 'CENTER': 'middle', 'BOTTOM': 'bottom'}.get(
        tp.get('textAlignVertical'), 'top')
    tc = composite_text_color(tp.get('fills'))
    disclosures.extend(tc['disclosures'])
    runs_html = esc(tp.get('characters', ''))
    cell = (f'display:table-cell;vertical-align:{va};text-align:{align};'
            f'white-space:{ws};{letter}')
    return {'cell': cell, 'runs_html': runs_html, 'lineHeightPx': line_px,
            'disclosures': disclosures}


def plan_text_layer(tp, stage_size, rule_version):
    """Emit one planned text node. Routes through design_text.build_text_css
    when the parallel module is available; visibility ONLY from the plan."""
    disclosures = []
    rb = _plan_box(tp)['renderAABB']
    local = _plan_box(tp)['local']
    m = tp.get('effectiveMatrix') or tp.get('absoluteTransform')
    op = tp.get('effectiveOpacity', tp.get('opacity', 1))
    nid = tp.get('nodeId', '?')

    cell, runs = '', esc(tp.get('characters', ''))
    if _dt_build_text_css is not None:
        # No silent rescue: a hard failure from design_text (mixed size without
        # per-run numeric fontSize, missing fills) propagates up and blocks the
        # build instead of being papered over with a local 16px fallback.
        css = _dt_build_text_css(_wire_text_ir(tp))
        if isinstance(css, dict):
            # Real design_text contract: css = full cell declarations (str),
            # runs = normalized run dicts.
            cell = css.get('css', '')
            if tp.get('linePlan'):
                rh, rd = _runs_html_from_calibrated_lines(
                    css.get('runs'), tp['linePlan'],
                    justified=(str(tp.get('textAlignHorizontal') or '').upper()
                               == 'JUSTIFIED'))
            else:
                rh, rd = _runs_html_from_runs(
                    css.get('runs'),
                    justify_hard_lines=(str(tp.get('textAlignHorizontal') or '').upper()
                                        == 'JUSTIFIED'))
            runs = rh or runs
            disclosures.extend(css.get('disclosures', []))
            disclosures.extend(rd)
        else:
            raise ValueError(
                f'design_text.build_text_css returned non-dict for node {nid!r}')
    else:
        css = _local_text_css(tp)
        cell = css['cell']; runs = css['runs_html']
        disclosures.extend(css['disclosures'])

    # Outer box width follows the compiler's layout decision: fixed-box -> planned
    # px width (wraps there); content-width/content-size -> auto (shrink to the
    # measured line, never force a 2nd line / overflow by clamping to renderAABB).
    outer_w = rb['w']
    if isinstance(css, dict) and css.get('outerWidth'):
        outer_w = css['outerWidth']  # already 'auto' or e.g. '200px'
    height_h = rb['h']
    if outer_w == 'auto':
        outer_w_css = 'width:auto;'
    elif isinstance(outer_w, str) and outer_w.endswith('px'):
        outer_w_css = f'width:{outer_w};'
    else:
        outer_w_css = f'width:{float(outer_w):.4f}px;'

    data_attr = (f'data-n="{esc(nid)}" data-rule="{esc(rule_version)}" '
                 f'data-disposition="{esc(tp.get("disposition", "rendered-text"))}"')
    if _is_identity_matrix(m):
        style = (f'left:{rb["x"]:.4f}px;top:{rb["y"]:.4f}px;'
                 f'{outer_w_css}height:{height_h:.4f}px;opacity:{op};'
                 f'{_text_clip_css(tp, rb, m, stage_size)}')
        html_out = (f'<div class="tlayer" {data_attr} style="{style}">'
                    f'<div class="tcell" style="{cell}">{runs}</div></div>')
    else:
        # Full accumulated affine: outer stage-sized wrapper carries the matrix;
        # inner local box sits at (0,0). Parent -180 + child -180 cancels to an
        # identity matrix here, so the child is upright generically.
        W, H = stage_size
        mat = 'matrix(' + ','.join(f'{_f3(v):.4f}' for v in m) + ')'
        outer = (f'left:0;top:0;width:{W:.3f}px;height:{H:.3f}px;'
                 f'transform:{mat};transform-origin:0 0;opacity:{op};'
                 f'{_text_clip_css(tp, rb, m, stage_size)}')
        inner_w_css = ('width:auto;' if outer_w == 'auto' else
                       (f'width:{outer_w};' if isinstance(outer_w, str) and outer_w.endswith('px')
                        else f'width:{float(outer_w):.3f}px;'))
        inner = (f'position:absolute;left:0;top:0;'
                 f'{inner_w_css}height:{local["h"]:.3f}px;'
                 f'display:table;table-layout:fixed;margin:0;')
        html_out = (f'<div class="tlayer" {data_attr} style="{outer}">'
                    f'<div style="{inner}"><div class="tcell" style="{cell}">'
                    f'{runs}</div></div></div>')
    return {'z': _zchain(tp), 'html': html_out, 'disclosures': disclosures,
            'nodeId': nid, 'kind': 'text'}


def plan_container_layer(n, stage_size, resolve_image, rule_version):
    """Emit a planned container/visual. Relations (incl. green-card relative
    offsets) are decided by the plan, not re-derived here."""
    disclosures = []
    rb = _plan_box(n)['renderAABB']
    local = _plan_box(n)['local']
    bg_css, d = build_background_css(n.get('fills'), resolve_image=resolve_image)
    disclosures.extend(d)
    border = ''
    if n.get('strokes'):
        col = composite_text_color(n.get('strokes'))
        border = (f'border:{n.get("strokeWeight", 1)}px solid '
                  f'{col["css"].replace("color:", "").rstrip(";")};'
                  f'box-sizing:border-box;')
    radius = n.get('cornerRadius')
    radius = f'border-radius:{radius}px;' if isinstance(radius, (int, float)) else ''
    op = n.get('effectiveOpacity', n.get('opacity', 1))
    m = n.get('effectiveMatrix') or n.get('absoluteTransform')
    nid = n.get('nodeId', '?')
    disposition = n.get('disposition', 'rendered-container')
    data_attr = (f'data-n="{esc(nid)}" data-rule="{esc(rule_version)}" '
                 f'data-disposition="{esc(disposition)}"')
    blend_css, blend_disc = _blend_css(n)
    if blend_disc:
        disclosures.append(blend_disc)
    visual = f'background:{bg_css};{border}{radius}opacity:{op};{blend_css}'

    if bg_css == 'transparent' and not border:
        # The container paints nothing (no SOLID/resolved IMAGE fill, no stroke).
        # Emit a transparent, POSITIONED box so relations (parentChildInset /
        # textLineInset) can still measure it -- but do NOT blanket-force
        # visibility:hidden. Only when the plan explicitly marks the node as a
        # zero-visual ledger anchor do we hide it; otherwise it stays measurable.
        hidden = 'visibility:hidden;' if n.get('zeroVisual') else ''
        if _is_identity_matrix(m):
            html_out = (f'<div class="container-visual" {data_attr} '
                        f'style="position:absolute;left:{rb["x"]:.4f}px;top:{rb["y"]:.4f}px;'
                        f'width:{rb["w"]:.4f}px;height:{rb["h"]:.4f}px;'
                        f'background:transparent;opacity:{op};{hidden}"></div>')
        else:
            W, H = stage_size
            mat = 'matrix(' + ','.join(f'{_f3(v):.4f}' for v in m) + ')'
            html_out = (f'<div class="container-visual" {data_attr} '
                        f'style="left:0;top:0;width:{W:.3f}px;height:{H:.3f}px;'
                        f'transform:{mat};transform-origin:0 0;opacity:{op};{hidden}">'
                        f'<div style="position:absolute;left:0;top:0;'
                        f'width:{local["w"]:.3f}px;height:{local["h"]:.3f}px;"></div></div>')
        return {'z': _zchain(n), 'html': html_out, 'disclosures': disclosures,
                'nodeId': nid, 'kind': 'container'}

    if _is_identity_matrix(m):
        inner = (f'<div class="container-visual" {data_attr} '
                 f'style="position:absolute;left:{rb["x"]:.4f}px;top:{rb["y"]:.4f}px;'
                 f'width:{rb["w"]:.4f}px;height:{rb["h"]:.4f}px;{visual}"></div>')
    else:
        W, H = stage_size
        mat = 'matrix(' + ','.join(f'{_f3(v):.4f}' for v in m) + ')'
        inner = (f'<div class="container-visual" {data_attr} '
                 f'style="position:absolute;left:0;top:0;width:{local["w"]:.3f}px;'
                 f'height:{local["h"]:.3f}px;{visual}"></div>')
        html_out = (f'<div class="container-visual" data-n="{esc(nid)}" '
                    f'data-rule="{esc(rule_version)}" '
                    f'style="left:0;top:0;width:{W:.3f}px;height:{H:.3f}px;'
                    f'transform:{mat};transform-origin:0 0;">{inner}</div>')
        return {'z': _zchain(n), 'html': html_out, 'disclosures': disclosures,
                'nodeId': nid, 'kind': 'container'}
    return {'z': _zchain(n), 'html': inner, 'disclosures': disclosures,
            'nodeId': nid, 'kind': 'container'}


def plan_asset_layer(a, out_dir, export_dir, rule_version):
    """Planned image/PNG asset. Uses pngPlacement (the render box family);
    rotated assets carry effectiveMatrix and are wrapped generically."""
    pp = a.get('pngPlacement') or a.get('renderAABB') or {}
    es = a.get('exportSize')
    if isinstance(es, dict):
        size = [es.get('w', es.get('width', 0)), es.get('h', es.get('height', 0))]
    elif isinstance(es, (list, tuple)):
        size = list(es)
    else:
        size = [pp.get('w', pp.get('width', 0)), pp.get('h', pp.get('height', 0))]
    nid = a.get('nodeId', '?')
    op = a.get('effectiveOpacity', a.get('opacity', 1))
    disposition = a.get('disposition', 'rendered-asset')
    data_attr = (f'data-n="{esc(nid)}" data-rule="{esc(rule_version)}" '
                 f'data-disposition="{esc(disposition)}"')
    src = ''
    rel = a.get('asset')
    if not rel and nid and nid != '?':
        # Real compiler names PNGs by nodeId: "1120:13709" -> "1120-13709.png".
        rel = f"{nid.replace(':', '-')}.png"
    if rel:
        p = os.path.join(export_dir, rel)
        if os.path.exists(p):
            src = os.path.relpath(p, out_dir).replace(os.sep, '/')
    if not src:
        return {'z': _zchain(a), 'html': '', 'disclosures': [{
            'kind': 'image-fill-unresolved', 'node': nid,
            'reason': 'planned asset has no resolvable file; omitted, not faked'}],
            'nodeId': nid, 'kind': 'asset', 'skipped': True}
    # Export PNG is ALWAYS rotation-baked pixel content (see export_artboard:
    # the PNG already contains the node rendered at its own rotation). Place it
    # flat at pngPlacement with its exportSize pixel dims. Do NOT wrap in a CSS
    # matrix and do NOT stretch to designBox/local size: that double-rotates and
    # distorts aspect.
    blend_css, blend_disc = _blend_css(a)
    html_out = (f'<img class="layer" {data_attr} src="{html.escape(src, quote=True)}" alt="" '
                f'style="position:absolute;left:{pp.get("x", 0):.4f}px;top:{pp.get("y", 0):.4f}px;'
                f'width:{size[0]:.4f}px;height:{size[1]:.4f}px;opacity:{op};{blend_css}">')
    return {'z': _zchain(a), 'html': html_out,
            'disclosures': ([blend_disc] if blend_disc else []),
            'nodeId': nid, 'kind': 'asset'}


def build_from_plan(plan, out_dir, export_dir, font_css_text='', title=None,
                    conn_map=None):
    """Build fixed HTML purely from a compile_design layout-plan.

    Returns (html_string, report). No raw-SDK free layout: every layer comes from
    a plan row stamped with nodeId + ruleVersion + disposition.
    """
    rule_version = plan.get('ruleVersion', RULE_VERSION)
    art = plan.get('artboard') or {}
    # Real compiler emits artboard.artboardSize{w,h}; tolerate legacy width/height.
    asize = art.get('artboardSize') or {}
    W = _f3(asize.get('w', art.get('w', art.get('width', 1120))))
    H = _f3(asize.get('h', art.get('h', art.get('height', 800))))
    stage_size = (W, H)
    resolver = resolve_image_factory(plan, export_dir, out_dir)

    # Geometry lives on nodes[]; textPlans[] carry styling only. Merge per nodeId.
    node_map = {n.get('nodeId'): n for n in (plan.get('nodes') or []) if n.get('nodeId')}

    layers = []
    disclosures = []
    mappings = []
    skipped_hidden = 0
    skipped_baked = 0
    rendered_assets = set()  # nodeIds already emitted as <img>; skip CSS container

    def _visible(node):
        # Visibility ONLY from the plan's effectiveVisibility.
        if node.get('effectiveVisibility') is False:
            return False
        return bool(node.get('effectiveVisibility', node.get('visible', True)))

    def _is_baked(n):
        """A node baked into an ancestor PNG (or with no render box) is a ledger
        row, NOT a DOM layer -- its pixels already live in the parent asset."""
        if (n.get('disposition') or '').startswith('baked-into-ancestor'):
            return True
        return n.get('renderAABB') is None

    for a in plan.get('assets', []) or []:
        if not _visible(a):
            skipped_hidden += 1
            mappings.append({'nodeId': a.get('nodeId'), 'kind': 'asset',
                             'disposition': a.get('disposition', 'hidden-skipped'),
                             'ruleVersion': rule_version, 'rendered': False})
            continue
        a['_stageW'], a['_stageH'] = W, H
        lyr = plan_asset_layer(a, out_dir, export_dir, rule_version)
        if lyr.get('skipped'):
            disclosures.extend(lyr['disclosures'])
            continue
        layers.append(lyr)
        rendered_assets.add(lyr['nodeId'])
        mappings.append({'nodeId': lyr['nodeId'], 'kind': 'asset',
                         'disposition': a.get('disposition', 'rendered-asset'),
                         'ruleVersion': rule_version, 'rendered': True})

    for n in plan.get('nodes', []) or []:
        if n.get('type') == 'TEXT' or n.get('kind') == 'text':
            continue  # handled by merged textPlans
        # A node that already rendered as a PNG <img> must NOT also emit a CSS
        # container-visual (that would double-draw and re-rotate the baked PNG).
        if n.get('nodeId') in rendered_assets:
            continue
        if not _visible(n):
            skipped_hidden += 1
            mappings.append({'nodeId': n.get('nodeId'), 'kind': 'container',
                             'disposition': n.get('disposition', 'hidden-skipped'),
                             'ruleVersion': rule_version, 'rendered': False})
            continue
        if _is_baked(n):
            skipped_baked += 1
            mappings.append({'nodeId': n.get('nodeId'), 'kind': 'container',
                             'disposition': n.get('disposition', 'baked-into-ancestor-png'),
                             'ruleVersion': rule_version, 'rendered': False})
            continue
        lyr = plan_container_layer(n, stage_size, resolver, rule_version)
        layers.append(lyr)
        mappings.append({'nodeId': lyr['nodeId'], 'kind': 'container',
                         'disposition': n.get('disposition', 'rendered-container'),
                         'ruleVersion': rule_version, 'rendered': True})

    for tp in plan.get('textPlans', []) or []:
        nid = tp.get('nodeId')
        # I1: merge geometry (effectiveMatrix/renderAABB/disposition/...) from
        # nodes[] onto the styling-only textPlan before emitting.
        merged = dict(node_map.get(nid, {}))
        merged.update(tp)
        if not _visible(merged):
            skipped_hidden += 1
            mappings.append({'nodeId': nid, 'kind': 'text',
                             'disposition': merged.get('disposition', 'hidden-skipped'),
                             'ruleVersion': rule_version, 'rendered': False})
            continue
        if _is_baked(merged):
            skipped_baked += 1
            mappings.append({'nodeId': nid, 'kind': 'text',
                             'disposition': merged.get('disposition', 'baked-into-ancestor-png'),
                             'ruleVersion': rule_version, 'rendered': False})
            continue
        lyr = plan_text_layer(merged, stage_size, rule_version)
        layers.append(lyr)
        mappings.append({'nodeId': lyr['nodeId'], 'kind': 'text',
                         'disposition': merged.get('disposition', 'rendered-text'),
                         'ruleVersion': rule_version, 'rendered': True})

    for n in layers:
        disclosures.extend(n.get('disclosures', []))
    layers.sort(key=lambda n: n['z'])

    # Stage background gate: the plan MUST carry rootFills as an explicit key.
    #   - key absent / None   -> unknown -> BLOCK build (never guess transparent)
    #   - rootFills == []     -> explicit no fill -> transparent + disclosure
    #   - rootFills == [...]   -> composite via build_background_css
    if 'rootFills' not in art or art.get('rootFills') is None:
        raise ValueError(
            'build_from_plan: plan.artboard.rootFills is unknown (key missing). '
            'Compile must propagate manifest.rootFills verbatim; refusing to '
            'guess a transparent stage background.')
    root_fills = art['rootFills'] or []
    root_bg, root_disc = build_background_css(root_fills, resolve_image=resolver)
    if not root_fills:
        root_disc.append({'kind': 'stage-explicit-no-fill',
                          'reason': 'artboard.rootFills == []: explicit empty stage '
                                    'background (transparent)'})
    disclosures.extend(root_disc)

    doc = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width={W},initial-scale=1"><title>{esc(title or art.get('name') or 'Figma artboard')}</title><style>
html,body{{margin:0;padding:0;width:{W}px;height:{H}px;overflow:hidden}}#stage{{position:relative;width:{W}px;height:{H}px;overflow:hidden;background:{root_bg};transform-origin:0 0;isolation:isolate}}
.layer,.tlayer,.clip,.container-visual{{position:absolute;display:block;max-width:none}}.clip{{overflow:hidden}}.tcell{{padding:0;border:0}}
{font_css_text}</style></head><body><div id="stage">{''.join(n['html'] for n in layers)}</div></body></html>'''
    report = {
        'mode': 'layout-plan',
        'ruleVersion': rule_version,
        'planHash': plan.get('planHash'),
        'artboardSize': [W, H],
        'mappings': mappings,
        'mappingCount': len(mappings),
        'renderedCount': sum(1 for m in mappings if m['rendered']),
        'skippedHidden': skipped_hidden,
        'skippedBaked': skipped_baked,
        'disclosures': disclosures,
        'disclosureCount': len(disclosures),
        'relationEdgeCount': len(plan.get('relations', []) or []),
        'unresolved': plan.get('unresolved', []),
        'note': 'Built from layout-plan; run audit_relations --strict before delivery.',
    }
    return doc, report


def load_plan(export_dir):
    """Read layout-plan.json next to the export dir. Returns None if absent."""
    p = pathlib.Path(export_dir) / 'layout-plan.json'
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding='utf-8'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--export-dir', required=True)
    ap.add_argument('--output', required=True)
    ap.add_argument('--title')
    ap.add_argument('--font-css', help='Optional CSS file with licensed @font-face rules')
    args = ap.parse_args()
    d = pathlib.Path(args.export_dir).resolve()
    out = pathlib.Path(args.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    font_css = pathlib.Path(args.font_css).read_text(encoding='utf-8') if args.font_css else ''
    plan = load_plan(str(d))
    if plan is not None:
        # Design Compiler path: consume layout-plan (preferred). The Agent does
        # not re-read raw SDK to lay out.
        conn_map = load_connector_map(str(d))
        doc, report = build_from_plan(plan, str(out.parent), str(d),
                                      font_css_text=font_css, title=args.title,
                                      conn_map=conn_map)
        out.write_text(doc, encoding='utf-8')
        report['output'] = str(out)
        report['artboardId'] = (plan.get('artboard') or {}).get('id')
        (out.parent/'build-report.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False))
        return
    # Legacy no-plan path: retained ONLY for existing unit-test compatibility.
    # Reconstruct later builds go through layout-plan.
    m = json.loads((d/'manifest.json').read_text(encoding='utf-8'))
    tm = json.loads((d/'text-manifest.json').read_text(encoding='utf-8'))
    font_css = pathlib.Path(args.font_css).read_text(encoding='utf-8') if args.font_css else ''
    conn_map = load_connector_map(str(d))
    doc, report = build_html(m, tm, str(out.parent), str(d),
                             font_css_text=font_css, title=args.title,
                             conn_map=conn_map)
    out.write_text(doc, encoding='utf-8')
    report['output'] = str(out)
    report['artboardId'] = m['artboardId']
    (out.parent/'build-report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
