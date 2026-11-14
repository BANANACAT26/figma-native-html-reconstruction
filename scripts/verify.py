#!/usr/bin/env python3
"""Run canonical capture + pixel diff and bind all outputs with SHA-256.

Hash contract (no self-reference paradox):
  * run.json binds ONLY external artifacts: html, reference, browser.png and
    metrics.json. Every recorded digest can be independently re-hashed from
    the files on disk.
  * run.json NEVER embeds its own digest. The digest of run.json itself is
    written to a SIDEcar file run.json.sha256, so the final manifest stays
    independently signable:  sha256(run.json) == contents(run.json.sha256).
  * hash_inputs / self_digest_file are declared in the manifest so a
    verifier knows exactly which files were hashed and where the manifest's
    own digest lives.
CLI is unchanged from previous versions.
"""
import argparse, datetime, hashlib, json, pathlib, subprocess, sys
from PIL import Image

MANIFEST_NAME = "run.json"
MANIFEST_DIGEST_NAME = "run.json.sha256"

def sha(p):
    h=hashlib.sha256()
    with open(p,'rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def run(cmd):
    print('+',' '.join(map(str,cmd)),flush=True)
    subprocess.run([str(x) for x in cmd],check=True)

def build_manifest(html, reference, out, threshold, background, m):
    """Assemble the run manifest. All digests bind external files only;
    the manifest itself carries no self digest (write it to the sidecar)."""
    out = pathlib.Path(out)
    return {'timestamp':datetime.datetime.now(datetime.timezone.utc).isoformat(),
       'html':str(pathlib.Path(html).resolve()),'reference':str(pathlib.Path(reference).resolve()),
       'out':str(out.resolve()),'viewport':list(Image.open(reference).size),
       'device_scale_factor':1,'threshold_rgb':threshold,'background':background,
       'html_sha256':sha(html),'reference_sha256':sha(reference),
       'browser_sha256':sha(out/'browser.png'),'metrics_sha256':sha(out/'metrics.json'),
       'raw':m.get('raw'),'outside_text':m.get('outside_text'),'text_region':m.get('text_region'),
       'hash_inputs':['html','reference','browser.png','metrics.json'],
       'self_digest_file':MANIFEST_DIGEST_NAME,'self_reference':False}

def write_manifest(manifest, out):
    """Write run.json, then write its own digest to the sidecar file.
    Returns the manifest digest. Never embeds the digest inside run.json."""
    out = pathlib.Path(out)
    (out/MANIFEST_NAME).write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    digest = sha(out/MANIFEST_NAME)
    (out/MANIFEST_DIGEST_NAME).write_text(digest+'\n',encoding='utf-8')
    return digest

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--html',required=True);ap.add_argument('--reference',required=True)
    ap.add_argument('--out',required=True);ap.add_argument('--threshold',type=float,default=20)
    ap.add_argument('--background',default='000000');ap.add_argument('--stage-selector',default='#stage')
    ap.add_argument('--chrome-channel',default='chrome');args=ap.parse_args()
    here=pathlib.Path(__file__).resolve().parent;out=pathlib.Path(args.out);out.mkdir(parents=True,exist_ok=True)
    run([sys.executable,here/'capture.py','--html',args.html,'--reference',args.reference,'--out',out,
         '--stage-selector',args.stage_selector,'--chrome-channel',args.chrome_channel])
    run([sys.executable,here/'pixel_diff.py','--browser',out/'browser.png','--reference',args.reference,
         '--outdir',out,'--text-mask',out/'text-mask.png','--threshold',args.threshold,'--background',args.background])
    metrics=out/'metrics.json';m=json.loads(metrics.read_text(encoding='utf-8'))
    d=build_manifest(args.html,args.reference,out,args.threshold,args.background,m)
    digest=write_manifest(d,out)
    d['manifest_sha256']=digest  # display-only echo; the durable record is the sidecar
    print(json.dumps(d,ensure_ascii=False))
if __name__=='__main__':main()
