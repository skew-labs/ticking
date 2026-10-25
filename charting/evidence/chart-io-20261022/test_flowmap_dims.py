import tempfile,sqlite3,pathlib,sys,itertools
sys.path.insert(0,str(pathlib.Path(__file__).parent));import flowmap_core,duckdb
for has_p,has_v in itertools.product((False,True),repeat=2):
 with tempfile.TemporaryDirectory() as td:
  p=pathlib.Path(td)/'dims.sqlite';s=sqlite3.connect(p);s.execute('CREATE TABLE tokens(token TEXT)')
  if has_p:s.executescript("CREATE TABLE pools(pool TEXT,protocol TEXT); INSERT INTO pools VALUES('a','univ3');")
  if has_v:s.executescript("CREATE TABLE v4pools(pool_id TEXT,protocol TEXT); INSERT INTO v4pools VALUES('b','univ4');")
  s.commit();s.close();c=duckdb.connect();c.execute('LOAD sqlite');c.execute("CREATE TABLE trc(pool TEXT,usd DOUBLE); INSERT INTO trc VALUES('a',10),('a',20),('b',30),('c',40)")
  cov=flowmap_core.pool_protocols(c,str(p));got=c.execute('SELECT pool,protocol FROM fm_pools ORDER BY pool').fetchall()
  assert got==[('a','univ3' if has_p else None),('b','univ4' if has_v else None),('c',None)],got
  assert cov['complete']==(has_p and has_v) and cov['available']==(has_p or has_v)
  assert c.execute('SELECT sum(usd) FROM trc LEFT JOIN fm_pools USING(pool)').fetchone()[0]==100
  c.execute("ATTACH '"+str(p)+"' AS fmd (TYPE sqlite, READ_ONLY)");c.execute('DETACH fmd');c.close()
print('PASS 4 table-presence combinations; amounts preserved; missing coverage explicit; attachment closed')
