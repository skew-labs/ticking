"""Ticking first-class Arbitrum One / Robinhood Chain release (+ wallet layer), in the style of qa/*-release.py.

Run on skew from the Ticking root (cwd must be R) unless --out is given for a dry run:
    python3 qa/em-map-release.py build   [--package /srv/skew/em-ticking-bridge/package] [--name ticking-em-map-YYYYMMDD-NN] [--out DIR]
    python3 qa/em-map-release.py verify  (hash + scope checks of the built release against the live pointer)
    python3 qa/em-map-release.py package (evidence/em-map-<date>/deployment-request.json for the Vercel API uploader, like
                                         qa/chart-flat-release.py package; unchanged files by sha1, new/changed files inline)
build: copies the CURRENT public release (.releases/current-public.json), applies the EM change set with apply_em_patch.py
(anchored, all-or-nothing), rebuilds the bundle with that release's own .products-build.mjs (new _app chunks are added; every
retained chunk stays), mirrors the changed sources into .vercel/output/static, and writes manifest.json, build-meta.json and
evidence/em-map-<date>/build.json. Nothing is deployed and current-public.json is never touched here.
"""
import argparse
import base64
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

R = Path("/srv/skew/tick-market-ui-20261007")
CHANGED_SRC, NEW_SRC = [], []
digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()


def scope(a):
    """The changed/new source set comes from the reviewed patch itself, never a hand-kept list."""
    global CHANGED_SRC, NEW_SRC
    import importlib.util
    spec = importlib.util.spec_from_file_location("apply_fe_patch", a.package + "/apply_fe_patch.py")
    fe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fe)
    CHANGED_SRC, NEW_SRC = sorted(fe.PATCHES), ["src/" + m for m in fe.MODULES]


def build(a):
    scope(a)
    pointer = json.loads((R / ".releases/current-public.json").read_text())
    base = R / pointer["release"]
    name = a.name or "ticking-em-map-%s-01" % datetime.date.today().strftime("%Y%m%d")
    D = Path(a.out) / name if a.out else R / ".releases" / name
    if not a.out:
        assert Path.cwd() == R, "run from the Ticking root"
    D.mkdir(parents=True, exist_ok=False)
    old = json.loads((base / "manifest.json").read_text())
    for n, sha in old.items():
        assert digest(base / ".vercel/output/static" / n) == sha, ("base release differs from its manifest", n)
    shutil.copytree(base / ".vercel", D / ".vercel")
    shutil.copytree(base / ".inputs", D / ".inputs")
    mjs = (base / ".products-build.mjs").read_text().replace(str(base / ".inputs"), str(D / ".inputs"))
    if a.out:   # dry run outside R: the production guard requires R/.releases; relax it only for the copy
        mjs = "\n".join(l for l in mjs.splitlines() if not l.startswith("if(!root.startsWith("))
    (D / ".products-build.mjs").write_text(mjs)
    pkg = Path(a.package)
    subprocess.run([sys.executable, str(pkg / "apply_fe_patch.py"), str(D), "--assets", str(pkg / "em-assets")], check=True)
    T = D / ".vercel/output/static"
    for n in CHANGED_SRC + NEW_SRC:   # the static tree also serves src/ (external SDK imports resolve against it)
        (T / n).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(D / ".inputs" / n, T / n)
    shutil.rmtree(T / "assets/em", ignore_errors=True)
    shutil.copytree(D / ".inputs/assets/em", T / "assets/em")
    shutil.copy2(D / ".inputs/index.html", T / "index.html")   # the builder rewrites script/style links from the source html
    subprocess.run(["node", str(D / ".products-build.mjs"), str(D)], check=True, cwd=str(D))
    new = {str(p.relative_to(T)): digest(p) for p in T.rglob("*") if p.is_file()}
    missing = sorted(set(old) - set(new))
    assert not missing, ("retained public files disappeared", missing[:5])
    changed = sorted(k for k in old if new[k] != old[k])
    allowed = set(CHANGED_SRC) | {"index.html"}
    assert all(k in allowed for k in changed), ("unexpected changes", [k for k in changed if k not in allowed])
    added = sorted(set(new) - set(old))
    assert all(k.startswith(("_app/", "assets/em/")) or k in NEW_SRC for k in added), ("unexpected new files", [k for k in added if not k.startswith(("_app/", "assets/em/"))][:5])
    assert (D / ".vercel/output/config.json").read_bytes() == (base / ".vercel/output/config.json").read_bytes()
    (D / "manifest.json").write_text(json.dumps(new, indent=2))
    meta = json.loads((D / "build-meta.json").read_text())
    meta.update(maintenanceBase=pointer, maintenanceChanges=changed + added[:0], emChangeSet={"changed": changed, "addedCount": len(added), "new": NEW_SRC})
    (D / "build-meta.json").write_text(json.dumps(meta, indent=2))
    (D / "retained-assets.json").write_text(json.dumps({"selection": "Exact base public manifest; all live chunks preserved", "base": pointer["release"], "files": old}, indent=2))
    report = {"passed": True, "release": str(D.relative_to(R)) if not a.out else str(D), "base": pointer["release"], "changed": changed,
              "added": len(added), "addedBytes": sum((T / k).stat().st_size for k in added), "files": len(new)}
    O = (R / "evidence" / ("em-map-%s" % datetime.date.today().strftime("%Y%m%d"))) if not a.out else D / "evidence"
    O.mkdir(parents=True, exist_ok=True)
    (O / "build.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


def evidence(a):
    if a.out:
        return Path(a.out) / (a.name or "ticking-em-map-%s-01" % datetime.date.today().strftime("%Y%m%d")) / "evidence"
    return R / "evidence" / ("em-map-%s" % datetime.date.today().strftime("%Y%m%d"))


def verify(a):
    O = evidence(a)
    d = json.loads((O / "build.json").read_text())
    D = R / d["release"]
    pointer = json.loads((R / ".releases/current-public.json").read_text())
    assert pointer["release"] == d["base"], "public release advanced since build; rebuild on the new base"
    m = json.loads((D / "manifest.json").read_text())
    bad = [k for k, sha in m.items() if digest(D / ".vercel/output/static" / k) != sha]
    assert not bad, bad[:5]
    print(json.dumps({"verified": True, "release": d["release"], "files": len(m)}))


def package(a):
    scope(a)
    O = evidence(a)
    d = json.loads((O / "build.json").read_text())
    D = R / d["release"]
    pointer = json.loads((R / ".releases/current-public.json").read_text())
    assert pointer == json.loads((D / "build-meta.json").read_text())["maintenanceBase"], "public release advanced; rebuild on the new base"
    base = R / pointer["release"]
    old = json.loads((base / "manifest.json").read_text())
    new = json.loads((D / "manifest.json").read_text())
    T = D / ".vercel/output/static"
    assert (D / ".vercel/output/config.json").read_bytes() == (base / ".vercel/output/config.json").read_bytes()
    files, inline = [], []
    for p in [D / ".vercel/output/config.json", *sorted(T.rglob("*"))]:
        if not p.is_file():
            continue
        raw = p.read_bytes()
        name = str(p.relative_to(D))
        rel = str(p.relative_to(T)) if p.is_relative_to(T) else None
        if rel:
            assert hashlib.sha256(raw).hexdigest() == new[rel], rel
        if rel is None or new[rel] == old.get(rel):   # already on Vercel from the base deployment
            files.append({"file": name, "sha": hashlib.sha1(raw).hexdigest(), "size": len(raw)})
        else:
            assert rel in CHANGED_SRC + NEW_SRC or rel == "index.html" or rel.startswith(("_app/", "assets/em/")), rel
            files.append({"file": name, "data": base64.b64encode(raw).decode(), "encoding": "base64"})
            inline.append(rel)
    payload = {"name": "ticking", "project": "prj_H9mGBcGrDgqTjycpu1ZFeAYYbkAp", "target": "production", "autoAssignCustomDomains": False,
               "files": files, "meta": {"actor": "claude", "purpose": "em-map-" + datetime.date.today().strftime("%Y%m%d")}}
    (O / "deployment-request.json").write_text(json.dumps(payload, separators=(",", ":")))
    (O / "deployment-preparation.json").write_text(json.dumps({"release": d["release"], "base": pointer["release"], "inline": len(inline),
                                                              "files": len(files), "privateStateExcluded": True}, indent=2))
    print(json.dumps({"files": len(files), "inline": len(inline), "requestBytes": (O / "deployment-request.json").stat().st_size}))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("build", "verify", "package"))
    ap.add_argument("--package", default="/srv/skew/em-ticking-bridge/package")
    ap.add_argument("--name")
    ap.add_argument("--out")
    a = ap.parse_args()
    {"build": build, "verify": verify, "package": package}[a.cmd](a)
