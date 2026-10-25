from pathlib import Path
import hashlib,json,re,shutil,subprocess,sys,os
R=Path("/srv/skew/tick-market-ui-20261007")
W=R/".work/muse-flow-answer-20261023"
O=R/"evidence/muse-flow-answer-20261023"
O.mkdir(exist_ok=True,parents=True)
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
P=R/".releases/current-public.json"
ptr=sha(P)
# Base on LIVE nopixel (dpl_8m4h…) even if current-public pointer lags
BASE=os.environ.get("BASE_RELEASE") or ".releases/ticking-wallet-flow-nofixel-20261023-01"
B=R/BASE
assert B.is_dir(), BASE
M=json.loads((B/"manifest.json").read_text())
S=B/".vercel/output/static"
NODE=R/".tooling/node-v24.14.1-linux-x64/bin/node"
SOURCE_BASE_INVESTIGATE=os.environ.get("SOURCE_BASE_INVESTIGATE") or M["src/explorer-investigate.js"]
assert M["src/explorer-investigate.js"]==SOURCE_BASE_INVESTIGATE,"live explorer-investigate.js moved: rebase overlay first"
allowed={
  "explorer-investigate.js","explorer-investigate-ask.js","explorer-assistant.js",
  "wallet-flow-chart.js","agent-wallet-tools.js","intelligence-panel.js",
  "assistant-intents.json",
}
overlay={}
for f in (W/"source/src").iterdir():
  if not f.is_file(): continue
  n="src/"+f.name
  if M.get(n)==sha(f): continue
  assert f.name in allowed, (f.name,"not in allowed")
  if f.suffix==".js":
    subprocess.run([NODE,"--check",str(f)],check=True,capture_output=True)
  overlay[n]=sha(f)
need={"src/wallet-flow-chart.js","src/agent-wallet-tools.js","src/intelligence-panel.js","src/explorer-investigate-ask.js"}
assert need <= set(overlay)|{n for n,d in M.items() if n in need and (W/"source"/n).exists() and sha(W/"source"/n)==d}, ("missing", need-set(overlay))
# force these into overlay even if sha matches (rebuild path)
for req in need:
  f=W/"source"/req
  assert f.is_file(), req
  if req not in overlay:
    if f.suffix==".js":
      subprocess.run([NODE,"--check",str(f)],check=True,capture_output=True)
    overlay[req]=sha(f)
# gates: Flow visual + Muse hydrate
chart=(W/"source/src/wallet-flow-chart.js").read_text()
assert "hydrateFlowAnswers" in chart and "FLOW" in chart and "In / Out" in chart
tools=(W/"source/src/agent-wallet-tools.js").read_text()
assert "asset-flow" in tools and "wallet-flow" in tools and "data-wf-answer" in tools
panel=(W/"source/src/intelligence-panel.js").read_text()
assert "hydrateFlowAnswers" in panel and "'flow','asset-flow'" in panel.replace(" ","")
ask=(W/"source/src/explorer-investigate-ask.js").read_text()
assert "on Flow" in ask or "Flow" in ask
REL=os.environ.get("REL","01")
D=R/(".releases/ticking-muse-flow-answer-20261023-"+REL)
D.mkdir(exist_ok=False)
I=D/".inputs"; T=D/".vercel/output/static"
for n,dig in M.items():
  if n.startswith("_app/"): continue
  assert sha(S/n)==dig,n
  q=I/n; q.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(S/n,q)
for n in overlay:
  shutil.copy2(W/"source"/n, I/n)
(D/".layer-inheritance.json").write_text(json.dumps({"base":BASE,"overlay":overlay,"mode":"wallet-flow-proper"},indent=2))
p=subprocess.run([sys.executable,str(R/"qa/apply-required-console-layers.py"),str(D)],capture_output=True,text=True)
(O/"console-layers.log").write_text(p.stdout+p.stderr)
assert p.returncode==0, p.stderr[-2000:]
layers=json.loads(p.stdout)
for n,dig in overlay.items():
  assert sha(I/n)==dig,("layer overwrote",n)
html=(I/"index.html").read_text()
html,k=re.subn(r'<script type="module" src="(?:\./src/app\.js|/_app/app-[A-Z0-9]+\.js)"></script>','<script type="module" src="./src/app.js"></script>',html,count=1)
assert k==1
(I/"index.html").write_text(html)
shutil.copytree(I,T)
s=(B/".products-build.mjs").read_text()
s,k=re.subn(r'const root="/srv/skew/tick-market-ui-20261007/\.releases/[^"\n]+/\.inputs";','const root='+json.dumps(str(I))+';',s,count=1)
assert k==1
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
# gates
idx=(T/"index.html").read_text()
assert re.search(r'/_app/app-[A-Z0-9]+\.js', idx)
assert "home-critical-" in idx or any("home-critical" in x for x in new)
assert "./src/app.js" not in idx
meta={
  "passed":True,
  "release":str(D.relative_to(R)),
  "base":BASE,
  "currentPublicSHA256AtBuild":ptr,
  "overlay":overlay,
  "changes":sorted(n for n,d in new.items() if M.get(n)!=d),
  "layers":layers,
  "published":False,
  "billingChanged":False,
  "backendChanged":False,
  "includes":[
    "Muse/search → wallet-flow + asset-flow APIs in answer",
    "intelligence-panel allows flow|asset-flow + hydrateFlowAnswers",
    "wallet-flow-chart FLOW badge In/Out bars ≠ price candles",
    "Ask chips / map-copy → Flow phrasing",
    "search_guard FLOW_P muse-agent handoff (server)",
  ],
  "gates":{"hashedApp":True,"homeCritical":True,"noThinSrcAppJs":True},
}
(D/"build-meta.json").write_text(json.dumps(meta,indent=2))
(O/"frontend-build.json").write_text(json.dumps(meta,indent=2))
print(json.dumps({"passed":True,"release":meta["release"],"changes":len(meta["changes"]),"overlay":sorted(overlay),"app":[c for c in meta["changes"] if c.startswith("_app/app-")]},indent=2))
