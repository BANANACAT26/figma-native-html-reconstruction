#!/usr/bin/env python3
"""Default Design Compiler flow:
extract -> normalize -> analyze -> plan -> build -> verify -> repair -> verify.

This orchestrator is the single default entry point. It NEVER lays out the
page itself: the Algorithm (compile_design.py -> layout-plan.json) is the only
build basis. SDK / MCP node trees are *inputs* to the compiler; the builder
consumes layout-plan.json only. The Agent must not screenshot-trace or
hand-place elements; a PNG of the design is final visual acceptance evidence
(reference), never page content.

Default stages (each writes its own machine-readable report JSON; status is one
of pass | fail | unknown; unknown means evidence was NOT collected and is
never a silent pass):

  extract   export_artboard.py (live) or --reuse-export-dir (offline re-audit)
  plan      compile_design.py --export-dir <export>   [DEFAULT ON]
            writes design-ir.json / component-relations.json / layout-plan.json
            (+ layout-plan.json.sha256 sidecar) INTO the export dir.
  build     build_fixed_html.py --export-dir <export> --output page.html
            auto-walks layout-plan.json (plan path); legacy manifest path is
            only a unit-test fallback.
  verify 1:
    A baseline/version    reference exists, viewport locked, run manifest
    C relations (DEFAULT)  measure DOM via MEASURE_JS -> audit_relations.py
                           strict full relation graph. No hand-written
                           geometry-spec required anymore.
    B content visibility   audit_content.py --browser-audit (REAL browser
                           measurement: computed display/visibility/opacity,
                           ancestor clip/mask, stage intersection, pixels).
    D text/assets          audit_text.py: real line boxes / fonts / added
                           wraps. On hard failure: ONE deterministic
                           repair_text round (CSS declarations only, never
                           font-size shrink) then re-audit once.
    E connectors           audit_connectors.py --strict. On strict failure:
                           ONE repair_connectors round (full SVG rebuild,
                           with backup + rollback) then re-audit once.
                           unverifiable blocks; it is never averaged away.
    F canonical+fit        verify.py canonical screenshot; overview-fit real
                           evidence is required (--overview-evidence) else
                           unknown.
    G pixel regression     same DPR/viewport raw/outside-text/text-region
                           metrics + run.json + run.json.sha256 sidecar.
  verify 2: re-capture pixel evidence after any repair round.

Strict is now the DEFAULT (--no-strict relaxes it): any stage `fail` OR
`unknown` exits non-zero. A failing upstream category (content / text /
connectors / relations) is NEVER masked by a good global pixel mean.
"""
import argparse
import json
import pathlib
import re
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


# ---------------------------------------------------------------------------
# Orchestration seam (monkeypatched by offline tests; never edits modules).
# ---------------------------------------------------------------------------
def run(cmd, check=True):
    """Run a child script. Tests patch reconstruct.RUNNER to record argv."""
    print('+', ' '.join(map(str, cmd)), flush=True)
    return subprocess.run([str(x) for x in cmd], check=check)


RUNNER = run


def _load_json(p):
    p = pathlib.Path(p)
    if p.exists():
        return json.loads(p.read_text(encoding='utf-8'))
    return None


def _write_json(p, obj):
    p = pathlib.Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                 encoding='utf-8')


# ---------------------------------------------------------------------------
# C: DOM measurement -> measured.json consumed by audit_relations.py
# ---------------------------------------------------------------------------
def measure_dom(page_html, stage_selector, chrome_channel, out_json):
    """Drive chromium once, run MEASURE_JS, write measured.json.

    Pure measurement: getBoundingClientRect + computed style + Range line
    boxes, exactly the input the compiler relation algorithm understands.
    Raises on failure -> caller records stage C as unknown (evidence not
    collected), never invents numbers.
    """
    from audit_relations import MEASURE_JS
    from playwright.sync_api import sync_playwright
    page_html = str(pathlib.Path(page_html).resolve())
    with sync_playwright() as p:
        b = p.chromium.launch(channel=chrome_channel, headless=True,
                              args=['--force-device-scale-factor=1',
                                    '--hide-scrollbars'])
        pg = b.new_page(viewport={'width': 2000, 'height': 2000},
                        device_scale_factor=1)
        pg.goto('file://' + __import__('urllib.parse', fromlist=['quote']).quote(page_html),
                wait_until='networkidle', timeout=60000)
        pg.evaluate('() => document.fonts && document.fonts.ready')
        measured = pg.evaluate(MEASURE_JS)
        b.close()
    _write_json(out_json, measured)
    return measured


# ---------------------------------------------------------------------------
# D: deterministic text repair (one round). repair_text.py is a pure planner;
# we apply ONLY its machine-readable, autoApplicable setTextLayout actions to
# the built HTML inline styles. We NEVER rewrite font-size (no shrink-to-fit).
# ---------------------------------------------------------------------------
def _patch_element_style(html, node_id, prop, value):
    """Merge ``prop:value;`` into the inline style of [data-n=node_id].

    Deterministic + idempotent (delegates to repair_text.apply_css_decl).
    Refuses font-size outright.
    """
    import repair_text as rt
    if prop == 'font-size':
        raise ValueError('text repair must never rewrite font-size')
    nid_q = node_id.replace('\\', '\\\\').replace('"', '\\"')
    pat = re.compile(r'<([a-zA-Z][^>]*data-n="' + nid_q + r'"[^>]*)>')
    hit = {'n': 0}

    def sub(m):
        hit['n'] += 1
        tag = m.group(1)
        sm = re.search(r'\bstyle="([^"]*)"', tag)
        if sm:
            new_style = rt.apply_css_decl(sm.group(1), f'{prop}:{value};')
            tag = tag[:sm.start(1)] + new_style + tag[sm.end(1):]
        else:
            tag = tag + f' style="{prop}:{value};"'
        return '<' + tag + '>'

    return pat.sub(sub, html, count=1), hit['n']


def apply_text_repair(html_text, actions):
    """Apply autoApplicable setTextLayout declarations. Returns (html, applied)."""
    applied = []
    for act in actions or []:
        if act.get('kind') != 'setTextLayout' or not act.get('autoApplicable'):
            continue
        nid = act.get('nodeId')
        sug = act.get('suggestedCss') or ''
        for decl in [d.strip() for d in sug.split(';') if d.strip()]:
            if ':' not in decl:
                continue
            prop, val = decl.split(':', 1)
            prop = prop.strip()
            if not re.fullmatch(r'[a-zA-Z\-]+', prop):
                continue  # structural hint, not an inline declaration
            html_text, n = _patch_element_style(html_text, nid, prop, val.strip())
            if n:
                applied.append({'nodeId': nid, 'rule': act.get('rule'),
                                'decl': f'{prop}:{val.strip()};'})
    return html_text, applied


def build_text_repair_inputs(text_plans, text_audit):
    """Adapt audit_text lineAudit into the measured shape repair_text expects."""
    measured = []
    for e in (text_audit or {}).get('lineAudit', []) or []:
        comp = e.get('computed') or {}
        measured.append({
            'nodeId': e.get('node'),
            'mode': e.get('mode'),
            'computedWhiteSpace': comp.get('whiteSpace'),
            'computedLineHeight': comp.get('lineHeight'),
            'overflow': e.get('overflow'),
            'hasTableWrapper': e.get('hasTableWrapper'),
            'outerWidthPx': None,
            'resolvedPx': None,
        })
    return measured


# ---------------------------------------------------------------------------
# main flow
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Default Design Compiler flow: extract -> compile -> build '
                    '-> verify -> repair -> re-verify. Strict is ON by default.')
    ap.add_argument('--node-id',
                    help='live Figma artboard node id (required for live export)')
    ap.add_argument('--reference', required=True)
    ap.add_argument('--workdir', required=True)
    ap.add_argument('--title'); ap.add_argument('--scale', type=int, default=1)
    ap.add_argument('--font-css'); ap.add_argument('--background', default='000000')
    ap.add_argument('--threshold', type=float, default=20)
    ap.add_argument('--stage-selector', default='#stage')
    ap.add_argument('--chrome-channel', default='chrome')
    ap.add_argument('--connectors-spec',
                    help='JSON listing connectors (references/connectors.md). '
                         'If omitted and connectors.manifest.json reports zero '
                         'connectors, stage E records an explicit empty-list pass.')
    ap.add_argument('--geometry-spec',
                    help='LEGACY optional extra geometry evidence (audit_geometry). '
                         'Stage C default is audit_relations over the full plan graph; '
                         'this no longer gates anything by itself.')
    ap.add_argument('--reuse-export-dir',
                    help='Skip live export + build; reuse an existing export dir '
                         '(offline re-audit). compile still runs unless '
                         '--no-compile or layout-plan.json already exists.')
    ap.add_argument('--html',
                    help='When --reuse-export-dir is given, explicit page.html path.')
    ap.add_argument('--no-compile', action='store_true',
                    help='Skip compile_design.py (legacy/manual flow). Stage C '
                         'becomes unknown unless a layout-plan.json is already present.')
    ap.add_argument('--overview-evidence',
                    help='Path to a recorded overview-fit screenshot proving '
                         'stage/fit/marker/stroke visibility. Without it stage F '
                         'is unknown (DOM existence is not visual proof).')
    ap.add_argument('--no-strict', action='store_true',
                    help='Relax strict: unknown/upstream-fail stages are listed '
                         'but do NOT exit non-zero. Default is strict.')
    args = ap.parse_args(argv)
    strict = not args.no_strict

    root = pathlib.Path(args.workdir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    evidence = root / 'evidence'
    evidence.mkdir(parents=True, exist_ok=True)

    stages = {}
    repairs = {'text': None, 'connectors': None}

    # ---- extract ---------------------------------------------------------
    if args.reuse_export_dir:
        export = pathlib.Path(args.reuse_export_dir).resolve()
        page = pathlib.Path(args.html).resolve() if args.html else (root / 'page.html')
        if not page.exists():
            print('reuse mode: --html page not found: %s' % page, file=sys.stderr)
            sys.exit(4)
    else:
        if not args.node_id:
            print('--node-id is required unless --reuse-export-dir is given',
                  file=sys.stderr)
            sys.exit(4)
        export = root / 'export'
        page = root / 'page.html'
        RUNNER([sys.executable, HERE / 'export_artboard.py', args.node_id,
                '--out-root', export, '--scale', args.scale, '--label', 'artboard'])
        export = export / 'artboard'

    # ---- normalize + analyze + plan (DEFAULT) ----------------------------
    plan_path = export / 'layout-plan.json'
    compiler = {'ran': False, 'planHash': None, 'sidecar': None}
    if not args.no_compile and not plan_path.exists():
        RUNNER([sys.executable, HERE / 'compile_design.py',
                '--export-dir', export])  # writes plan INTO export dir
        compiler['ran'] = True
    plan = _load_json(plan_path)
    if plan is not None:
        compiler['planHash'] = plan.get('planHash')
        sc = export / 'layout-plan.json.sha256'
        if sc.exists():
            compiler['sidecar'] = sc.read_text(encoding='utf-8').strip().split()[0]

    # ---- P0 data gate: compile output contract must hold BEFORE build -------
    gate_evidence = evidence / 'data-gate.json'
    gate = RUNNER([sys.executable, HERE / 'data_gate.py',
                   '--export-dir', export, '--out', gate_evidence], check=False)
    compiler['dataGateExit'] = gate.returncode
    if gate.returncode != 0:
        if strict:
            raise SystemExit(
                f'data_gate failed (exit {gate.returncode}); refusing to build on '
                f'a broken compile contract. See {gate_evidence}')
        stages['G0_data'] = {'report': str(gate_evidence), 'status': 'fail',
                             'reason': 'data_gate contract violations; relaxed by --no-strict'}

    # ---- build (builder walks the plan) -----------------------------------
    # Always rebuild, including --reuse-export-dir: offline re-verify is often
    # run precisely to pick up a new --font-css or builder change. Skipping the
    # build here silently re-verified the OLD html (missing @font-face) while
    # reporting fresh evidence.
    cmd = [sys.executable, HERE / 'build_fixed_html.py',
           '--export-dir', export, '--output', page]
    if args.title:
        cmd += ['--title', args.title]
    if args.font_css:
        cmd += ['--font-css', args.font_css]
    RUNNER(cmd)

    # ---- A: baseline / version -------------------------------------------
    ref = pathlib.Path(args.reference).resolve()
    stage_a = {'report': None, 'status': 'unknown',
               'reason': 'reference not found' if not ref.exists() else None}
    if ref.exists():
        stage_a.update({'status': 'pass',
                        'reason': 'reference locked; viewport/DPR/threshold '
                                  'recorded by verify.py'})
    stages['A_baseline'] = stage_a

    # ---- canonical verify (capture + pixel diff + run manifest) -----------
    verify_cmd = [sys.executable, HERE / 'verify.py',
                  '--html', page, '--reference', ref, '--out', evidence,
                  '--background', args.background, '--threshold', args.threshold,
                  '--stage-selector', args.stage_selector,
                  '--chrome-channel', args.chrome_channel]
    vr = RUNNER(verify_cmd, check=False)
    metrics = _load_json(evidence / 'metrics.json') or {}
    run_manifest = None
    if (evidence / 'run.json').exists():
        run_manifest = {'manifest': str(evidence / 'run.json'),
                        'self_digest_file': 'run.json.sha256'}
        sidecar = evidence / 'run.json.sha256'
        if sidecar.exists():
            run_manifest['manifest_sha256'] = sidecar.read_text(
                encoding='utf-8').strip().split()[0]

    # ---- G: pixel regression ---------------------------------------------
    if vr.returncode == 0 and metrics:
        stage_g = {'report': str(evidence / 'metrics.json'), 'status': 'pass',
                   'raw': metrics.get('raw'),
                   'outside_text': metrics.get('outside_text'),
                   'text_region': metrics.get('text_region'),
                   'reason': 'same-DPR/same-viewport pixel diff recorded'}
    else:
        stage_g = {'report': str(evidence / 'metrics.json'), 'status': 'fail'
                   if vr.returncode != 0 else 'unknown',
                   'reason': 'verify.py failed or metrics.json missing; do NOT '
                             'average a global pixel mean to mask local gaps'}
    stages['G_pixel'] = stage_g

    # ---- C: relations (DEFAULT full plan graph; no geometry-spec gate) ----
    if plan is None:
        stage_c = {'report': str(export / 'component-relations.json'),
                   'status': 'unknown',
                   'reason': 'no layout-plan.json (compile skipped or not '
                             'produced); relation graph NOT machine-verified '
                             'this run'}
    else:
        measured_json = evidence / 'measured.json'
        try:
            measure_dom(page, args.stage_selector, args.chrome_channel,
                        measured_json)
        except Exception as exc:  # measurement evidence could not be collected
            stage_c = {'report': str(measured_json), 'status': 'unknown',
                       'reason': 'DOM measurement failed (%s); unknown is not a '
                                 'pass' % type(exc).__name__}
            measured_json = None
        if measured_json is not None:
            ar = RUNNER([sys.executable, HERE / 'audit_relations.py',
                         '--export-dir', export, '--measured', measured_json,
                         '--out-dir', evidence], check=False)
            relations = _load_json(evidence / 'relations-audit.json') or {}
            summ = relations.get('summary', {})
            if ar.returncode == 0 and summ.get('strictFail', 1) == 0:
                stage_c = {'report': str(evidence / 'relations-audit.json'),
                           'status': 'pass',
                           'edges': summ,
                           'planHash': relations.get('planHash'),
                           'reason': 'every planned relation pass/fail/unknown '
                                     'audited against real DOM; unknown is not '
                                     'averaged into a pass'}
            elif ar.returncode == 0:
                stage_c = {'report': str(evidence / 'relations-audit.json'),
                           'status': 'fail',
                           'edges': summ,
                           'reason': 'relation edges fail/unknown; parent-child / '
                                     'sibling / alignment / baseline / text-line '
                                     'relations are measured, not inferred from '
                                     'two good absolute boxes'}
            else:
                stage_c = {'report': str(evidence / 'relations-audit.json'),
                           'status': 'unknown',
                           'reason': 'audit_relations.py did not complete'}
        # legacy supplementary geometry evidence (never replaces relations)
        if args.geometry_spec:
            geom_report = evidence / 'geometry.json'
            gr = RUNNER([sys.executable, HERE / 'audit_geometry.py',
                         '--html', page, '--spec', args.geometry_spec,
                         '--out', geom_report,
                         '--stage-selector', args.stage_selector,
                         '--chrome-channel', args.chrome_channel], check=False)
            stage_c['geometrySupplement'] = {
                'report': str(geom_report), 'exitCode': gr.returncode}
    stages['C_relations'] = stage_c

    # ---- B: content visibility (REAL browser measurement, default) -------
    content_report = evidence / 'content-ledger.json'
    cr = RUNNER([sys.executable, HERE / 'audit_content.py',
                 '--export-dir', export, '--html', page, '--out', content_report,
                 '--browser-audit', '--strict'], check=False)
    content = _load_json(content_report) or {}
    hf = content.get('hardFailures', 0)
    ba = content.get('browserAudit') or {}
    if cr.returncode == 0 and hf == 0 and ba.get('allPass', True):
        stage_b = {'report': str(content_report), 'status': 'pass',
                   'ledgerSummary': content.get('ledgerSummary'),
                   'browserGate': ba.get('gateSummary'),
                   'reason': 'every effective-visible node rendered in real '
                             'browser computed style; skipped invisible/'
                             'transparent nodes NOT over-rendered'}
    elif cr.returncode == 0:
        stage_b = {'report': str(content_report), 'status': 'fail',
                   'hardFailures': hf,
                   'missingInHtml': content.get('missingInHtml'),
                   'missingTexts': content.get('missingTexts'),
                   'overRenderedSkipped': content.get('overRenderedSkipped'),
                   'browserGate': ba.get('gateSummary'),
                   'reason': 'content ledger / browser visibility hard failures; '
                             'must be fixed per category, never masked by pixel'}
    else:
        stage_b = {'report': str(content_report), 'status': 'unknown',
                   'reason': 'audit_content.py did not run (missing manifest?)'}
    stages['B_content'] = stage_b

    # ---- D: text (line boxes / fonts / added wraps) + ONE deterministic
    #         repair_text round, then re-audit once -------------------------
    text_report = evidence / 'text-layout.json'
    text_manifest = export / 'text-manifest.json'

    def run_audit_text(out_file):
        tcmd = [sys.executable, HERE / 'audit_text.py',
                '--html', page, '--out', out_file,
                '--chrome-channel', args.chrome_channel, '--strict']
        if text_manifest.exists():
            tcmd += ['--text-manifest', text_manifest]
        if (export / 'layout-plan.json').exists():
            tcmd += ['--layout-plan', export / 'layout-plan.json']
        return RUNNER(tcmd, check=False)

    tr = run_audit_text(text_report)
    text_res = _load_json(text_report) or {}
    n_hard = text_res.get('hardFailureCount', 0)

    if tr.returncode == 0 and n_hard > 0:
        # ONE deterministic repair round: plan only from plan+measured diff.
        import repair_text as rt
        plans = (plan or {}).get('textPlans', [])
        measured = build_text_repair_inputs(plans, text_res)
        fonts_missing = ((text_res.get('fonts') or {}).get('missing')) or []
        repair_plan = rt.build_repair_plan(plans, measured, fonts_missing)
        _write_json(evidence / 'text-repair-plan.json', repair_plan)
        backup = str(page) + '.before-text-repair'
        shutil.copyfile(page, backup)
        html_text = page.read_text(encoding='utf-8')
        html_text, applied = apply_text_repair(html_text, repair_plan.get('actions'))
        page.write_text(html_text, encoding='utf-8')
        repairs['text'] = {'rounds': 1, 'applied': applied,
                           'blocked': repair_plan.get('blocked'),
                           'unknown': repair_plan.get('unknown'),
                           'fontSizeRewrites': repair_plan.get(
                               'summary', {}).get('fontSizeRewrites', 0),
                           'backup': backup}
        # re-audit once
        text_report = evidence / 'text-layout.after-repair.json'
        tr2 = run_audit_text(text_report)
        text_res = _load_json(text_report) or {}
        n_hard = text_res.get('hardFailureCount', 0)
        repairs['text']['reAuditExit'] = tr2.returncode

    ca = text_res.get('colorAudit', {})
    if tr.returncode == 0 and n_hard == 0:
        stage_d = {'report': str(text_report), 'status': 'pass',
                   'colorUnknowns': len(ca.get('unknowns', [])),
                   'repairRound': repairs['text']['rounds'] if repairs['text'] else 0,
                   'reason': 'real line boxes / fonts / added-wrap clean'
                             + (' after one deterministic text repair round'
                                if repairs['text'] else '')}
    elif tr.returncode == 0:
        stage_d = {'report': str(text_report), 'status': 'fail',
                   'hardFailures': text_res.get('hardFailures'),
                   'repairRound': repairs['text']['rounds'] if repairs['text'] else 0,
                   'reason': 'text line boxes / fonts / added wraps still failing '
                             'after one deterministic repair round; wrap/overflow=0 '
                             'alone is NOT a visual pass'}
    elif text_res:
        # audit_text.py exits non-zero ON hard failures, so a non-zero exit with
        # a written report is a real FAIL, not "did not run". Mapping it to
        # unknown made a broken page look merely un-audited.
        stage_d = {'report': str(text_report), 'status': 'fail',
                   'hardFailures': text_res.get('hardFailureCount',
                                                text_res.get('hardFailures')),
                   'auditExit': tr.returncode,
                   'repairRound': repairs['text']['rounds'] if repairs['text'] else 0,
                   'reason': 'audit_text.py reported hard failures (non-zero exit '
                             'with a written report); see text-layout.json'}
    else:
        stage_d = {'report': str(text_report), 'status': 'unknown',
                   'reason': 'audit_text.py did not run'}
    stages['D_text'] = stage_d

    # ---- E: connectors + ONE repair_connectors fallback round -------------
    conn_report = evidence / 'connectors.json'
    cm_sidecar = export / 'connectors.manifest.json'
    cm = _load_json(cm_sidecar) or {}
    conn_count = cm.get('connectorCount', 0)
    stage_e = None
    if args.connectors_spec:
        def run_audit_conn(out_file):
            ecmd = [sys.executable, HERE / 'audit_connectors.py',
                    '--html', page, '--spec', args.connectors_spec,
                    '--out', out_file,
                    '--stage-selector', args.stage_selector,
                    '--chrome-channel', args.chrome_channel, '--strict']
            return RUNNER(ecmd, check=False)

        er = run_audit_conn(conn_report)
        cres = _load_json(conn_report) or {}
        summ = cres.get('summary', {})
        if er.returncode == 0 and summ.get('strictFail', 1) == 0:
            stage_e = {'report': str(conn_report), 'status': 'pass',
                       'tipVerified': summ.get('tipVerified'),
                       'total': summ.get('total'),
                       'reason': 'every connector SVG tip verified at target edge'}
        else:
            # ONE fallback round: full SVG rebuild with backup + rollback.
            if plan_path.exists():
                repaired = evidence / 'page.after-connectors.html'
                rp = RUNNER([sys.executable, HERE / 'repair_connectors.py',
                             '--html', page,
                             '--connector-plan', str(plan_path),
                             '--audit', conn_report,
                             '--out', repaired,
                             '--stage-selector', args.stage_selector,
                             '--chrome-channel', args.chrome_channel], check=False)
                repairs['connectors'] = {'rounds': 1, 'exitCode': rp.returncode}
                repaired_html = repaired if repaired.exists() else None
            else:
                repaired_html = None
                repairs['connectors'] = {'rounds': 0,
                                         'blocked': 'no layout-plan.json; '
                                                    'cannot rebuild connectors'}
            if repaired_html is not None:
                shutil.copyfile(repaired_html, page)  # rollback handled inside repair_connectors
                conn_report2 = evidence / 'connectors.after-repair.json'
                er2 = run_audit_conn(conn_report2)
                cres = _load_json(conn_report2) or {}
                summ = cres.get('summary', {})
                repairs['connectors']['reAuditExit'] = er2.returncode
                if er2.returncode == 0 and summ.get('strictFail', 1) == 0:
                    stage_e = {'report': str(conn_report2), 'status': 'pass',
                               'tipVerified': summ.get('tipVerified'),
                               'total': summ.get('total'),
                               'repairRound': 1,
                               'reason': 'connector strict-fail repaired once '
                                         '(full SVG rebuild, backup kept, '
                                         'rollback unless all applicable edges '
                                         'pass) and re-verified'}
            if stage_e is None:
                stage_e = {'report': str(conn_report),
                           'status': 'fail',
                           'byVerdict': summ.get('byVerdict'),
                           'strictFail': summ.get('strictFail'),
                           'repair': repairs['connectors'],
                           'reason': 'connector strict-fail persisted after one '
                                     'fallback round; unverifiable/blocked edges '
                                     'block delivery and are never averaged to a '
                                     'pass'}
    else:
        if cm_sidecar.exists() and conn_count == 0:
            stage_e = {'report': str(cm_sidecar), 'status': 'pass',
                       'connectorCount': 0,
                       'reason': 'connectors.manifest.json proves zero '
                                 'connector nodes in this export'}
        else:
            stage_e = {'report': str(cm_sidecar) if cm_sidecar.exists() else None,
                       'status': 'unknown',
                       'connectorCount': conn_count,
                       'reason': ('connectors exist in export but no '
                                  '--connectors-spec was supplied to audit them'
                                  if conn_count > 0 else
                                  'no --connectors-spec and no '
                                  'connectors.manifest.json; operator must '
                                  'explicitly confirm the connector list is '
                                  'empty')}
    stages['E_connectors'] = stage_e

    # ---- F: canonical + overview-fit REAL evidence -------------------------
    ov = pathlib.Path(args.overview_evidence).resolve() if args.overview_evidence else None
    if ov is not None and ov.exists():
        stage_f = {'report': str(evidence / 'browser.png'),
                   'overviewEvidence': str(ov), 'status': 'pass',
                   'reason': 'canonical browser.png captured AND overview-fit '
                             'recorded as real evidence'}
    else:
        stage_f = {'report': str(evidence / 'browser.png'), 'status': 'unknown',
                   'reason': 'canonical browser.png captured; overview-fit real '
                             'visibility (stage/fit/marker/stroke) is NOT '
                             'recorded -- DOM existence is not visual proof; '
                             'pass --overview-evidence <path>'}
    stages['F_visual'] = stage_f

    # ---- re-verify pixel evidence after any repair round ------------------
    if repairs['text'] or repairs['connectors']:
        vr2 = RUNNER(verify_cmd, check=False)
        metrics = _load_json(evidence / 'metrics.json') or {}
        if vr2.returncode == 0 and metrics:
            stage_g = {'report': str(evidence / 'metrics.json'), 'status': 'pass',
                       'raw': metrics.get('raw'),
                       'outside_text': metrics.get('outside_text'),
                       'text_region': metrics.get('text_region'),
                       'reason': 'pixel evidence re-captured after repair round'}
            stages['G_pixel'] = stage_g

    # ---- rollup -----------------------------------------------------------
    fails = [k for k, v in stages.items() if v.get('status') == 'fail']
    unknowns = [k for k, v in stages.items() if v.get('status') == 'unknown']
    summary = {
        'ruleVersion': 'design-compiler/1.0.0',
        'pipeline': ['extract', 'normalize', 'analyze', 'plan', 'build',
                     'verify', 'repair', 'verify'],
        'status': 'candidate_and_evidence_complete',
        'page': str(page),
        'export_dir': str(export),
        'evidence': str(evidence),
        'reference': str(ref),
        'compiler': compiler,
        'builder': 'layout-plan (builder consumes plan only; SDK/MCP is input)',
        'repairs': repairs,
        'run_manifest': run_manifest,
        'stages': stages,
        'failedStages': fails,
        'unknownStages': unknowns,
        'strict': strict,
        'next': ('Inspect failed/unknown stages per category; unknown means '
                 'evidence not collected. Do NOT use global pixel mean to mask '
                 'a local missing category.'),
    }
    _write_json(root / 'summary.json', summary)
    print(json.dumps({'status': summary['status'],
                      'planHash': compiler['planHash'],
                      'failedStages': fails, 'unknownStages': unknowns,
                      'strict': strict}, ensure_ascii=False))

    if strict and (fails or unknowns):
        print('STRICT GATE: failing stages: %s; unknown stages: %s'
              % (', '.join(fails) or '-', ', '.join(unknowns) or '-'),
              file=sys.stderr)
        sys.exit(2)


if __name__ == '__main__':
    main()
