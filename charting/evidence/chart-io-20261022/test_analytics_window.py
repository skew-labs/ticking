import ast
import sqlite3
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import duckdb
import analytics_window as aw

HERE=Path(__file__).parent

class WindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.c=duckdb.connect();self.c.execute("SET threads=1; SET memory_limit='128MB'")
        self.c.execute("CREATE TABLE bm AS SELECT (1700000000+i*60)::BIGINT AS minute, (100+i*43)::UBIGINT first_block FROM range(10000) t(i)")
    def tearDown(self):self.c.close();self.tmp.cleanup()
    def fixture(self,name,start=0,count=20000,unsorted=False):
        p=self.root/name
        order='ORDER BY hash(i)' if unsorted else ''
        self.c.execute(f"COPY (SELECT CASE WHEN i%997=0 THEN NULL ELSE i END::UBIGINT bn, '0xtoken'||(i%7)::VARCHAR token, '0xholder'||(i%31)::VARCHAR holder, CASE WHEN i%41=0 THEN NULL ELSE ((i%19)-9)/3.0 END::DOUBLE delta FROM range({start},{start+count}) t(i) {order}) TO '{p}' (FORMAT PARQUET, ROW_GROUP_SIZE 2048)")
        return str(p)
    def same(self,files,minimum=100,batch=1024):
        self.c.execute('CREATE OR REPLACE TABLE expected AS SELECT x.token,x.holder,x.delta,b.minute FROM (SELECT * FROM read_parquet(?) WHERE bn>=?) x ASOF JOIN bm b ON x.bn>=b.first_block',[files,minimum])
        stats=aw.build_window(self.c,files,minimum,batch_rows=batch)
        diff=self.c.execute('SELECT count(*) FROM ((SELECT * FROM expected EXCEPT ALL SELECT * FROM xf) UNION ALL (SELECT * FROM xf EXCEPT ALL SELECT * FROM expected))').fetchone()[0]
        self.assertEqual(diff,0)
        self.assertLessEqual(stats['maxBatchRows'],batch)
        return stats
    def test_all_rows_multifile_multigroup_cross_boundary_and_null(self):
        files=[self.fixture('a.parquet'),self.fixture('b.parquet',19800,23000,True)]
        stats=self.same(files)
        self.assertGreater(stats['rowGroupsRead'],2);self.assertGreater(stats['batches'],stats['rowGroupsRead'])
    def test_seven_days_exact_boundary_with_preceding_minute_outside_batch(self):
        p=self.fixture('seven-days.parquet',0,22000)
        # The cutoff deliberately lies between minute boundaries and inside a row group.
        stats=self.same([p],minimum=8301)
        self.assertGreater(stats['rowGroupsSkipped'],0)
        got=self.c.execute("SELECT count(*) FROM xf WHERE minute=1700011400").fetchone()[0]
        self.assertGreater(got,0)
    def test_duplicate_files_preserve_duplicate_events(self):self.same([self.fixture('a.parquet')]*2)
    def test_empty_and_all_filtered_files(self):
        self.same([self.fixture('empty.parquet',0,0),self.fixture('old.parquet',0,50)],100)
        self.assertEqual(self.c.execute('SELECT count(*) FROM xf').fetchone()[0],0)
    def test_no_files(self):self.assertEqual(aw.build_window(self.c,[],100)['batches'],0)
    def test_invalid_batch(self):
        for n in (0,1,262145):
            with self.assertRaises(ValueError):aw.build_window(self.c,[],100,batch_rows=n)
    def test_input_replacement_fails_before_publication(self):
        p=self.fixture('a.parquet');original=aw._stamp
        calls=[]
        def changed(f):
            s=original(f);calls.append(1);return s if len(calls)==1 else s[:-1]+(s[-1]+1,)
        with patch.object(aw,'_stamp',changed),self.assertRaisesRegex(ValueError,'changed'):
            aw.build_window(self.c,[p],100)
    def test_missing_schema_fails(self):
        p=self.root/'bad.parquet';self.c.execute(f"COPY (SELECT 1 x WHERE false) TO '{p}' (FORMAT PARQUET)")
        with self.assertRaisesRegex(ValueError,'schema'):aw.build_window(self.c,[str(p)],100)

class LifecycleTests(unittest.TestCase):
    def load_module(self):
        common=types.ModuleType('common');common.EM='/unused';common.ROOT='/unused';common.connect=lambda **kw:None
        with patch.dict(sys.modules,{'common':common}):
            spec=importlib.util.spec_from_file_location('analytics_under_test',HERE/'analytics.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        return mod
    def test_refresh_closes_connection_on_input_failure(self):
        a=self.load_module()
        class Conn:
            closed=False
            def close(self):self.closed=True
        c=Conn()
        with tempfile.TemporaryDirectory() as p:
            Path(p,'static.json').write_text(json.dumps({'snapshot_block':1,'tail_end_block':1}))
            with patch.object(a,'d',lambda chain:p),patch.object(a,'connect',lambda **kw:c),patch.object(a,'load_dims',side_effect=ValueError('test failure')):
                with self.assertRaisesRegex(ValueError,'test failure'):a.refresh('fixture',reader=object())
        self.assertTrue(c.closed)
    def test_zero_delta_checkpoint_reuses_filtered_exact_values(self):
        a=self.load_module();c=duckdb.connect()
        try:
            with tempfile.TemporaryDirectory() as p:
                Path(p,'holders_snap.parquet').write_bytes(b'fixture')
                f=Path(p,'x.parquet');f.write_bytes(b'fixture')
                c.execute(f"COPY (SELECT 't' token,'h' holder, -1.23456789012345::DOUBLE amount) TO '{p}/bal_ckpt.parquet' (FORMAT PARQUET)")
                def sig(f):st=os.stat(f);return [st.st_size,st.st_mtime_ns]
                manifest={'snap':7,'bm_lo':5,'files':{str(f):sig(f),'__holders_snap__':sig(Path(p,'holders_snap.parquet'))}}
                Path(p,'bal_ckpt.json').write_text(json.dumps(manifest))
                sql,mode,_=a.bal_inputs(c,p,'',7,[str(f)],5)
                self.assertEqual(mode,'incremental (+0 files)')
                self.assertEqual(c.execute(sql).fetchall(),[('t','h',-1.23456789012345)])
                f.write_bytes(b'changed')
                self.assertEqual(a.bal_inputs(c,p,'SELECT NULL',7,[str(f)],5)[1],'full')
        finally:c.close()

class PublicationTests(unittest.TestCase):
    def test_failed_publication_rolls_back_and_releases_writer(self):
        source=ast.parse((HERE/'analytics.py').read_text())
        fn=next(n for n in source.body if isinstance(n,ast.FunctionDef) and n.name=='write_results')
        start=next(i for i,n in enumerate(fn.body) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='db' for t in n.targets))
        body=fn.body[start:]
        wrapper=ast.FunctionDef(name='publish',args=ast.arguments(posonlyargs=[],args=[],kwonlyargs=[],kw_defaults=[],defaults=[]),body=body,decorator_list=[])
        module=ast.fix_missing_locations(ast.Module(body=[wrapper],type_ignores=[]))
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'results.sqlite';db=sqlite3.connect(p,isolation_level=None)
            db.execute('CREATE TABLE token_state(a,b,c)');db.execute("INSERT INTO token_state VALUES('previous','{}',1)")
            class Failing:
                def execute(self,sql,*args):
                    if sql=='DELETE FROM liq_positions':raise ValueError('injected publication failure')
                    return db.execute(sql,*args)
                def executemany(self,sql,args):return db.executemany(sql,args)
                def close(self):db.close()
            env=dict(res_db=lambda chain:Failing(),chain='x',states={},now=2,json=json)
            exec(compile(module,'publication-test','exec'),env)
            with self.assertRaisesRegex(ValueError,'injected'):env['publish']()
            other=sqlite3.connect(p,timeout=.1,isolation_level=None)
            self.assertEqual(other.execute('SELECT a FROM token_state').fetchone(),('previous',))
            other.execute('BEGIN IMMEDIATE');other.execute('ROLLBACK');other.close()

if __name__=='__main__':unittest.main(verbosity=2)
