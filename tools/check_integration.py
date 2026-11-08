#!/usr/bin/env python3
"""Offline evidence only. --compat normalizes in-memory lazy syntax, never files.
No Redis server, Docker, real TIFF dataset or output-accuracy acceptance implied.
"""
import argparse
import ast
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_SHA256 = {'src/dispatcher.py': '83491ab6924f75c01b16ab242f6e0674bcebd1096bb74f40464eae457280e28f', 'compose.yaml': '13aaffe33877a06abfc34c87bfea2328640af77453c43dcff66537b20cdb16f4'}

def normalized(text):
    return re.sub(r'(?m)^(\s*)lazy (import |from )', r'\1\2', text)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compat', action='store_true', help='diagnostic test harness for Python <3.15')
    parser.add_argument('--output', default='integration-evidence.json')
    args = parser.parse_args()
    checks = []
    def record(name, passed, detail):
        checks.append(dict(name=name, passed=bool(passed), detail=detail))
    sys.path.insert(0, str(ROOT))
    sources = sorted(ROOT.glob('src/**/*.py')) + sorted(ROOT.glob('tests/*.py')) + [Path(__file__).resolve()]
    for path in sources:
        try:
            text = path.read_text()
            compile(normalized(text) if args.compat else text, str(path), 'exec')
            record('compile:' + str(path.relative_to(ROOT)), True, 'in-memory normalized' if args.compat else 'native')
        except Exception as exc:
            record('compile:' + str(path.relative_to(ROOT)), False, repr(exc))
    for p, expected in ORIGINAL_SHA256.items():
        actual = hashlib.sha256((ROOT/p).read_bytes()).hexdigest()
        record('original_bytes:' + p, actual == expected, dict(expected=expected, actual=actual))
    text = (ROOT/'src/dispatcher.py').read_text()
    tree = ast.parse(text)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'LogDispatcher')
    init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '__init__')
    record('startup_invokes_recovery', any(isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='recover_and_auto_resume' for n in ast.walk(init)), 'static AST assertion; not live Redis')
    # Execute the original recovery method in isolation with interface doubles.
    # No application import, no connection, no thread started by this harness.
    method = next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='recover_and_auto_resume')
    from enum import Enum
    from types import SimpleNamespace
    class Mode(str, Enum):
        IDLE='IDLE'; LIVE='LIVE'; BATCH='BATCH'
    class Status(str, Enum):
        COMPLETED='COMPLETED'; PROCESSING='PROCESSING'; MERGING='MERGING'
    for mode in ('LIVE','BATCH','IDLE'):
        calls=[]
        class Thread:
            def __init__(self, **kw): self.kw=kw
            def start(self): calls.append(('thread',self.kw))
        ns=dict(DispatcherMode=Mode,LogStatus=Status,threading=SimpleNamespace(Thread=Thread))
        try:
            exec(compile(ast.Module(body=[method],type_ignores=[]),'<recovery-isolation>','exec'),ns)
            client=SimpleNamespace(get_all_tracked_logs=lambda: {'done':{'status':Status.COMPLETED},'depth':{'status':Status.PROCESSING},'merge':{'status':Status.MERGING}},get_pipeline_state=lambda: dict(mode=mode,is_running=mode!='IDLE',current_batch=2,total_batches=4,batch_size=3,target_file='fixture.json'))
            def dispatch(path,enqueue):
                calls.append(('audit',str(path),enqueue));return {'action':'RECONCILED','re_enqueued':1}
            batch=lambda **kw: None
            live=lambda **kw: None
            obj=SimpleNamespace(client=client,active_log_ids=[],completed_log_ids=[],mode=Mode.IDLE,current_batch_index=0,total_batches=0,_resolve_annotation_path=lambda log:Path(log+'.json'),dispatch_single_log=dispatch,run_batch_pipeline=batch,run_live_stream=live)
            with contextlib.redirect_stdout(io.StringIO()): ns['recover_and_auto_resume'](obj)
            assert obj.completed_log_ids==['done'] and obj.active_log_ids==['depth','merge']
            assert [c for c in calls if c[0]=='audit']==[('audit','depth.json',False),('audit','merge.json',False)]
            threads=[c[1] for c in calls if c[0]=='thread']
            if mode=='IDLE': assert not threads
            else:
                assert len(threads)==1 and threads[0]['daemon'] is True
                expected={'batch_size':3,'resume_from_batch':2} if mode=='BATCH' else {'target_file':'fixture.json'}
                assert threads[0]['kwargs']==expected
                assert threads[0]['target'] is (batch if mode=='BATCH' else live)
                assert obj.current_batch_index==2 and obj.total_batches==4
            record('isolated_startup_recovery:'+mode,True,'interface doubles; no durability claim')
        except Exception as exc:
            record('isolated_startup_recovery:'+mode,False,repr(exc))
    # Validate build COPY inputs exist and dependency names, without invoking pip.
    for path in sorted(ROOT.glob('build/*/Dockerfile')):
        missing=[]
        for line in path.read_text().splitlines():
            if line.startswith('COPY '):
                for source in line.split()[1:-1]:
                    if not (ROOT/source).exists(): missing.append(source)
        record('build_inputs:'+str(path.relative_to(ROOT)),not missing,missing)
    required={'depth':{'numpy','redis','tifffile','zarr','pillow','imagecodecs'},'dispatcher':{'redis','fastapi','pydantic','uvicorn'},'lithology':{'numpy','redis','rasterio','pillow'},'merger':{'numpy','redis','rasterio','pillow'}}
    for service, names in required.items():
        text=(ROOT/'build'/service/'requirements.txt').read_text()
        actual={re.split(r'[<>=!~\[; ]',s.strip().lower())[0] for s in text.splitlines() if s.strip() and not s.startswith('#')}
        record('requirements:'+service,names<=actual and 'tiffile' not in actual,dict(required=sorted(names),declared=sorted(actual)))
    # Existing contract suite supplies import-only redis/rasterio doubles in compat.
    # Import it FIRST so later suites share the same normalized src loader.
    if args.compat: os.environ['PIPELINE_CONTRACT_COMPAT']='1'
    log=io.StringIO()
    result=None
    try:
        suite=unittest.TestSuite()
        with contextlib.redirect_stdout(log),contextlib.redirect_stderr(log):
            loader=unittest.TestLoader()
            for pattern in ['test_pipeline_contract.py'] + [p.name for p in sorted((ROOT/'tests').glob('test*.py')) if p.name != 'test_pipeline_contract.py']:
                suite.addTests(loader.discover(str(ROOT/'tests'),pattern=pattern))
            result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
    except Exception as exc:
        record('test_runner',False,repr(exc))
    tests={} if result is None else dict(run=result.testsRun,failures=[{'test':str(t),'traceback':trace} for t,trace in result.failures],errors=[{'test':str(t),'traceback':trace} for t,trace in result.errors],skipped=[{'test':str(t),'reason':why} for t,why in result.skipped],successful=result.wasSuccessful())
    passed=all(c['passed'] for c in checks) and result is not None and result.testsRun>0 and result.wasSuccessful()
    evidence=dict(scope='offline diagnostics',python=sys.version,compat=args.compat,production_source_modified=False,checks=checks,tests=tests,test_log=log.getvalue(),overall_passed=passed,not_executed=['docker compose build/up','native Python 3.15 run' if args.compat else 'live Redis durability test','real dataset end-to-end CSV accuracy and completion','process crash and AOF recovery'])
    Path(args.output).write_text(json.dumps(evidence,indent=2)+'\n')
    print(json.dumps(evidence,indent=2))
    return 0 if passed else 1

if __name__=='__main__':
    raise SystemExit(main())
