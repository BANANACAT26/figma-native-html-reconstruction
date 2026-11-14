#!/usr/bin/env python3
"""Capture fixed-artboard HTML at the reference's physical size, DPR=1."""
import argparse, json, os, pathlib, urllib.parse
from PIL import Image
import numpy as np
from playwright.sync_api import sync_playwright

JS = r"""(stageSelector) => {
 const stage=document.querySelector(stageSelector); const sr=stage?stage.getBoundingClientRect():null;
 const out={dpr:devicePixelRatio,innerW:innerWidth,innerH:innerHeight,
  scrollW:document.documentElement.scrollWidth,scrollH:document.documentElement.scrollHeight,
  stageRect:sr?{x:sr.x,y:sr.y,w:sr.width,h:sr.height}:null,
  fontsReady:document.fonts?document.fonts.status:'unknown',textNodes:0,rects:[]};
 const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT); let n;
 while((n=walker.nextNode())){const t=(n.textContent||'').trim(); if(!t)continue;
  const el=n.parentElement;if(!el)continue;const r=el.getBoundingClientRect();
  if(r.width<2||r.height<2)continue;out.rects.push({x:r.x,y:r.y,w:r.width,h:r.height,text:t.slice(0,80)});out.textNodes++;}
 return out;
}"""

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--html',required=True);ap.add_argument('--reference',required=True)
    ap.add_argument('--out',required=True);ap.add_argument('--stage-selector',default='#stage')
    ap.add_argument('--chrome-channel',default='chrome');ap.add_argument('--settle-ms',type=int,default=600)
    args=ap.parse_args(); html=os.path.abspath(args.html);ref=os.path.abspath(args.reference)
    out=pathlib.Path(args.out);out.mkdir(parents=True,exist_ok=True)
    W,H=Image.open(ref).size
    with sync_playwright() as p:
        browser=p.chromium.launch(channel=args.chrome_channel,headless=True,
            args=['--force-device-scale-factor=1','--hide-scrollbars','--disable-blink-features=AutomationControlled','--disable-gpu'])
        ctx=browser.new_context(viewport={'width':W,'height':H},device_scale_factor=1,is_mobile=False)
        page=ctx.new_page();page.goto('file://'+urllib.parse.quote(html),wait_until='networkidle',timeout=60000)
        page.evaluate("() => document.fonts && document.fonts.ready")
        page.wait_for_timeout(args.settle_ms);info=page.evaluate(JS,args.stage_selector)
        problems=[];sr=info.get('stageRect')
        if abs(info['dpr']-1)>0.01:problems.append(f"DPR={info['dpr']}")
        if (info['innerW'],info['innerH'])!=(W,H):problems.append('viewport mismatch')
        if info['scrollW']>W+1 or info['scrollH']>H+1:problems.append(f"overflow {info['scrollW']}x{info['scrollH']}")
        if sr:
            if max(abs(sr['x']),abs(sr['y']),abs(sr['w']-W),abs(sr['h']-H))>1:problems.append(f"stage {sr}")
        else:problems.append(f"stage not found: {args.stage_selector}")
        if problems:
            browser.close();raise SystemExit('[capture] '+'; '.join(problems))
        page.screenshot(path=str(out/'browser.png'),clip={'x':0,'y':0,'width':W,'height':H})
        browser.close()
    if Image.open(out/'browser.png').size!=(W,H):raise SystemExit('[capture] output size mismatch')
    mask=np.zeros((H,W),dtype=np.uint8)
    for r in info['rects']:
        x0=max(0,int(round(r['x']))-2);y0=max(0,int(round(r['y']))-2)
        x1=min(W,int(round(r['x']+r['w']))+2);y1=min(H,int(round(r['y']+r['h']))+2)
        mask[y0:y1,x0:x1]=255
    Image.fromarray(mask,'L').save(out/'text-mask.png')
    report={'html':html,'reference':ref,'target_px':[W,H],'browser_pixel_size':[W,H],
      'dpr':info['dpr'],'viewport':[info['innerW'],info['innerH']],
      'scroll_size':[info['scrollW'],info['scrollH']],'stage_rect':info['stageRect'],
      'fonts_status':info['fontsReady'],'text_node_count':info['textNodes'],
      'text_mask_fraction':round(float((mask>0).mean()),6),'verification':'passed'}
    (out/'capture-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'text-nodes.json').write_text(json.dumps(info['rects'],ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
if __name__=='__main__':main()
