"""Fail-closed public-tree boundary checks. Prints locations, never secret values."""
from pathlib import Path
import ast,hashlib,json,re,sys
root=Path(__file__).resolve().parents[1]
ignored={'.git','.build','.venv','.state','__pycache__','.pytest_cache','node_modules','vendor'}
allowed={'.md','.py','.cpp','.hpp','.js','.mjs','.ts','.css','.html','.json','.txt','.sh','.yml','.yaml','.lock'}
patterns={
 'private-key':rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
 'provider-token':rb'ory_at_[A-Za-z0-9_-]+',
 'github-token':rb'gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}',
 'cloud-key':rb'AKIA[0-9A-Z]{16}',
 'private-path':rb'/Users/heoun|/srv/skew|45\.94\.80\.106|148\.113\.153\.116',
 'embedded-credential':rb'https?://[^\s/]+:[^\s/@]+@',
}
errors=[];records={}
for file in sorted(root.rglob('*')):
    rel=file.relative_to(root)
    if any(x in ignored for x in rel.parts):continue
    if file.is_symlink():errors.append((str(rel),'symlink'));continue
    if not file.is_file():continue
    if file.suffix in {'.so','.o','.a'} or str(rel)=='engine/engine/slotline-observer':continue
    if file.name.startswith('._') or file.suffix in {'.sqlite','.db','.pem','.key','.zip','.gz','.zst'} or file.name.startswith('.env') and file.name!='.env.example':errors.append((str(rel),'private/artifact file'))
    if file.suffix not in allowed and file.name!='.gitignore':errors.append((str(rel),'unallowlisted type'))
    raw=file.read_bytes()
    if len(raw)>2*1024*1024:errors.append((str(rel),'oversize source'))
    if file.name!='publication-check.py':
        for name,pattern in patterns.items():
            if re.search(pattern,raw):errors.append((str(rel),name))
    if file.suffix=='.py':
        try:ast.parse(raw.decode())
        except SyntaxError:errors.append((str(rel),'python syntax'))
    if file.suffix in {'.js','.mjs'}:
        s=raw.decode()
        for name in re.findall(r'''(?:from\s*|import\s*\(|import\s*)['"](\.[^'"]+)['"]''',s):
            if not (file.parent/name).exists():errors.append((str(rel),'missing module '+name))
    if file.name not in {'source-manifest.json','verification.json'}:records[str(rel)]=hashlib.sha256(raw).hexdigest()
collector=(root/'engine/engine/hyperliquid_public.cpp').read_text()
if 'candleSnapshot' in collector or 'priority_history(' in collector:errors.append(('engine/engine/hyperliquid_public.cpp','excluded history downloader'))
for absent in ['terminal/src/app.js','terminal/src/console.js','terminal/src/data.js','api/server/news.py']:
    if (root/absent).exists():errors.append((absent,'excluded frontend/data'))
if errors:
    print(json.dumps({'passed':False,'findings':errors},indent=2));sys.exit(1)
print(json.dumps({'passed':True,'sourceFiles':len(records),'sourceBytes':sum((root/n).stat().st_size for n in records)}))
if '--manifest' in sys.argv:(root/'source-manifest.json').write_text(json.dumps(records,indent=2)+'\n')
