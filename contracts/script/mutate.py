"""Mutation testing for src/TickingRouter.sol. Generates operator mutants line-by-line, runs the non-fork suite
(unit + fuzz + invariant on mocks covering every venue) in isolated copies, reports kill rate + survivors.
Usage: python3 script/mutate.py [workers]  -> writes mutation_report.json"""
import os, re, sys, json, shutil, subprocess, tempfile, concurrent.futures as cf
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, 'src/TickingRouter.sol')).read().split('\n')
OPS = [(r'!=', '=='), (r'==', '!='), (r'<=', '<'), (r'>=', '>'), (r'(?<![<>=!])<(?![<=])', '<='),
       (r'(?<![<>=\-])>(?![>=])', '>='), (r'&&', '||'), (r'\|\|', '&&'), (r'\+ 1\b', '+ 0'), (r'- 1\b', '+ 1'),
       (r'\btrue\b', 'false'), (r'\bzfo \?', '!zfo ?'), (r'MIN_SQRT_P1 : MAX_SQRT_M1', 'MAX_SQRT_M1 : MIN_SQRT_P1')]
STMT_DEL = re.compile(r'^\s*(revert \w+\(.*\);|if \(.*\) revert \w+\(.*\);|_disarmed\(\);|_arm\(.*\);|tstore\(T_\w+, 0\)|'
                      r'_pay\(.*\);|_approve\(.*\);|_transfer\(.*\);|total \+= a;)\s*(//.*)?$')
def in_body(n):  # skip interfaces / comments / constants declarations
    l = SRC[n].strip()
    return l and not l.startswith(('//', '///', '*', 'uint256 internal constant', 'address public', 'error ', 'event ',
                                   'function ', 'interface', 'struct', 'pragma', 'import', 'contract'))
mutants = []
end = next(i for i, l in enumerate(SRC) if l.startswith('// ───') and 'interfaces' in l)
for n in range(end):
    if not in_body(n): continue
    line = SRC[n]
    code = line.split('//')[0]
    for pat, rep in OPS:
        for m in re.finditer(pat, code):
            mutants.append((n, line[:m.start()] + rep + line[m.end():], f'{pat}->{rep}'))
    if STMT_DEL.match(line):
        mutants.append((n, re.sub(r'\S.*', '{}' if 'tstore' not in line else 'pop(0)', line, count=1), 'delete-stmt'))
def run(idx):
    n, newline, kind = mutants[idx]
    d = tempfile.mkdtemp(prefix='mut')
    for x in ('foundry.toml', 'src', 'test'): (shutil.copytree if os.path.isdir(os.path.join(ROOT, x)) else shutil.copy)(os.path.join(ROOT, x), os.path.join(d, x))
    for x in ('test/Fork.t.sol', 'test/ForkCov.t.sol', 'test/legacy', 'test/halmos'):
        p = os.path.join(d, x); shutil.rmtree(p) if os.path.isdir(p) else os.path.exists(p) and os.remove(p)
    os.symlink(os.path.join(ROOT, 'lib'), os.path.join(d, 'lib'))
    src = list(SRC); src[n] = newline
    open(os.path.join(d, 'src/TickingRouter.sol'), 'w').write('\n'.join(src))
    env = dict(os.environ, FOUNDRY_FUZZ_RUNS='128', FOUNDRY_INVARIANT_RUNS='12', FOUNDRY_INVARIANT_DEPTH='60')
    b = subprocess.run(['forge', 'build', '-q'], cwd=d, env=env, capture_output=True, timeout=600)
    if b.returncode != 0: res = 'stillborn'
    else:
        t = subprocess.run(['forge', 'test'], cwd=d, env=env, capture_output=True, timeout=900)
        res = 'killed' if t.returncode != 0 else 'SURVIVED'
    shutil.rmtree(d, ignore_errors=True)
    return dict(line=n + 1, kind=kind, orig=SRC[n].strip(), mutant=newline.strip(), result=res)
if __name__ == '__main__':
    w = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    print(len(mutants), 'mutants', flush=True)
    out = []
    with cf.ThreadPoolExecutor(w) as ex:
        for r in ex.map(run, range(len(mutants))):
            out.append(r); print(r['result'], r['line'], r['kind'], r['mutant'][:90], flush=True)
    valid = [r for r in out if r['result'] != 'stillborn']
    k = sum(r['result'] == 'killed' for r in valid)
    rep = dict(total=len(out), stillborn=len(out) - len(valid), valid=len(valid), killed=k,
               kill_rate=round(100 * k / max(1, len(valid)), 2), survivors=[r for r in valid if r['result'] == 'SURVIVED'])
    json.dump(rep, open(os.path.join(ROOT, 'mutation_report.json'), 'w'), indent=1)
    print(json.dumps({k: v for k, v in rep.items() if k != 'survivors'}))
