#!/usr/bin/env python3
"""Synthetic regressions for the Design Compiler text module.

Covers the contract in iteration-3/diagnostics/compiler-contract.json:
  * fixed-box long text soft-wraps (trailing \\n must NOT downgrade to pre);
  * auto-width (WIDTH_AND_HEIGHT) grows content, no soft wrap;
  * explicit \\n / U+2028 preserved;
  * mixed runs carry per-run font-size/weight;
  * AUTO line-height resolves to px when the design box is known;
  * font fallback / missing family is disclosed, not silently passed;
  * repair plan is deterministic and NEVER shrinks font-size.

Pure Python (no browser). Run:
    python3 tests/test_text_compiler.py
Exits non-zero on first failure.
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, 'scripts')
sys.path.insert(0, SCRIPTS)

import design_text as dtc      # noqa: E402
import repair_text as rt       # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(('  ok  ' if cond else ' FAIL ') + name +
          (('  -- ' + str(detail)) if detail and not cond else ''))


def _css_decl(css, prop):
    for part in css.split(';'):
        if ':' in part:
            k, v = part.split(':', 1)
            if k.strip() == prop:
                return v.strip()
    return None


# ---------------------------------------------------------------------------
# 1. fixed box: long text soft-wraps; trailing \n must NOT become pre (RC-1)
# ---------------------------------------------------------------------------
def test_fixed_box_wraps_not_pre():
    print('[1] fixed-box long text wraps; trailing newline is not pre')
    long_text = '视觉和前庭感受冲突，大脑预测不了当前状态。' * 12
    t = {'characters': long_text + '\n\n\n',
         'textAutoResize': 'NONE', 'local': {'width': 400},
         'fontName': {'family': 'PingFang SC', 'style': 'Regular'},
         'fontSize': 19, 'lineHeight': {'unit': 'AUTO'},
         'letterSpacing': {'unit': 'PERCENT', 'value': 0}}
    r = dtc.build_text_css(t)
    check('fixed box uses pre-wrap', r['whiteSpace'] == 'pre-wrap', r['whiteSpace'])
    check('fixed box NOT pre', r['whiteSpace'] != 'pre')
    check('outer width = local.width', r['outerWidth'] == '400px', r['outerWidth'])
    check('trailing newlines counted', r['constraints']['trailingNewlineCount'] == 3,
          r['constraints'])
    check('fixed box expects soft wrap', r['constraints']['expectedSoftWrap'] is True)


# ---------------------------------------------------------------------------
# 2. auto width: W_AND_H grows, nowrap, outer width auto (RC-7)
# ---------------------------------------------------------------------------
def test_auto_width_grows():
    print('[2] WIDTH_AND_HEIGHT grows content, no fixed width')
    t = {'characters': '短标题', 'textAutoResize': 'WIDTH_AND_HEIGHT',
         'fontName': {'family': 'PingFang SC', 'style': 'Semibold'}, 'fontSize': 34}
    r = dtc.build_text_css(t)
    check('content-size mode', r['mode'] == 'content-size', r['mode'])
    check('no hard break -> nowrap', r['whiteSpace'] == 'nowrap', r['whiteSpace'])
    check('outer width auto', r['outerWidth'] == 'auto', r['outerWidth'])
    check('semibold weight 600', r['runs'][0]['font_weight'] == 600, r['runs'][0])
    check('no soft wrap expected', r['constraints']['expectedSoftWrap'] is False)


# ---------------------------------------------------------------------------
# 3. explicit \n inside auto-width is preserved (pre-line, not nowrap collapse)
# ---------------------------------------------------------------------------
def test_explicit_newline_preserved():
    print('[3] explicit \\n / U+2028 preserved in auto-width mode')
    t = {'characters': '第一行\n第二行', 'textAutoResize': 'WIDTH_AND_HEIGHT',
         'fontName': {'family': 'PingFang SC', 'style': 'Regular'}, 'fontSize': 19}
    r = dtc.build_text_css(t)
    check('hard break detected', r['constraints']['hasHardBreak'] is True)
    check('auto-width w/ hard break -> pre-line', r['whiteSpace'] == 'pre-line',
          r['whiteSpace'])
    # U+2028 is also a hard break.
    t2 = {'characters': 'a b', 'textAutoResize': 'NONE', 'local': {'width': 100},
          'fontName': {'family': 'D', 'style': 'Regular'}, 'fontSize': 14}
    r2 = dtc.build_text_css(t2)
    check('U+2028 counts as hard break', r2['constraints']['hasHardBreak'] is True,
          r2['constraints'])


# ---------------------------------------------------------------------------
# 4. mixed runs carry per-run font-size / weight (RC-4)
# ---------------------------------------------------------------------------
def test_mixed_runs():
    print('[4] mixed-size runs normalize per-run style')
    t = {'characters': '', 'textAutoResize': 'NONE', 'local': {'width': 400},
         'fontName': {'family': 'PingFang SC', 'style': 'Regular'}, 'fontSize': 19,
         'segments': [
             {'fontSize': 34, 'style': 'Semibold', 'text': '1.为什么会晕？\n'},
             {'fontSize': 9, 'style': 'Regular', 'text': '  '},
             {'fontSize': 19, 'style': 'Regular', 'text': '视觉和前庭感受冲突'},
         ]}
    r = dtc.build_text_css(t)
    sizes = [round(x['font_size_px']) for x in r['runs']]
    check('three runs', len(r['runs']) == 3, sizes)
    check('run sizes 34/9/19', sizes == [34, 9, 19], sizes)
    check('first run semibold 600', r['runs'][0]['font_weight'] == 600,
          r['runs'][0])


# ---------------------------------------------------------------------------
# 5. AUTO line-height resolves to px from design box (RC-3)
# ---------------------------------------------------------------------------
def test_auto_lineheight_px():
    print('[5] AUTO line-height: measured resolvedPx -> px; box height never faked')
    t = {'characters': '标题', 'textAutoResize': 'NONE', 'local': {'width': 1110},
         'relativeToArtboard': {'x': 0, 'y': 0, 'width': 1110, 'height': 88},
         'fontName': {'family': 'PingFang SC', 'style': 'Semibold'}, 'fontSize': 40,
         'lineHeight': {'unit': 'AUTO', 'resolvedPx': 52.0}}
    r = dtc.build_text_css(t)
    check('line-height trusts measured resolvedPx',
          _css_decl(r['css'], 'line-height') == '52.000px',
          _css_decl(r['css'], 'line-height'))
    # And a box of height 88 WITHOUT resolvedPx must NOT fabricate 88px.
    t2 = dict(t); t2['lineHeight'] = {'unit': 'AUTO'}
    r2 = dtc.build_text_css(t2)
    check('box height alone does not fake line-height',
          _css_decl(r2['css'], 'line-height') == 'normal',
          _css_decl(r2['css'], 'line-height'))


def test_auto_lineheight_unresolved_disclosed():
    print('[6] AUTO line-height without box -> normal + disclosure')
    t = {'characters': 'x', 'textAutoResize': 'NONE', 'local': {'width': 100},
         'fontName': {'family': 'D', 'style': 'Regular'}, 'fontSize': 14,
         'lineHeight': {'unit': 'AUTO'}}
    r = dtc.build_text_css(t)
    check('falls back to normal',
          _css_decl(r['css'], 'line-height') == 'normal',
          _css_decl(r['css'], 'line-height'))
    check('disclosed unresolved',
          any(x['kind'] == 'line-height-auto-unresolved' for x in r['disclosures']),
          r['disclosures'])


# ---------------------------------------------------------------------------
# 7. letter-spacing units + N3 default
# ---------------------------------------------------------------------------
def test_letter_spacing():
    print('[7] letter-spacing PIXELS / PERCENT / default')
    pct = dtc.build_text_css({'characters': 'x', 'textAutoResize': 'NONE',
                              'local': {'width': 100}, 'fontSize': 20,
                              'letterSpacing': {'unit': 'PERCENT', 'value': 5}})
    check('5% -> 0.05em', _css_decl(pct['css'], 'letter-spacing') == '0.050000em',
          _css_decl(pct['css'], 'letter-spacing'))
    px = dtc.build_text_css({'characters': 'x', 'textAutoResize': 'NONE',
                             'local': {'width': 100}, 'fontSize': 20,
                             'letterSpacing': {'unit': 'PIXELS', 'value': 1.5}})
    check('1.5px -> 1.5px', _css_decl(px['css'], 'letter-spacing') == '1.500px',
          _css_decl(px['css'], 'letter-spacing'))
    default = dtc.build_text_css({'characters': 'x', 'textAutoResize': 'NONE',
                                  'local': {'width': 100}, 'fontSize': 20})
    check('missing unit -> 0px', _css_decl(default['css'], 'letter-spacing') == '0.000px',
          _css_decl(default['css'], 'letter-spacing'))


# ---------------------------------------------------------------------------
# 8. N1: alias wire names for segments
# ---------------------------------------------------------------------------
def test_segments_alias():
    print('[8] N1: styledTextRuns alias normalized')
    t = {'characters': 'ab', 'textAutoResize': 'NONE', 'local': {'width': 100},
         'fontName': {'family': 'D', 'style': 'Regular'}, 'fontSize': 14,
         'styledTextRuns': [{'fontSize': 20, 'style': 'Bold', 'text': 'a'},
                            {'fontSize': 14, 'text': 'b'}]}
    r = dtc.build_text_css(t)
    check('alias yields 2 runs', len(r['runs']) == 2, r['runs'])
    check('first run bold 700', r['runs'][0]['font_weight'] == 700, r['runs'][0])


# ---------------------------------------------------------------------------
# 9. alignment / vertical-align
# ---------------------------------------------------------------------------
def test_alignment():
    print('[9] horizontal + vertical alignment')
    t = {'characters': '居中', 'textAutoResize': 'NONE', 'local': {'width': 200},
         'fontName': {'family': 'D', 'style': 'Regular'}, 'fontSize': 14,
         'textAlignHorizontal': 'JUSTIFIED', 'textAlignVertical': 'BOTTOM'}
    r = dtc.build_text_css(t)
    check('justify', _css_decl(r['css'], 'text-align') == 'justify',
          _css_decl(r['css'], 'text-align'))
    check('vertical bottom -> middle cell',
          _css_decl(r['css'], 'vertical-align') == 'bottom',
          _css_decl(r['css'], 'vertical-align'))


# ---------------------------------------------------------------------------
# 10. line-break classification (audit contract)
# ---------------------------------------------------------------------------
def test_classification():
    print('[10] explicit / soft / added line-break classification')
    fixed = dtc.classify_lines('abc', 5, 'fixed-box', {'x': 0, 'y': 0})
    check('fixed-box soft wrap is not failure', fixed['hardFailure'] is False, fixed)
    check('fixed-box softWrapLines counted', fixed['softWrapLines'] == 4, fixed)
    content = dtc.classify_lines('a\nb', 3, 'content-size', {'x': 0, 'y': 0})
    check('content added wrap IS hard failure', content['hardFailure'] is True, content)
    check('content addedLines counted', content['addedLines'] == 1, content)
    over = dtc.classify_lines('abc', 1, 'fixed-box', {'x': 20, 'y': 0})
    check('horizontal overflow IS hard failure', over['hardFailure'] is True, over)
    fixed_y = dtc.classify_lines('abc', 3, 'fixed-box', {'x': 0, 'y': 80})
    check('fixed-box vertical overflow is design-box observation', fixed_y['hardFailure'] is False, fixed_y)
    content_y = dtc.classify_lines('abc', 2, 'content-size', {'x': 0, 'y': 80})
    check('content-mode vertical overflow IS hard failure', content_y['hardFailure'] is True, content_y)


# ---------------------------------------------------------------------------
# 11. repair plan: deterministic rules, no font shrink
# ---------------------------------------------------------------------------
def test_repair_plan():
    print('[11] repair plan deterministic; never shrinks font')
    plans = [dtc.build_text_css(
        {'characters': '长段落…', 'textAutoResize': 'NONE', 'local': {'width': 300},
         'fontName': {'family': 'PingFang SC', 'style': 'Regular'}, 'fontSize': 19,
         'relativeToArtboard': {'x': 0, 'y': 0, 'width': 300, 'height': 28.5},
         'lineHeight': {'unit': 'AUTO', 'resolvedPx': 28.5}})]
    measured = [{
        'nodeId': plans[0]['nodeId'],
        'computedWhiteSpace': 'pre',           # wrong: fixed box should pre-wrap
        'computedLineHeight': 'normal',
        'overflow': {'x': 20, 'y': 6},
        'hasTableWrapper': False,
        'outerWidthPx': 300,
        'runFontSizes': [19],
    }]
    plan = rt.build_repair_plan(plans, measured, fonts_missing=['PingFang SC'])
    rules = [a['rule'] for a in plan['actions']]
    check('R-WS-1 suggested', 'R-WS-1' in rules, rules)
    check('R-WS-2 wrapper suggested', 'R-WS-2' in rules, rules)
    check('R-LH-1 suggested', 'R-LH-1' in rules, rules)
    check('R-WS-1 suggests pre-wrap',
          any(a['rule'] == 'R-WS-1' and a['suggestedCss'] == 'white-space:pre-wrap;'
              for a in plan['actions']), plan['actions'])
    check('missing font blocked/requireExternalData',
          plan['blocked'] and plan['blocked'][0]['rule'] == 'R-FONT-1', plan['blocked'])
    check('zero font-size rewrites', plan['summary']['fontSizeRewrites'] == 0,
          plan['summary'])


def test_apply_css_refuses_fontsize():
    print('[12] apply_css_decl merges; refuses font-size')
    css = 'white-space:pre;font-size:19px;line-height:normal;'
    out = rt.apply_css_decl(css, 'white-space:pre-wrap;line-height:24px;')
    check('white-space replaced', 'white-space:pre-wrap;' in out, out)
    check('line-height replaced', 'line-height:24px;' in out, out)
    check('font-size untouched', 'font-size:19px;' in out, out)
    try:
        rt._set_decl(css, 'font-size', '12px')
        check('font-size rewrite refused', False)
    except ValueError:
        check('font-size rewrite refused', True)


# ---------------------------------------------------------------------------
# 13. REAL-SHAPE regression (I3): fontName='mixed' string, per-segment dict.
# Mirrors offline-real frame03 node 1120:13810: node-level fontName is the
# literal string "mixed"; each segment carries its own {family,style} dict.
# ---------------------------------------------------------------------------
REAL_MIXED_NODE = {
    'nodeId': '1120:13810', 'characters': '1.为什么会晕？\n  视觉和前庭感受冲突',
    'effectiveVisible': True, 'fontName': 'mixed',
    'fontSize': 19, 'lineHeight': {'unit': 'AUTO'},
    'textAutoResize': 'NONE', 'local': {'width': 1110},
    'relativeToArtboard': {'x': 0, 'y': 0, 'width': 1110, 'height': 400},
    'segments': [
        {'characters': '1.为什么会晕？\n', 'start': 0, 'end': 9,
         'fontName': {'family': 'PingFang SC', 'style': 'Semibold'},
         'fontSize': 34.03561, 'fills': [{'type': 'SOLID', 'visible': True,
                                            'color': {'r': 1, 'g': 1, 'b': 1}}]},
        {'characters': '  ', 'start': 9, 'end': 11,
         'fontName': {'family': 'PingFang SC', 'style': 'Regular'},
         'fontSize': 9.72446, 'fills': []},
        {'characters': '视觉和前庭感受冲突', 'start': 11, 'end': 21,
         'fontName': {'family': 'Inter', 'style': 'Regular'},
         'fontSize': 19.448, 'fills': []},
    ],
}


def test_real_mixed_font_shape():
    print('[13] real shape: fontName="mixed" expands from segments, no crash')
    # enumerate_families must not treat the string as a dict.
    fams, unres = dtc.enumerate_families(REAL_MIXED_NODE)
    check('mixed expands to PingFang SC + Inter',
          fams == ['PingFang SC', 'Inter'], fams)
    check('mixed resolved via segments (not unresolved)', unres is False, unres)
    # build_text_css must compile the node without crashing.
    r = dtc.build_text_css(REAL_MIXED_NODE)
    sizes = [round(x['font_size_px'], 1) for x in r['runs']]
    check('three runs compiled', len(r['runs']) == 3, sizes)
    check('per-run sizes 34.0/9.7/19.4', sizes == [34.0, 9.7, 19.4], sizes)
    check('heading run semibold 600', r['runs'][0]['font_weight'] == 600,
          r['runs'][0])
    # content dict-style fontName still works (no regression).
    d_fams, _ = dtc.enumerate_families(
        {'fontName': {'family': 'Inter', 'style': 'Medium'}})
    check('dict fontName still resolved', d_fams == ['Inter'], d_fams)


def test_real_mixed_unresolved_when_no_runs():
    print('[14] real shape: fontName="mixed" without runs is unresolved, not guessed')
    fams, unres = dtc.enumerate_families({'fontName': 'mixed', 'characters': 'x'})
    check('mixed with no runs -> no family', fams == [], fams)
    check('mixed with no runs -> unresolved', unres is True, unres)
    # plain string family is accepted (not mistaken for mixed).
    s_fams, s_unres = dtc.enumerate_families({'fontName': 'Helvetica'})
    check('literal string family accepted', s_fams == ['Helvetica'], s_fams)
    check('literal string family resolved', s_unres is False, s_unres)


def test_multiline_box_does_not_read_boxheight_as_leading():
    print('[15] multi-line box height is NOT per-line leading (N2 single-line only)')
    # frame02 1120:13751: box 113x624 at fontSize 25, soft-wraps to ~18 lines.
    # Reading box.height/explicitLines gave line-height:624px -> 11k px overflow.
    t = {'characters': '长段落' * 20, 'textAutoResize': 'NONE',
         'local': {'width': 113},
         'relativeToArtboard': {'x': 0, 'y': 0, 'width': 113, 'height': 624},
         'fontName': {'family': 'PingFang SC', 'style': 'Regular'}, 'fontSize': 25,
         'lineHeight': {'unit': 'AUTO'}}
    r = dtc.build_text_css(t)
    check('multi-line box falls back to normal',
          _css_decl(r['css'], 'line-height') == 'normal',
          _css_decl(r['css'], 'line-height'))
    check('not a bogus 624px leading', '624' not in _css_decl(r['css'], 'line-height'),
          _css_decl(r['css'], 'line-height'))


def test_r_lh_multiline_rule():
    print('[16] R-LH-MULTILINE collapses bogus box-height leading to normal')
    plan = [dtc.build_text_css(
        {'characters': '长段落' * 20, 'textAutoResize': 'NONE',
         'local': {'width': 113},
         'relativeToArtboard': {'width': 113, 'height': 624},
         'fontName': {'family': 'PingFang SC', 'style': 'Regular'}, 'fontSize': 25,
         'lineHeight': {'unit': 'AUTO'}})]
    measured = [{
        'nodeId': plan[0]['nodeId'], 'mode': 'fixed-box',
        'computedWhiteSpace': 'pre-wrap', 'computedLineHeight': '624.000px',
        'fontSizePx': 25, 'lineCount': 18, 'explicitLines': 1,
        'overflow': {'x': 0, 'y': 10619}, 'hasTableWrapper': True,
    }]
    out = rt.build_repair_plan(plan, measured, fonts_missing=[])
    rules = [a['rule'] for a in out['actions']]
    check('R-LH-MULTILINE suggested', 'R-LH-MULTILINE' in rules, rules)
    check('R-LH-MULTILINE -> normal (no font shrink)',
          any(a['rule'] == 'R-LH-MULTILINE'
              and a['suggestedCss'] == 'line-height:normal;'
              for a in out['actions']), out['actions'])


def main():
    test_fixed_box_wraps_not_pre()
    test_auto_width_grows()
    test_explicit_newline_preserved()
    test_mixed_runs()
    test_auto_lineheight_px()
    test_auto_lineheight_unresolved_disclosed()
    test_letter_spacing()
    test_segments_alias()
    test_alignment()
    test_classification()
    test_repair_plan()
    test_apply_css_refuses_fontsize()
    test_real_mixed_font_shape()
    test_real_mixed_unresolved_when_no_runs()
    test_multiline_box_does_not_read_boxheight_as_leading()
    test_r_lh_multiline_rule()
    print('\n=== RESULT ===')
    print(f'passed: {len(PASS)}  failed: {len(FAIL)}')
    if FAIL:
        print('FAILURES:'); [print(' -', f) for f in FAIL]
        sys.exit(1)


if __name__ == '__main__':
    main()
