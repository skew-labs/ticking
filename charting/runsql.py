"""Run ';'-separated read-only SQL from a file (or stdin) in DuckDB and print each result (dev helper)."""
import sys
import duckdb
db = sys.argv[2] if len(sys.argv) > 2 else ":memory:"
c = duckdb.connect(db, read_only=db != ":memory:")
c.execute("SET threads=2")
sql = open(sys.argv[1]).read() if len(sys.argv) > 1 and sys.argv[1] != "-" else sys.stdin.read()
for stmt in [s for s in sql.split(";") if s.strip()]:
    try:
        cur = c.execute(stmt)
        if cur.description:
            print([d[0] for d in cur.description])
            for r in cur.fetchall():
                print(r)
    except Exception as e:
        print("ERR", str(e)[:400])
