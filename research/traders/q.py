"""Ad-hoc read-only queries: q.py < file.sql (statements separated by ;; ). O = traders dir of the chain in $CH."""
import duckdb, os, sys
c = duckdb.connect(); c.execute("SET threads=4"); c.execute("SET memory_limit=\x274GB\x27")
O = "/srv/skew/em-data/traders/" + os.environ.get("CH", "robinhood")
for stmt in sys.stdin.read().split(";;"):
    stmt = stmt.strip().replace("{O}", O)
    if not stmt: continue
    cur = c.execute(stmt)
    if cur.description:
        print(" | ".join(d[0] for d in cur.description))
        for r in cur.fetchall():
            print(" | ".join(str(round(x, 4) if isinstance(x, float) else x) for x in r))
    print("--")
