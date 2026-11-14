#!/usr/bin/env python3
"""Design Compiler -- text module (pure, deterministic).

This is the *build-side* compiler for Figma TEXT nodes. It turns a normalized
text manifest entry (the ``text_ir``) into a closed-decision-table CSS result.
There is no browser, no SDK re-read, no natural-language guess: every emitted
CSS declaration is traceable to one row of a decision table keyed on fields
present in the normalized IR (compiler-contract.json, invariants I1..I5).

Public API
----------
normalize_runs(t) -> dict
    Normalize a raw text-manifest entry into node-level derived fields plus a
    list of runs. Implements normalization rules N1..N4.
decide_text_layout(t) -> dict
    The white-space / width decision table (I1). Pure function of
    (textAutoResize, local.width, presence of explicit hard break).
build_text_css(text_ir) -> dict
    The main entry point: deterministic CSS declarations + constraints + runs.

White-space policy (the RC-1 / RC-7 fix)
----------------------------------------
* fixed box (textAutoResize == NONE, or a finite local.width) -> ``pre-wrap``.
  It preserves embedded ``\\n`` / U+2028 AND soft-wraps at the box width. A box
  that merely *ends* in trailing ``\\n\\n`` must NOT be downgraded to ``pre``.
* auto width (WIDTH_AND_HEIGHT, or HEIGHT_AND_WIDTH) -> the box grows with
  content, so soft wrapping is suppressed. When the text has no hard break we
  emit ``nowrap`` (one growing line); when it DOES contain ``\\n`` / U+2028 we
  emit ``pre-line`` so the hard breaks are still honoured (``nowrap`` would
  collapse them to a space). Outer width is ``auto``.
* the only ``pre`` case is the defensive fallback: no autoResize, no finite
  width, no wrapping intent (rare).

We never blanket-apply ``nowrap``/``pre`` to the whole artboard: the value is
chosen per node by the table above.
"""
import re

# ---------------------------------------------------------------------------
# font style / weight
# ---------------------------------------------------------------------------
WEIGHT = {'Thin': 100, 'Extralight': 200, 'ExtraLight': 200, 'Light': 300,
          'Regular': 400, 'Medium': 500, 'Semibold': 600, 'SemiBold': 600,
          'Bold': 700, 'Heavy': 800, 'Black': 900}

_HARD_BREAK_RE = re.compile(r'[\n\u2028\u2029]')
_ALIGN_H = {'LEFT': 'left', 'CENTER': 'center', 'RIGHT': 'right',
            'JUSTIFIED': 'justify'}
_ALIGN_V = {'TOP': 'top', 'CENTER': 'middle', 'BOTTOM': 'bottom'}


def _num(v, default=None):
    try:
        if v is None or v == '':
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _has_finite_width(local):
    w = _num((local or {}).get('width'))
    return w is not None and w > 0


def count_explicit_lines(characters):
    """Number of lines implied by hard breaks (``\\n`` / U+2028 / U+2029).

    A trailing ``\\n`` produces an empty final line, which Figma counts as a
    line box -- so ``"a\\n\\n"`` is 3 parts = 3 explicit lines.
    """
    if characters is None:
        return 1
    parts = re.split(r'[\n\u2028\u2029]', str(characters))
    return max(1, len(parts))


def trailing_newline_count(characters):
    if not characters:
        return 0
    n = 0
    for ch in reversed(str(characters)):
        if ch in ('\n', '\u2028', '\u2029'):
            n += 1
        else:
            break
    return n


def _font_style_css(style):
    s = str(style or '').lower()
    if 'italic' in s or 'oblique' in s:
        return 'italic'
    return 'normal'


def _font_weight_css(style):
    s = str(style or '')
    # strip the italic/oblique word before lookup ("Semibold Italic" -> Semibold)
    base = re.sub(r'\b(italic|oblique)\b', '', s, flags=re.IGNORECASE).strip()
    return WEIGHT.get(base, 400)


def _letter_spacing_css(ls):
    """N3: missing unit defaults to PIXELS value 0 (Figma default unit is px)."""
    ls = ls or {}
    unit = ls.get('unit')
    value = _num(ls.get('value'), 0.0)
    if unit == 'PERCENT':
        # Figma letterSpacing percent is relative to font size.
        return f'letter-spacing:{value / 100:.6f}em;'
    # PIXELS (default)
    return f'letter-spacing:{value:.3f}px;'


def _line_height_css(lh, fallback_size, resolved_px=None):
    """Return (css_decl, disclosures).

    AUTO + resolvedPx -> ``line-height:<px>px`` (RC-3 fix).
    AUTO without resolvedPx -> ``line-height:normal`` + disclosure (unknown).
    PERCENT -> unitless multiplier (e.g. 1.4).
    PIXELS -> ``line-height:<px>px``.
    """
    lh = lh or {}
    unit = lh.get('unit', 'AUTO')
    disclosures = []
    if unit == 'PIXELS':
        v = _num(lh.get('value'), fallback_size)
        return f'line-height:{v:.3f}px;', disclosures
    if unit == 'PERCENT':
        v = _num(lh.get('value'), 100.0)
        return f'line-height:{v / 100:.5f};', disclosures
    # AUTO
    px = resolved_px if resolved_px is not None else _num(lh.get('resolvedPx'))
    if px is not None and px > 0:
        return f'line-height:{px:.3f}px;', disclosures
    disclosures.append({
        'kind': 'line-height-auto-unresolved',
        'reason': 'lineHeight.unit=AUTO with no resolvedPx; emitted '
                  'line-height:normal (browser intrinsic ~1.2); Figma AUTO '
                  'is family-specific and cannot be reproduced without the '
                  'resolved pixel value'})
    return 'line-height:normal;', disclosures


# ---------------------------------------------------------------------------
# normalization (N1..N4)
# ---------------------------------------------------------------------------
def normalize_runs(t):
    """Normalize a raw text-manifest entry.

    Returns a dict with:
      nodeId, characters, fontName, fontSize, fills,
      lineHeight (normalized, with resolvedPx derived when missing),
      letterSpacing (normalized, N3),
      hasHardBreak, hardBreakCount, trailingNewlineCount,
      runs: [{characters, font_family, font_style, font_weight,
               font_size_px, fills, lineHeightPx, letterSpacingPx,
               start, end}]
    """
    t = t or {}
    node_id = t.get('nodeId')
    characters = t.get('characters', '') or ''
    disclosures = []

    # N1: accept segments under any of the wire names Figma uses.
    raw_segs = (t.get('segments')
                or t.get('styledTextSegments')
                or t.get('styledTextRuns')
                or [])
    if not isinstance(raw_segs, list):
        raw_segs = []

    parent_fn = t.get('fontName')
    if not isinstance(parent_fn, dict):
        parent_fn = {}  # string "mixed"/family is expanded per-run below
    # N-font: parent font size is numeric only when the manifest says so.
    # "mixed" / missing is NOT silently coerced to 16px. It becomes None and
    # then EVERY run must supply its own numeric fontSize, else hard failure.
    parent_size = _num(t.get('fontSize'))
    parent_fills = t.get('fills')
    parent_lh = t.get('lineHeight') or {}
    parent_ls = t.get('letterSpacing') or {}

    explicit_lines = count_explicit_lines(characters)
    # N-line: AUTO is a live value; resolvedPx is trusted ONLY when export
    # measured it. Never fabricate from box height / line count.
    resolved_px = _num(parent_lh.get('resolvedPx'))

    # N3: letterSpacing default unit PIXELS 0.
    if not parent_ls.get('unit'):
        parent_ls = dict(parent_ls)
        parent_ls['unit'] = 'PIXELS'
        parent_ls.setdefault('value', 0)

    def _require_size(node_id, seg_index, raw_size, ctx):
        """Return a numeric font size or raise. No 16px impersonation."""
        v = _num(raw_size)
        if v is None or v <= 0:
            raise ValueError(
                f'text node {node_id}: run #{seg_index} ({ctx!r}) has no numeric '
                f'fontSize; parent fontSize={t.get("fontSize")!r}. Hard failure, '
                f'not a 16px default.')
        return v

    runs = []
    if raw_segs:
        for idx, s in enumerate(raw_segs):
            if not isinstance(s, dict):
                continue
            s_chars = s.get('characters')
            if s_chars is None:
                s_chars = s.get('text', '')
            # Figma segments may carry style/fontSize at top level
            # ({"fontSize":34,"style":"Semibold","text":...}) OR nested under
            # fontName. Read both, preferring the segment's own values.
            s_fn = s.get('fontName') or {}
            s_style = s.get('style') or s_fn.get('style') or parent_fn.get('style')
            s_family = s_fn.get('family') or parent_fn.get('family', 'sans-serif')
            # Inherit parent size only when parent size is numeric; otherwise the
            # run must provide its own.
            raw_size = s.get('fontSize') if s.get('fontSize') is not None else parent_size
            s_size = _require_size(node_id, idx, raw_size, s_chars)
            s_fills = s.get('fills', parent_fills)
            s_lh = s.get('lineHeight') or parent_lh
            s_lh_px = _num(s_lh.get('resolvedPx'))
            s_ls = s.get('letterSpacing') or parent_ls
            runs.append({
                'characters': s_chars or '',
                'font_family': s_family,
                'font_style': _font_style_css(s_style),
                'font_weight': _font_weight_css(s_style),
                'font_size_px': s_size,
                'fills': s_fills,
                'lineHeightPx': s_lh_px,
                'letterSpacingPx': _num(s_ls.get('value'), 0.0),
                'start': s.get('start'),
                'end': s.get('end'),
            })
    else:
        # No segments: a single whole-text run. Its size must be the numeric
        # parent fontSize; mixed/missing with visible characters is a hard fail.
        if characters:
            s_size = _require_size(node_id, 0, parent_size, characters)
        else:
            s_size = None
        runs.append({
            'characters': characters,
            'font_family': parent_fn.get('family', 'sans-serif'),
            'font_style': _font_style_css(parent_fn.get('style')),
            'font_weight': _font_weight_css(parent_fn.get('style')),
            'font_size_px': s_size,
            'fills': parent_fills,
            'lineHeightPx': resolved_px,
            'letterSpacingPx': _num(parent_ls.get('value'), 0.0),
            'start': None,
            'end': None,
        })

    return {
        'nodeId': node_id,
        'characters': characters,
        'fontName': parent_fn,
        'fontSize': parent_size,
        'fills': parent_fills,
        'lineHeight': dict(parent_lh, resolvedPx=resolved_px),
        'letterSpacing': parent_ls,
        'hasHardBreak': bool(_HARD_BREAK_RE.search(characters)),
        'hardBreakCount': explicit_lines - 1,
        'explicitLineCount': explicit_lines,
        'trailingNewlineCount': trailing_newline_count(characters),
        'runs': runs,
        'disclosures': disclosures,
    }


# ---------------------------------------------------------------------------
# font-family enumeration (I3: fontName may be a dict OR the string "mixed")
# ---------------------------------------------------------------------------
def _add_family(acc, fam):
    if isinstance(fam, str):
        fam = fam.strip()
        if fam and fam.lower() != 'mixed' and fam not in acc:
            acc.append(fam)


def enumerate_families(t):
    """Deterministically enumerate the font families a TEXT node uses.

    ``fontName`` may be:
      * a dict ``{family, style}``;
      * a plain string that IS the family name;
      * the string ``"mixed"`` -- then families must be expanded from the runs
        (``segments`` / ``styledTextSegments`` / ``styledTextRuns`` /
        ``characterStyleOverrides``), never guessed.

    Returns ``(families, unresolved)``. ``unresolved`` is True when no family
    could be resolved (e.g. fontName='mixed' with no usable runs) -- the
    caller must disclose it, not assume a fallback.
    """
    t = t or {}
    found = []

    fn = t.get('fontName')
    if isinstance(fn, dict):
        _add_family(found, fn.get('family'))
    elif isinstance(fn, str):
        # A bare string that isn't "mixed" is treated as a literal family name.
        if fn.strip().lower() != 'mixed':
            _add_family(found, fn)
        # 'mixed' falls through to run expansion below; do not invent a family.

    # Expand runs under any of the wire names Figma uses.
    run_lists = (t.get('segments'), t.get('styledTextSegments'),
                 t.get('styledTextRuns'))
    for segs in run_lists:
        if isinstance(segs, list):
            for s in segs:
                if not isinstance(s, dict):
                    continue
                sfn = s.get('fontName')
                if isinstance(sfn, dict):
                    _add_family(found, sfn.get('family'))
                elif isinstance(sfn, str):
                    _add_family(found, sfn)

    # characterStyleOverrides: {index: {fontName: {family,...}}}
    cso = t.get('characterStyleOverrides')
    if isinstance(cso, dict):
        for v in cso.values():
            if isinstance(v, dict):
                sfn = v.get('fontName')
                if isinstance(sfn, dict):
                    _add_family(found, sfn.get('family'))
                elif isinstance(sfn, str):
                    _add_family(found, sfn)

    return found, (len(found) == 0)


# ---------------------------------------------------------------------------
# decision table (I1)
# ---------------------------------------------------------------------------
def decide_text_layout(t):
    """White-space / outer-width decision table.

    Pure function of (textAutoResize, local.width, presence of hard break).
    Returns {mode, whiteSpace, widthStrategy, outerWidth, verticalAlign,
             textAlign}.
    """
    t = t or {}
    ar = t.get('textAutoResize', 'NONE')
    local = t.get('local') or {}
    w = _num(local.get('width'))
    has_finite_w = w is not None and w > 0
    chars = t.get('characters', '') or ''
    has_hard = bool(_HARD_BREAK_RE.search(chars))

    if ar == 'WIDTH_AND_HEIGHT':
        # box grows with content on both axes: no soft wrap.
        return {
            'mode': 'content-size',
            'whiteSpace': 'nowrap' if not has_hard else 'pre-line',
            'widthStrategy': 'auto',
            'outerWidth': 'auto',
        }
    if ar == 'HEIGHT_AND_WIDTH':
        # height fixed, width grows: still no soft wrap.
        return {
            'mode': 'content-width',
            'whiteSpace': 'nowrap' if not has_hard else 'pre-line',
            'widthStrategy': 'auto',
            'outerWidth': 'auto',
        }
    if ar == 'NONE' or has_finite_w:
        # fixed box: wrap at box width, preserve hard breaks. Trailing \n
        # must NOT downgrade this to pre.
        return {
            'mode': 'fixed-box',
            'whiteSpace': 'pre-wrap',
            'widthStrategy': 'fixed',
            'outerWidth': f'{w:g}px' if has_finite_w else 'auto',
        }
    # defensive fallback: no autoResize, no finite width, no wrap intent.
    return {
        'mode': 'fixed-box',
        'whiteSpace': 'pre',
        'widthStrategy': 'auto',
        'outerWidth': 'auto',
    }


# ---------------------------------------------------------------------------
# main build
# ---------------------------------------------------------------------------
def build_text_css(text_ir):
    """Compile a normalized text IR into deterministic CSS + constraints.

    Returns a dict:
      nodeId, mode, whiteSpace, widthStrategy, outerWidth,
      css: str (inline declarations for the text cell),
      runs: [..], disclosures: [..],
      constraints: {hasHardBreak, hardBreakCount, trailingNewlineCount,
                    explicitLineCount, paragraphSpacingPx, paragraphIndentPx,
                    expectedSoftWrap: bool}
    """
    node = normalize_runs(text_ir)
    layout = decide_text_layout(text_ir)
    t = text_ir or {}
    disclosures = list(node['disclosures'])

    fn = t.get('fontName')
    if not isinstance(fn, dict):
        fn = {}  # string ("mixed"/family) is handled via runs below
    size = node['fontSize']
    lh_css, lh_disc = _line_height_css(
        t.get('lineHeight'), size, resolved_px=node['lineHeight'].get('resolvedPx'))
    disclosures.extend(lh_disc)
    ls_css = _letter_spacing_css(node['letterSpacing'])

    align_h = _ALIGN_H.get((t.get('textAlignHorizontal') or 'LEFT').upper(), 'left')
    align_v = _ALIGN_V.get((t.get('textAlignVertical') or 'TOP').upper(), 'top')

    # paragraph indent (first-line) and spacing. Indent is encodable inline;
    # spacing between hard-broken paragraphs needs paragraph splitting, so it is
    # surfaced as a constraint rather than faked with a magic margin.
    para_indent = _num(t.get('paragraphIndent'), 0.0) or 0.0
    para_spacing = _num(t.get('paragraphSpacing'), 0.0) or 0.0
    indent_css = f'text-indent:{para_indent:.3f}px;' if para_indent > 0 else ''

    # Node-level fontName may be the string "mixed" -> fall back to the first
    # run's family (runs carry the real per-segment {family,style}).
    family = fn.get('family')
    if not family and node['runs']:
        family = node['runs'][0].get('font_family')
    family = family or 'sans-serif'
    declarations = [
        f"font-family:'{family}';",
        f"font-style:{node['runs'][0]['font_style'] if node['runs'] else 'normal'};",
        f"font-weight:{node['runs'][0]['font_weight'] if node['runs'] else 400};",
    ]
    # Cell font-size only when a real numeric parent size exists. Mixed runs
    # carry their own font-size on each <span>; never emit a fabricated px.
    if isinstance(size, (int, float)) and size > 0:
        declarations.append(f"font-size:{size:.3f}px;")
    declarations += [
        lh_css,
        ls_css,
        f'text-align:{align_h};',
        f'vertical-align:{align_v};',
        f"white-space:{layout['whiteSpace']};",
        indent_css,
    ]
    css = ''.join(declarations)

    return {
        'nodeId': node['nodeId'],
        'mode': layout['mode'],
        'whiteSpace': layout['whiteSpace'],
        'widthStrategy': layout['widthStrategy'],
        'outerWidth': layout['outerWidth'],
        'css': css,
        'runs': node['runs'],
        'disclosures': disclosures,
        'constraints': {
            'hasHardBreak': node['hasHardBreak'],
            'hardBreakCount': node['hardBreakCount'],
            'explicitLineCount': node['explicitLineCount'],
            'trailingNewlineCount': node['trailingNewlineCount'],
            'paragraphSpacingPx': para_spacing,
            'paragraphIndentPx': para_indent,
            # fixed-box pre-wrap EXPECTS soft wrapping; content modes do not.
            'expectedSoftWrap': layout['mode'] == 'fixed-box',
        },
    }


# ---------------------------------------------------------------------------
# pure line-break classification (shared with audit_text)
# ---------------------------------------------------------------------------
def classify_lines(design_characters, observed_line_count, mode, overflow):
    """Classify observed lines into explicit / soft / added.

    Returns {explicitLines, softWrapLines, addedLines, hardFailure: bool,
              reason: str|None}.

    * explicitLines = lines implied by hard breaks in the design.
    * For fixed-box (pre-wrap) soft wrapping at the box width is expected;
      observed - explicit is softWrapLines and is NOT a failure.
    * For content modes (nowrap/pre-line) there must be NO soft wrap: any
      observed line beyond explicitLines is an *added* break -> hard failure.
    * overflow (x>2 or y>2) is always a hard failure.
    """
    explicit = count_explicit_lines(design_characters)
    overflow = overflow or {}
    ox = _num(overflow.get('x'), 0.0) or 0.0
    oy = _num(overflow.get('y'), 0.0) or 0.0
    # A fixed Figma text box may intentionally be shorter than its visible line
    # stack; ancestor clipping, not the text box height, decides visibility.
    # Horizontal overflow still fails because it indicates an uncompiled line
    # plan. Content-sized modes remain strict on both axes.
    over = ox > 2 or (oy > 2 and mode != 'fixed-box')
    added = 0
    soft = 0
    if mode == 'fixed-box':
        soft = max(0, observed_line_count - explicit)
    else:
        added = max(0, observed_line_count - explicit)
    reason = None
    hard = False
    if over:
        hard = True
        reason = f'overflow x={ox:.1f} y={oy:.1f}'
    if added > 0:
        hard = True
        reason = (reason + '; ' if reason else '') + \
                 f'added wrap: observed {observed_line_count} lines, design ' \
                 f'{explicit} (content mode forbids soft wrap)'
    return {
        'explicitLines': explicit,
        'softWrapLines': soft,
        'addedLines': added,
        'fixedBoxVerticalOverflow': mode == 'fixed-box' and oy > 2,
        'hardFailure': hard,
        'reason': reason,
    }


if __name__ == '__main__':
    import json, sys
    ir = json.loads(sys.stdin.read())
    print(json.dumps(build_text_css(ir), ensure_ascii=False, indent=2))
