#!/usr/bin/env python3
"""Design Compiler -- text repair planner (deterministic, no LLM).

Given the per-node text plan (from design_text.build_text_css) and the browser
measurement (from audit_text), produce a *machine-readable* repair plan:

    {ruleVersion, actions: [...], blocked: [...], unknown: [...]}

Every action is traceable to a closed rule (R-*) and derived purely from the
plan+measured diff. The single hard prohibition: we NEVER shrink font-size to
make an overflow disappear (compiler-contract repairPlan: "no arbitrary font
scaling or fixed compensation"). If the only way to fit is to change type size,
that is a blocked/unknown decision, not an action.

Public API
----------
build_repair_plan(plans, measured, fonts_missing=None) -> dict
apply_css_decl(css_str, suggested_css) -> str
    Deterministically merge a suggested declaration into an existing inline
    CSS string (idempotent). Never touches font-size.
"""
import re

RULE_VERSION = 'design-compiler/text-repair/1.0.0'


def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _set_decl(css, prop, value):
    """Return ``css`` with ``prop:value;`` set, replacing any existing one.

    Deterministic and idempotent. Never used for ``font-size``.
    """
    if prop == 'font-size':
        raise ValueError('repair_text must never rewrite font-size (no shrink-to-fit)')
    pat = re.compile(rf'\s*{re.escape(prop)}\s*:[^;]*;?')
    decl = f'{prop}:{value};'
    if pat.search(css):
        return pat.sub(decl, css).replace(';;', ';')
    return css.rstrip(';') + ';' + decl


def apply_css_decl(css_str, suggested_css):
    """Merge a suggested declaration (e.g. 'white-space:pre-wrap;') into css."""
    out = css_str or ''
    for decl in [d.strip() for d in (suggested_css or '').split(';') if d.strip()]:
        if ':' not in decl:
            continue
        prop, value = decl.split(':', 1)
        out = _set_decl(out, prop.strip(), value.strip())
    return out


def build_repair_plan(plans, measured, fonts_missing=None):
    """Build the deterministic repair plan.

    Parameters
    ----------
    plans : list[dict]
        Outputs of design_text.build_text_css (the intended CSS).
    measured : list[dict]
        Per-node browser measurement, e.g.::
            {nodeId, mode, computedWhiteSpace, computedLineHeight,
             overflow: {x, y}, hasTableWrapper: bool, outerWidthPx: float|None,
             resolvedPx: float|None, runFontSizes: [..]}
    fonts_missing : list[str]
        Families requested in the manifest that document.fonts.check failed on.
    """
    fonts_missing = fonts_missing or []
    plan_by_id = {p.get('nodeId'): p for p in (plans or [])}
    measured_by_id = {m.get('nodeId'): m for m in (measured or [])}

    actions, blocked, unknown = [], [], []

    for nid, plan in plan_by_id.items():
        m = measured_by_id.get(nid) or {}
        mode = plan.get('mode')
        over = m.get('overflow') or {}
        ox = _num(over.get('x'))
        oy = _num(over.get('y'))
        overrun = ox > 2
        overheight = oy > 4

        # R-WS-1: fixed box rendered as `pre` (or otherwise not wrapping) while
        # it overflows horizontally -> must wrap at box width (RC-1).
        cws = (m.get('computedWhiteSpace') or '').strip()
        if overrun and cws == 'pre':
            actions.append({
                'nodeId': nid, 'rule': 'R-WS-1', 'kind': 'setTextLayout',
                'currentCss': 'white-space:pre;',
                'suggestedCss': 'white-space:pre-wrap;',
                'autoApplicable': True,
                'reason': 'fixed box overflows horizontally; trailing \\n must not '
                          'downgrade the box to pre (RC-1) -> pre-wrap',
            })

        # R-WS-2: overrun without a display:table wrapper -> anonymous table
        # width propagation is fragile; wrap explicitly (RC-2).
        if overrun and m.get('hasTableWrapper') is False:
            actions.append({
                'nodeId': nid, 'rule': 'R-WS-2', 'kind': 'setTextLayout',
                'currentCss': 'bare display:table-cell (no display:table parent)',
                'suggestedCss': 'wrap in display:table;table-layout:fixed;width:<box.w>',
                'autoApplicable': True,
                'reason': 'RC-2: unify matrix/non-matrix branches behind a real '
                          'display:table wrapper',
            })

        # R-AUTORESIZE: content-mode node rendered with a fixed outer width ->
        # nowrap cannot grow; release width to auto (RC-7).
        if mode in ('content-size', 'content-width'):
            outer = m.get('outerWidthPx')
            if outer is not None and _num(outer) > 0:
                actions.append({
                    'nodeId': nid, 'rule': 'R-AUTORESIZE', 'kind': 'setTextLayout',
                    'currentCss': f'width:{_num(outer):.1f}px;',
                    'suggestedCss': 'width:auto;',
                    'autoApplicable': True,
                    'reason': 'WIDTH_AND_HEIGHT/HEIGHT_AND_WIDTH must size to '
                              'content; fixed outer width clips nowrap text (RC-7)',
                })

        # R-LH-1: vertical overflow with line-height:normal and a resolved px
        # available -> emit the resolved pixel leading (RC-3).
        clh = (m.get('computedLineHeight') or '').strip()
        _runs = plan.get('runs') or []
        resolved = m.get('resolvedPx') or (_runs[0].get('lineHeightPx') if _runs else None)
        if overheight and clh == 'normal' and resolved:
            actions.append({
                'nodeId': nid, 'rule': 'R-LH-1', 'kind': 'setTextLayout',
                'currentCss': 'line-height:normal;',
                'suggestedCss': f'line-height:{_num(resolved):.3f}px;',
                'autoApplicable': True,
                'reason': 'AUTO line-height emitted as browser intrinsic normal; '
                          'use resolved Figma leading (RC-3)',
            })

        # R-LH-MULTILINE: a soft-wrapped multi-line box whose line-height was
        # wrongly derived as the whole box height (e.g. 624px) -> collapse to
        # browser normal leading. Deterministic, never shrinks font.
        lc = _num(m.get('lineCount'), 1)
        explicit = _num(m.get('explicitLines'), 1)
        fs = _num(m.get('fontSizePx'), 0)
        clh_val = None
        mclh = (m.get('computedLineHeight') or '').strip()
        if mclh.endswith('px'):
            try:
                clh_val = float(mclh[:-2])
            except ValueError:
                clh_val = None
        if (lc > explicit + 1 and fs > 0 and clh_val is not None
                and clh_val > fs * 3 and overheight):
            actions.append({
                'nodeId': nid, 'rule': 'R-LH-MULTILINE', 'kind': 'setTextLayout',
                'currentCss': f'line-height:{clh_val:.1f}px (whole box height)',
                'suggestedCss': 'line-height:normal;',
                'autoApplicable': True,
                'reason': 'multi-line soft-wrapped box must not read box height as '
                          'per-line leading; use browser normal (no font shrink)',
            })

        # R-RUN-1: mixed runs where per-run font-size is not honored in markup.
        run_sizes = m.get('runFontSizes') or []
        plan_runs = plan.get('runs') or []
        if len(plan_runs) > 1 and len(set(round(r['font_size_px'], 2)
                                          for r in plan_runs)) > 1:
            if len(set(run_sizes)) <= 1 and run_sizes:
                actions.append({
                    'nodeId': nid, 'rule': 'R-RUN-1', 'kind': 'setTextLayout',
                    'currentCss': 'all runs share one font-size',
                    'suggestedCss': 'add per-span font-size (no other change)',
                    'autoApplicable': True,
                    'reason': 'RC-4: mixed-size runs must carry per-run leading '
                              'and size',
                })

        # R-AFFINE: recorded matrix mismatch (affine rebuild).
        if m.get('affineMismatch'):
            actions.append({
                'nodeId': nid, 'rule': 'R-AFFINE', 'kind': 'setRelationGeometry',
                'currentCss': m.get('currentTransform', ''),
                'suggestedCss': f'transform:matrix({m["plannedMatrix"]});',
                'autoApplicable': True,
                'reason': 'rotated/translated text drift; rebuild from accumulated '
                          'affine (transform-origin:0 0)',
            })

    # R-FONT-1: missing fonts are NOT auto-fixable without the font bytes.
    if fonts_missing:
        blocked.append({
            'rule': 'R-FONT-1', 'kind': 'requireExternalData',
            'families': list(fonts_missing),
            'suggestedCss': 'inject licensed @font-face, or disclose unresolved',
            'reason': 'requested family failed document.fonts.check; cannot be '
                      'patched from CSS alone (RC-6). Do not silently fall back.',
        })

    # Anything still unknown (gradient/image fills, no-fill) stays unknown.
    for nid, plan in plan_by_id.items():
        for d in plan.get('disclosures', []):
            unknown.append({'nodeId': nid, 'kind': d.get('kind'),
                            'reason': d.get('reason')})

    return {
        'ruleVersion': RULE_VERSION,
        'actions': actions,
        'blocked': blocked,
        'unknown': unknown,
        'summary': {
            'actions': len(actions),
            'blocked': len(blocked),
            'unknown': len(unknown),
            'fontSizeRewrites': 0,  # invariant: never shrink font
        },
    }


if __name__ == '__main__':
    import json, sys
    data = json.loads(sys.stdin.read())
    print(json.dumps(build_repair_plan(
        data.get('plans', []), data.get('measured', []),
        data.get('fontsMissing', [])), ensure_ascii=False, indent=2))
