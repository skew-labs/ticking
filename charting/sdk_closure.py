"""Print the relative import closure of a Ticking SDK entry (read-only)."""
import os, re, sys
root, entry = sys.argv[1], sys.argv[2]
os.chdir(root)
pat = re.compile(r"""(?:import|export)[^;]*?from\s*['"]([^'"]+)['"]|import\(\s*['"]([^'"]+)['"]\s*\)|new URL\(\s*['"]([^'"]+)['"]\s*,\s*import\.meta\.url""")
seen, todo, missing = set(), [entry], []
while todo:
    f = todo.pop()
    if f in seen:
        continue
    if not os.path.exists(f):
        missing.append(f); continue
    seen.add(f)
    if not f.endswith(".js"):
        continue
    for m in pat.findall(open(f, encoding="utf-8").read()):
        p = [x for x in m if x][0]
        if p.startswith("."):
            todo.append(os.path.normpath(os.path.join(os.path.dirname(f), p)))
print(len(seen), "files", sum(os.path.getsize(f) for f in seen) // 1024, "KB; missing:", missing)
print("\n".join(sorted(seen)))
