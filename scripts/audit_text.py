#!/usr/bin/env python3
"""Audit real DOM text line boxes against the design text manifest.

This is gate D (text) of the deterministic compiler. It does NOT silently
pass a wrap/overflowing build:

  * per TEXT node it measures every line box from ``Range.getClientRects()``,
    the line boxes' inset against the parent box on all four sides, line count,
    measured line-height / baseline, and scroll/client overflow;
  * it reads the *computed* ``white-space`` / ``font-family`` / ``line-height``
    (not the authored inline string);
  * it waits for ``document.fonts.ready`` and then calls
    ``document.fonts.check('16px "<family>"')`` for every family the manifest
    requests; a family that does not actually load is reported as
    ``fontsMissing`` and disclosed as unresolved -- never silently passed as a
    fallback;
  * it distinguishes explicit hard breaks (``\\n``/U+2028 in the design),
    expected soft wraps (fixed-box pre-wrap wrapping at the box width), and
    *added* wraps (a content-mode node that wrapped where it must not).

Hard failures (gate D fail): horizontal/vertical overflow beyond the 2px
tolerance, added wraps, missing fonts, color suspects, missing-in-DOM, errors.
Gradient / image / no-fill visibility remains *unknown* (not pass/fail).
"""
import argparse, json, pathlib, re, sys, urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import design_text as dt  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

JS = r"""(design) => {
 const stage=document.getElementById('stage');
 const stageBg=stage?getComputedStyle(stage).backgroundColor:'rgba(0,0,0,0)';
 function lum(rgb){const m=String(rgb).match(/\d+(\.\d+)?/g);if(!m)return 0;
   const [r,g,b]=[+m[0],+m[1],+m[2]].map(v=>{v/=255;return v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4);});
   return 0.2126*r+0.7152*g+0.0722*b;}
 const bgLum=lum(stageBg);
 const els=[...document.querySelectorAll('[data-n]')].filter(e=>{
   const w=document.createTreeWalker(e,NodeFilter.SHOW_TEXT);let n,has=false;
   while((n=w.nextNode())){if((n.textContent||'').trim()){has=true;break}}
   return has && ![...e.children].some(c=>c.hasAttribute&&c.hasAttribute('data-n')&&(c.innerText||'').trim());
 });
 function hasDisplayTable(el){
   let n=el;
   while(n){const d=getComputedStyle(n).display;if(d==='table'||d==='inline-table')return true;n=n.parentElement;}
   return false;
 }
 const nodes=els.map((el,i)=>{
   const nid=el.getAttribute('data-n')||`dom-${i}`,r=el.getBoundingClientRect();
   let textEl=el;
   const tw=document.createTreeWalker(el,NodeFilter.SHOW_TEXT);
   let tn;while((tn=tw.nextNode())){if((tn.textContent||'').trim()){textEl=tn.parentElement||el;break}}
   const cs=getComputedStyle(textEl);
   // Measure TEXT NODE ranges, not selectNodeContents(el): the latter includes
   // the outer block box and can merge several visual lines into one tall rect.
   const raw=[];
   const lineWalker=document.createTreeWalker(el,NodeFilter.SHOW_TEXT);
   let lineNode;
   while((lineNode=lineWalker.nextNode())){
     if(!lineNode.textContent)continue;
     const lr=document.createRange();lr.selectNodeContents(lineNode);
     for(const q of lr.getClientRects()){
       if(q.width>.5&&q.height>.5)raw.push({x:q.x,y:q.y,w:q.width,h:q.height});
     }
   }
   raw.sort((a,b)=>a.y-b.y||a.x-b.x);const lines=[];
   // Rects on the SAME visual line overlap vertically; stacked lines only touch.
   for(const q of raw){const last=lines[lines.length-1];
     if(last){
       const top=Math.max(q.y,last.y),bottom=Math.min(q.y+q.h,last.y+last.h);
       const overlap=bottom-top;
       if(overlap>Math.min(q.h,last.h)*0.5){
         const nx=Math.min(last.x,q.x),ny=Math.min(last.y,q.y);
         const nr=Math.max(last.x+last.w,q.x+q.w),nb=Math.max(last.y+last.h,q.y+q.h);
         last.x=nx;last.y=ny;last.w=nr-nx;last.h=nb-ny;continue;
       }
     }
     lines.push({x:q.x,y:q.y,w:q.w,h:q.h});}
   // average measured line height + baseline offset
   let lhSum=0;for(const l of lines)lhSum+=l.h;
   const avgLineH=lines.length?lhSum/lines.length:0;
   // Per-run spans: compare EACH span's computed font style to its expected run
   // style, so mixed nodes are judged run-by-run, not by the container.
   const runSpans=[...el.querySelectorAll('span[data-run-index]')].map(s=>({
     index:parseInt(s.getAttribute('data-run-index'),10),
     text:(s.textContent||''),
     fontSize:getComputedStyle(s).fontSize,
     fontWeight:getComputedStyle(s).fontWeight,
     fontStyle:getComputedStyle(s).fontStyle,
     fontFamily:getComputedStyle(s).fontFamily
   }));
   return {nid,text:(el.innerText||'').trim(),
     box:{x:r.x,y:r.y,w:r.width,h:r.height},
     lines,lineCount:lines.length,
     observedLineTexts:[...el.querySelectorAll('.cal-line')].map(s=>s.textContent||''),
     avgLineHeight:avgLineH,baselineRatio:0.8,
     overflow:{x:Math.max(0,el.scrollWidth-el.clientWidth),y:Math.max(0,el.scrollHeight-el.clientHeight)},
     computed:{whiteSpace:cs.whiteSpace,fontFamily:cs.fontFamily,
       lineHeight:cs.lineHeight,fontSize:cs.fontSize,
       textAlign:cs.textAlign,verticalAlign:cs.verticalAlign},
     runSpans,
     hasTableWrapper:hasDisplayTable(textEl),
     color:cs.color,colorLuminance:lum(cs.color),
     backgroundClipText:(cs.webkitBackgroundClip==='text'||cs.backgroundClip==='text')};
 });
 // ---- fill-mode classifier (from design manifest, not CSS color) ----
 function fillMode(d){
   if(!d)return 'unknown';
   let f=d.fills;
   if(typeof f==='string'||!Array.isArray(f))return 'unknown';
   const vis=f.filter(x=>x&&x.visible!==false);
   if(!vis.length)return 'none';
   const kinds=new Set(vis.map(x=>x.type));
   if(kinds.has('IMAGE'))return 'image';
   if(kinds.has('GRADIENT_LINEAR')||kinds.has('GRADIENT_RADIAL')||kinds.has('GRADIENT_ANGULAR'))return 'gradient';
   if(kinds.has('SOLID'))return 'solid';
   return 'other';
 }
 const colorAudit={stageBackground:stageBg,stageLuminance:+(bgLum).toFixed(3),suspects:[],unknowns:[]};
 for(const n of nodes){
   const d=design[n.nid];const mode=fillMode(d);
   if(mode==='gradient'){
     if(!n.backgroundClipText)colorAudit.suspects.push({node:n.nid,text:n.text.slice(0,40),mode,
       reason:'design says gradient fill but DOM has no background-clip:text'});
     continue;}
   if(mode==='image'){colorAudit.unknowns.push({node:n.nid,text:n.text.slice(0,40),mode,
       reason:'image-filled text; cannot judge rendered color without reference pixels'});continue;}
   if(mode==='none'){colorAudit.unknowns.push({node:n.nid,text:n.text.slice(0,40),mode,
       reason:'no visible fills; rendered transparent; unknown without reference'});continue;}
   if(mode==='solid'){
     if(n.colorLuminance<0.12 && bgLum<0.12)colorAudit.suspects.push({node:n.nid,text:n.text.slice(0,40),mode,
       color:n.color,colorLuminance:+(n.colorLuminance).toFixed(3),stageLuminance:+(bgLum).toFixed(3),
       reason:'dark solid text on dark stage background'});
     continue;}
   if(d && typeof d.fills==='string')colorAudit.unknowns.push({node:n.nid,text:n.text.slice(0,40),mode,
     reason:`fills value is ${JSON.stringify(d.fills)}; mixed per-run fills unresolved`});
   else colorAudit.unknowns.push({node:n.nid,text:n.text.slice(0,40),mode,
     reason:'no design manifest entry; cannot judge fill mode'});
 }
 // ---- coverage ----
 const domIds=new Set(nodes.map(n=>n.nid));
 const missingInDom=[],extraInDom=[];
 for(const nid of Object.keys(design)){const d=design[nid];
   if(d.effectiveVisible===false)continue;
   if(!domIds.has(nid))missingInDom.push({node:nid,expected:(d.characters||'').slice(0,40)});}
 for(const n of nodes){if(!design[n.nid])extraInDom.push({node:n.nid,text:n.text.slice(0,40)});}
 return {nodes,colorAudit,coverage:{missingInDom,extraInDom},
   errors:nodes.filter(n=>!n.text).map(n=>({node:n.nid,error:'empty-visible-text-mapping'}))};
}"""


def _line_insets(box, lines):
    """Each line box's inset against the parent box on all four sides."""
    out = []
    for ln in lines:
        out.append({
            'left': round(ln['x'] - box['x'], 2),
            'top': round(ln['y'] - box['y'], 2),
            'right': round((box['x'] + box['w']) - (ln['x'] + ln['w']), 2),
            'bottom': round((box['y'] + box['h']) - (ln['y'] + ln['h']), 2),
            'w': round(ln['w'], 2), 'h': round(ln['h'], 2),
        })
    return out


def _px(v):
    m = re.match(r'^\s*([0-9]+(?:\.[0-9]+)?)\s*px\s*$', str(v or ''))
    return float(m.group(1)) if m else None


def post_process(design, result, fonts_missing, unresolved_font_nodes):
    """Pure post-process: classify line boxes, run fontSize/color gates, and
    aggregate hard failures against the design manifest. No browser access, so
    it is directly unit-testable with synthetic DOM measurements.

    `design`: {nodeId: textManifestEntry}; `result`: the raw JS-evaluated dict
    (must carry nodes[], overflow, computed, colorAudit, coverage, errors).
    Mutates and returns `result` (adds lineAudit, hardFailures, hardFailureCount,
    unresolved, fonts, coverage.textManifestProvided).
    """
    line_audit = []
    fontSize_mismatch = []
    fontStyle_mismatch = []
    linePlan_mismatch = []
    transparent_but_solid = []
    for n in result['nodes']:
        d = design.get(n['nid'])
        entry = {
            'node': n['nid'], 'lineCount': n['lineCount'],
            'avgLineHeight': round(n['avgLineHeight'], 2),
            'overflow': n['overflow'],
            'computed': n['computed'],
            'hasTableWrapper': n['hasTableWrapper'],
            'lineInsets': _line_insets(n['box'], n['lines']),
        }
        if d:
            layout = dt.decide_text_layout(d)
            cls = dt.classify_lines(d.get('characters', ''), n['lineCount'],
                                    layout['mode'], n['overflow'])
            entry['mode'] = layout['mode']
            entry['expectedWhiteSpace'] = layout['whiteSpace']
            entry['lineBreakClassification'] = cls
            lp = d.get('linePlan')
            if isinstance(lp, dict):
                expected_lines = lp.get('lines') or []
                observed_lines = n.get('observedLineTexts') or []
                lp_ok = (observed_lines == expected_lines and
                         n['lineCount'] == len(expected_lines))
                entry['linePlanAudit'] = {
                    'expectedLines': len(expected_lines),
                    'observedLines': len(observed_lines),
                    'measuredLineBoxes': n['lineCount'], 'pass': lp_ok}
                if not lp_ok:
                    linePlan_mismatch.append({
                        'node': n['nid'], 'expected': expected_lines,
                        'observed': observed_lines,
                        'measuredLineBoxes': n['lineCount']})
            try:
                norm = dt.normalize_runs(d)
                exp_runs = [r for r in norm['runs']
                            if isinstance(r.get('font_size_px'), (int, float))]
                expected_sizes = sorted({r['font_size_px'] for r in exp_runs})
                dom_spans = n.get('runSpans') or []
                if dom_spans:
                    # Run-by-run comparison by data-run-index: a mixed node is
                    # correct only when EVERY span matches its own expected size.
                    by_idx = {s.get('index'): s for s in dom_spans}
                    worst = 0.0
                    for i, er in enumerate(exp_runs):
                        span = by_idx.get(i)
                        span_fs = _px(span.get('fontSize')) if span else None
                        if span and span.get('fontWeight') is not None:
                            try:
                                actual_weight = int(str(span['fontWeight']).strip())
                                if actual_weight != er['font_weight']:
                                    fontStyle_mismatch.append({
                                        'node': n['nid'], 'runIndex': i,
                                        'property': 'font-weight',
                                        'expected': er['font_weight'],
                                        'actual': actual_weight})
                            except (TypeError, ValueError):
                                pass
                        if (span and span.get('fontStyle') is not None and
                                str(span['fontStyle']).strip() != er['font_style']):
                            fontStyle_mismatch.append({
                                'node': n['nid'], 'runIndex': i,
                                'property': 'font-style',
                                'expected': er['font_style'],
                                'actual': span['fontStyle']})
                        entry.setdefault('expectedRunFontSizes',
                                         [r['font_size_px'] for r in exp_runs])
                        entry['domRunFontSizes'] = [
                            _px(s.get('fontSize')) for s in
                            (by_idx.get(i) for i in range(len(exp_runs))) if s]
                        if span_fs is None:
                            # A separator-only run is compiled into the block
                            # boundary itself and legitimately has no text span.
                            separator_only = not re.sub(
                                r'[\n\u2028\u2029]', '', er.get('characters') or '')
                            if not separator_only:
                                worst = max(worst, 999.0)
                        else:
                            worst = max(worst, abs(span_fs - er['font_size_px']))
                    if worst > 2.0:
                        fontSize_mismatch.append({
                            'node': n['nid'],
                            'expectedRunFontSizes': [r['font_size_px'] for r in exp_runs],
                            'domRunFontSizes': [
                                _px((by_idx.get(i) or {}).get('fontSize'))
                                for i in range(len(exp_runs))],
                            'deltaPx': round(worst, 2)})
                else:
                    # Single-run / no per-run spans: compare container computed size.
                    dom_fs = _px(n['computed'].get('fontSize'))
                    if expected_sizes and dom_fs is not None:
                        worst = max(abs(dom_fs - e) for e in expected_sizes)
                        entry['expectedFontSizes'] = expected_sizes
                        entry['domFontSizePx'] = dom_fs
                        if worst > 2.0:
                            fontSize_mismatch.append({
                                'node': n['nid'], 'expected': expected_sizes,
                                'dom': dom_fs, 'deltaPx': round(worst, 2)})
            except Exception as e:
                fontSize_mismatch.append({'node': n['nid'],
                                         'expected': None,
                                         'dom': _px(n['computed'].get('fontSize')),
                                         'reason': f'unknown fontSize: {e}'})
            dom_color = str(n.get('color', ''))
            if 'rgba(0, 0, 0, 0)' in dom_color or dom_color == 'transparent':
                fills = d.get('fills')
                runs = d.get('segments') or d.get('runs') or []
                solid_visible = False
                for f in (fills or []):
                    if isinstance(f, dict) and f.get('type') == 'SOLID' and f.get('visible', True):
                        solid_visible = True
                for r in runs:
                    for f in (r.get('fills') or []):
                        if isinstance(f, dict) and f.get('type') == 'SOLID' and f.get('visible', True):
                            solid_visible = True
                if solid_visible:
                    transparent_but_solid.append({
                        'node': n['nid'], 'domColor': dom_color,
                        'reason': 'design has visible SOLID fill but DOM color is transparent'})
        line_audit.append(entry)

    result['lineAudit'] = line_audit
    result['fonts'] = {'verified': [f for f in result.get('fonts', {}).get('verified', [])],
                       'missing': fonts_missing}
    result['coverage']['textManifestProvided'] = bool(design)

    overflow_nodes = [e for e in line_audit
                     if e['overflow']['x'] > 2 or
                     (e['overflow']['y'] > 2 and e.get('mode') != 'fixed-box')]
    fixed_box_overflow = [e for e in line_audit
                          if e.get('mode') == 'fixed-box' and e['overflow']['y'] > 2]
    added_wrap = [e for e in line_audit
                  if (e.get('lineBreakClassification') or {}).get('addedLines', 0) > 0]
    suspects = result['colorAudit']['suspects']
    missing = result['coverage']['missingInDom']
    errors = result['errors']

    result['hardFailures'] = {
        'overflow': [{'node': e['node'], **e['overflow']} for e in overflow_nodes],
        'addedWrap': [{'node': e['node'],
                       **e['lineBreakClassification']} for e in added_wrap],
        'fontsMissing': fonts_missing,
        'colorSuspects': suspects,
        'fontSizeMismatch': fontSize_mismatch,
        'fontStyleMismatch': fontStyle_mismatch,
        'linePlanMismatch': linePlan_mismatch,
        'transparentButSolid': transparent_but_solid,
        'missingInDom': missing,
        'errors': errors,
    }
    result['hardFailureCount'] = (len(overflow_nodes) + len(added_wrap)
                                  + len(fonts_missing) + len(suspects)
                                  + len(fontSize_mismatch) + len(fontStyle_mismatch)
                                  + len(linePlan_mismatch) + len(transparent_but_solid)
                                  + len(missing) + len(errors))
    result['designBoxOverflow'] = [
        {'node': e['node'], **e['overflow'], 'classification':
         'fixed-box vertical overflow; visibility governed by ancestor clip'}
        for e in fixed_box_overflow]
    result['unresolved'] = {
        'fontsMissing': fonts_missing,
        'fontNameUnresolved': unresolved_font_nodes,
        'colorUnknowns': result['colorAudit'].get('unknowns', []),
    }
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--html', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--text-manifest')
    ap.add_argument('--layout-plan', help='optional compiled plan carrying calibrated linePlan')
    ap.add_argument('--chrome-channel', default='chrome')
    ap.add_argument('--strict', action='store_true',
                    help='non-zero exit on any gate-D hard failure')
    args = ap.parse_args()

    design = {}
    if args.text_manifest:
        data = json.loads(pathlib.Path(args.text_manifest).read_text(encoding='utf-8'))
        design = {t['nodeId']: t for t in data.get('texts', [])}
    if args.layout_plan:
        plan = json.loads(pathlib.Path(args.layout_plan).read_text(encoding='utf-8'))
        for tp in plan.get('textPlans', []):
            if tp.get('nodeId') in design and tp.get('linePlan'):
                design[tp['nodeId']]['linePlan'] = tp['linePlan']

    with sync_playwright() as p:
        b = p.chromium.launch(channel=args.chrome_channel, headless=True,
                              args=['--force-device-scale-factor=1', '--hide-scrollbars'])
        page = b.new_page(viewport={'width': 1600, 'height': 1000}, device_scale_factor=1)
        page.goto('file://' + urllib.parse.quote(str(pathlib.Path(args.html).resolve())),
                  wait_until='networkidle', timeout=60000)
        # M5: wait for font loading before checking.
        page.evaluate('() => document.fonts && document.fonts.ready')
        # Collect distinct requested families and actually check them.
        # I3: fontName may be a dict, a literal family string, or "mixed" --
        # expand mixed from runs deterministically; unresolvable -> unresolved.
        family_set = set()
        unresolved_font_nodes = []
        for nid, t in design.items():
            fams, unres = dt.enumerate_families(t)
            family_set.update(fams)
            if unres:
                unresolved_font_nodes.append(nid)
        families = sorted(family_set)
        font_rows = page.evaluate(
            '(fams) => fams.map(f => ({family: f, loaded: document.fonts && document.fonts.check("16px \\"" + f + "\\"")}))',
            families)
        result = page.evaluate(JS, design)
        b.close()

    fonts_verified = [r['family'] for r in font_rows if r['loaded']]
    fonts_missing = [r['family'] for r in font_rows if not r['loaded']]

    # ---- post-process: pure, testable classification + gate aggregation ----
    result['fonts'] = {'verified': fonts_verified, 'missing': fonts_missing}
    result = post_process(design, result, fonts_missing, unresolved_font_nodes)
    line_audit = result['lineAudit']
    overflow_nodes = result['hardFailures']['overflow']
    added_wrap = result['hardFailures']['addedWrap']
    suspects = result['colorAudit']['suspects']
    n_hard = result['hardFailureCount']

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')

    hf = result['hardFailures']
    print(json.dumps({'nodes': len(result['nodes']),
                      'overflow': len(overflow_nodes),
                      'addedWrap': len(added_wrap),
                      'fontsVerified': len(fonts_verified),
                      'fontsMissing': fonts_missing,
                      'colorSuspects': len(suspects),
                      'fontSizeMismatch': len(hf['fontSizeMismatch']),
                      'fontStyleMismatch': len(hf['fontStyleMismatch']),
                      'transparentButSolid': len(hf['transparentButSolid']),
                      'missingInDom': len(hf['missingInDom']),
                      'errors': len(hf['errors']),
                      'hardFailures': n_hard}, ensure_ascii=False))
    if args.strict and n_hard:
        sys.exit(2)


if __name__ == '__main__':
    main()
