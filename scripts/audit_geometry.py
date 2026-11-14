#!/usr/bin/env python3
"""Compare live-Figma expected geometry with DOM, including relative relations.

Input JSON: {width,height,nodes:[{id,selector,expected:{x,y,width,height}}],
relations:[{source,target,source_edge,target_edge,expected_dx,expected_dy}]}
"""
import argparse,json,pathlib,sys,urllib.parse
from playwright.sync_api import sync_playwright

JS=r"""(spec)=>{
 const out={};for(const n of spec.nodes){const el=document.querySelector(n.selector);if(!el){out[n.id]={missing:true};continue}
  const r=el.getBoundingClientRect();out[n.id]={x:r.x,y:r.y,width:r.width,height:r.height,
    right:r.right,bottom:r.bottom,cx:r.x+r.width/2,cy:r.y+r.height/2};}
 // Connectors: measure the connector asset box and the actual source/target node
 // boxes. We CANNOT see arrow-tip pixels inside a flattened PNG, so the generic
 // contract is: the connector box must reach the expected source edge and the
 // expected target edge within tolerance. Missing source/target evidence =>
 // 'unverifiable', never a silent pass.
 const conns=[];
 for(const c of (spec.connectors||[])){
   const el=document.querySelector(c.selector);
   const src=c.sourceSelector?document.querySelector(c.sourceSelector):null;
   const tgt=c.targetSelector?document.querySelector(c.targetSelector):null;
   const rec={id:c.id,selector:c.selector,arrowheadAt:c.arrowheadAt||'target'};
   if(!el){rec.status='missing-asset';conns.push(rec);continue;}
   if(!src||!tgt){rec.status='unverifiable';
     rec.reason='source/target selector missing; cannot confirm tip-to-edge contact';
     conns.push(rec);continue;}
   const R=el.getBoundingClientRect(),S=src.getBoundingClientRect(),T=tgt.getBoundingClientRect();
   rec.box={x:R.x,y:R.y,w:R.width,h:R.height};
   const tol=c.tolerancePx==null?2:c.tolerancePx;
   function edgePoint(box,edge){
     if(edge==='right')return {x:box.right,y:box.y+box.height/2};
     if(edge==='left')return {x:box.left,y:box.y+box.height/2};
     if(edge==='top')return {x:box.x+box.width/2,y:box.top};
     if(edge==='bottom')return {x:box.x+box.width/2,y:box.bottom};
     return {x:box.x+box.width/2,y:box.y+box.height/2};}
   // horizontal connector: source.right -> target.left (or reverse)
   const sp=edgePoint(S,c.sourceEdge),tp=edgePoint(T,c.targetEdge);
   // gap = how far the connector box is from the expected edge point.
   function gapToEdge(box,pt){
     const gx=Math.max(box.left-pt.x,0,pt.x-box.right);
     const gy=Math.max(box.top-pt.y,0,pt.y-box.bottom);
     return Math.hypot(gx,gy);}
   rec.sourceGap=+gapToEdge(R,sp).toFixed(2);
   rec.targetGap=+gapToEdge(R,tp).toFixed(2);
   rec.tolerancePx=tol;
   if(rec.sourceGap>tol||rec.targetGap>tol){
     rec.status='edge-gap';
     rec.reason=`connector box does not reach expected edge(s): sourceGap=${rec.sourceGap} targetGap=${rec.targetGap} tol=${tol}`;
   } else {
     rec.status='pass';
   }
   conns.push(rec);
 }
 return {nodes:out,connectors:conns};}"""
def edge(r,e):
    if not r:return None
    return {'left':r['x'],'right':r['right'],'top':r['y'],'bottom':r['bottom'],'cx':r['cx'],'cy':r['cy']}[e]
def delta(a,b):return {k:round(a[k]-b[k],4) for k in ('x','y','width','height')}
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--html',required=True);ap.add_argument('--spec',required=True);ap.add_argument('--out',required=True)
    ap.add_argument('--stage-selector',default='#stage');ap.add_argument('--chrome-channel',default='chrome')
    ap.add_argument('--strict',action='store_true',
                    help='non-zero exit when any connector is edge-gap / missing-asset / unverifiable')
    args=ap.parse_args()
    spec=json.loads(pathlib.Path(args.spec).read_text(encoding='utf-8'));W,H=spec['width'],spec['height']
    with sync_playwright() as p:
        b=p.chromium.launch(channel=args.chrome_channel,headless=True,args=['--force-device-scale-factor=1','--hide-scrollbars'])
        page=b.new_page(viewport={'width':W,'height':H},device_scale_factor=1)
        page.goto('file://'+urllib.parse.quote(str(pathlib.Path(args.html).resolve())),wait_until='networkidle',timeout=60000)
        page.evaluate('() => document.fonts && document.fonts.ready')
        sr=page.locator(args.stage_selector).bounding_box();measured=page.evaluate(JS,spec);b.close()
    raw_nodes=measured.get('nodes',{})
    connectors=measured.get('connectors',[])
    nodes=[]
    for n in spec['nodes']:
        m=raw_nodes.get(n['id']);r={'id':n['id'],'dom':m}
        if m and not m.get('missing'):
            m={**m,'x':m['x']-sr['x'],'y':m['y']-sr['y'],'right':m['right']-sr['x'],'bottom':m['bottom']-sr['y'],
               'cx':m['cx']-sr['x'],'cy':m['cy']-sr['y']};r['dom']=m
            if n.get('expected'):r['delta_dom_minus_figma']=delta(m,n['expected'])
        nodes.append(r)
    byid={n['id']:n.get('dom') for n in nodes};relations=[]
    for q in spec.get('relations',[]):
        a,b=byid.get(q['source']),byid.get(q['target']);r=dict(q)
        if a and b:
            # Explicit axis avoids mixing right/left with top/bottom.
            sx=q.get('source_x_edge',q.get('source_edge'));tx=q.get('target_x_edge',q.get('target_edge'))
            sy=q.get('source_y_edge','cy');ty=q.get('target_y_edge','cy')
            dx=edge(b,tx)-edge(a,sx) if sx in ('left','right','cx') and tx in ('left','right','cx') else 0
            dy=edge(b,ty)-edge(a,sy) if sy in ('top','bottom','cy') and ty in ('top','bottom','cy') else 0
            r['observed']={'dx':dx,'dy':dy}
            if 'expected_dx' in q:r['delta_dx']=dx-q['expected_dx']
            if 'expected_dy' in q:r['delta_dy']=dy-q['expected_dy']
        else:r['missing']=True
        relations.append(r)
    result={'viewport':[W,H],'dpr':1,'stageOrigin':sr,'nodes':nodes,'relations':relations,
      'connectors':connectors,
      'connectorSummary':{'total':len(connectors),
        'pass':sum(1 for c in connectors if c.get('status')=='pass'),
        'edgeGap':sum(1 for c in connectors if c.get('status')=='edge-gap'),
        'missingAsset':sum(1 for c in connectors if c.get('status')=='missing-asset'),
        'unverifiable':sum(1 for c in connectors if c.get('status')=='unverifiable')},
      'note':'Only like-for-like boxes may be compared. Connectors are judged per-edge; '
             'missing source/target evidence is unverifiable, never averaged into a pass.'}
    out=pathlib.Path(args.out);out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'nodes':len(nodes),'relations':len(relations),'connectors':result['connectorSummary']},ensure_ascii=False))
    if args.strict:
        bad=result['connectorSummary']['edgeGap']+result['connectorSummary']['missingAsset']+result['connectorSummary']['unverifiable']
        if bad:
            sys.exit(2)
if __name__=='__main__':main()
