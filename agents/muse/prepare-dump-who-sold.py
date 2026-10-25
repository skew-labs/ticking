from pathlib import Path
import hashlib,json,re,shutil,subprocess,sys,os
R=Path("/srv/skew/tick-market-ui-20261007")
W=R/".work/dump-who-sold-20261023"
O=R/"evidence/dump-who-sold-20261023"
O.mkdir(exist_ok=True,parents=True)
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
P=R/".releases/current-public.json"
ptr=sha(P)
BASE=os.environ.get("BASE_RELEASE") or ".releases/ticking-muse-flow-answer-20261023-01"
B=R/BASE
assert B.is_dir(), BASE
M=json.loads((B/"manifest.json").read_text())
S=B/".vercel/output/static"
NODE=R/".tooling/node-v24.14.1-linux-x64/bin/node"
overlay={}
f=W/"source/src/wallet-flow-chart.js"
assert f.is_file()
subprocess.run([NODE,"--check",str(f)],check=True,capture_output=True)
overlay["src/wallet-flow-chart.js"]=sha(f)
assert M.get("src/wallet-flow-chart.js")!=overlay["src/wallet-flow-chart.js"],"chart unchanged — abort"
chart=f.read_text()
assert "paintDumpMarkers" in chart and "dumpTags" in chart and "hydrateFlowAnswers" in chart
assert "FLOW" in chart and "In / Out" in chart
REL=os.environ.get("REL","01")
D=R/(".releases/ticking-dump-who-sold-20261023-"+REL)
if D.exists():
  shutil.rmtree(D)
D.mkdir(exist_ok=False)
I=D/".inputs"; T=D/".vercel/output/static"
for n,dig in M.items():
  if n.startswith("_app/"): continue
  assert sha(S/n)==dig,n
  q=I/n; q.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(S/n,q)
for n in overlay:
  shutil.copy2(W/"source"/n, I/n)
(D/".layer-inheritance.json").write_text(json.dumps({"base":BASE,"overlay":overlay,"mode":"dump-who-sold"},indent=2))
p=subprocess.run([sys.executable,str(R/"qa/apply-required-console-layers.py"),str(D)],capture_output=True,text=True)
(O/"console-layers.log").write_text(p.stdout+p.stderr)
assert p.returncode==0, (p.stderr[-2000:] or p.stdout[-2000:])
layers=json.loads(p.stdout)
for n,dig in overlay.items():
  assert sha(I/n)==dig,("layer overwrote",n)
html=(I/"index.html").read_text()
html,k=re.subn(
  r'<script type="module" src="(?:\./src/app\.js|/_app/app-[A-Z0-9]+\.js)"></script>',
  '<script type="module" src="./src/app.js"></script>',
  html,count=1)
assert k==1
(I/"index.html").write_text(html)
shutil.copytree(I,T)
s=(B/".products-build.mjs").read_text()
s,k=re.subn(
  r'const root="/srv/skew/tick-market-ui-20261007/\.releases/[^"\n]+/\.inputs";',
  'const root='+json.dumps(str(I))+';',
  s,count=1)
assert k==1, "products-build root rewrite failed"
runner=D/".products-build.mjs"; runner.write_text(s)
subprocess.run([NODE,str(runner),str(D)],check=True)
for n,dig in M.items():
  if n.startswith("_app/"):
    q=T/n; q.parent.mkdir(parents=True,exist_ok=True)
    if not q.exists(): shutil.copy2(S/n,q)
shutil.copy2(B/".vercel/output/config.json", D/".vercel/output/config.json")
new={str(x.relative_to(T)):sha(x) for x in T.rglob("*") if x.is_file()}
for n,dig in M.items():
  if not n.startswith("_app/") and n not in set(overlay)|{"index.html"}:
    assert new.get(n)==dig,n
assert sha(P)==ptr
(D/"manifest.json").write_text(json.dumps(new,indent=2))
idx=(T/"index.html").read_text()
assert re.search(r'/_app/app-[A-Z0-9]+\.js', idx)
assert "home-critical-" in idx or any("home-critical" in x for x in new)
assert "./src/app.js" not in idx
chunk_hit=False
for c in sorted(new):
  if not c.startswith("_app/"): continue
  if new.get(c)==M.get(c): continue
  txt=(T/c).read_text(errors="ignore")
  if "paintDumpMarkers" in txt or "data-wf-dumps" in txt or "Dump tags" in txt:
    chunk_hit=True
    break
assert chunk_hit, "dump marker strings missing from changed _app bundles"
meta={
  "passed":True,
  "release":str(D.relative_to(R)),
  "base":BASE,
  "rollbackDeployment":"dpl_4GJPmXyEn9hyrUa8itr4XEm8RcJM",
  "currentPublicSHA256AtBuild":ptr,
  "overlay":overlay,
  "changes":sorted(n for n,d in new.items() if M.get(n)!=d),
  "layers":layers,
  "published":False,
  "billingChanged":False,
  "backendChanged":True,
  "backendNote":"em-holdings-proxy dump_tags.py + asset_flow/wallet_flow markers (Arb TT)",
  "includes":[
    "Flow chart dump/crash interval tags with who sold (markers/dumpTags)",
    "paintDumpMarkers on wallet-flow-chart.js time axis",
    "API markers from real Arb TT only; honest empty if unknown",
  ],
  "gates":{"hashedApp":True,"homeCritical":True,"noThinSrcAppJs":True,"dumpMarkersInBundle":True},
}
(D/"build-meta.json").write_text(json.dumps(meta,indent=2))
(O/"frontend-build.json").write_text(json.dumps(meta,indent=2))
print(json.dumps({
  "passed":True,
  "release":meta["release"],
  "changes":len(meta["changes"]),
  "overlay":sorted(overlay),
  "app":[c for c in meta["changes"] if c.startswith("_app/app-")][:5],
  "idxApp":re.search(r"/_app/app-[A-Z0-9]+\.js",idx).group(0),
},indent=2))
