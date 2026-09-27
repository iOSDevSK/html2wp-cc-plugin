#!/usr/bin/env python3
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
import sys
sys.path.insert(0, str(HERE / 'lib'))
import astro_delivery as policy
import theme_state
spec = importlib.util.spec_from_file_location('astro_repair', HERE / 'astro-delivery.py')
helper = importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)


class Recovery(unittest.TestCase):
    def test_coverage_keeps_expected_routes_even_when_manifest_is_reduced(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = Path(tmp); d = w / 'astro-project/dist'; d.mkdir(parents=True)
            (d / 'auth.html').write_text('<h1>Error</h1>')
            helper.write(w / 'conversion-manifest.json', {'pages':[{'file':'auth.html'}]})
            helper.write(w / 'route-inventory.json', {'routes':['/', '/auth', '/men']})
            result = policy.coverage(w)
            self.assertFalse(result['passed']); self.assertEqual(result['missing'], ['index.html', 'men.html'])
            (d / 'index.html').write_text('home'); (d / 'men').mkdir(); (d / 'men/index.html').write_text('men')
            self.assertTrue(policy.coverage(w)['passed'])
            helper.write(w / 'prerender-report.json', {'pages':{'auth.html':{'sourceErrors':['backend unavailable']}}})
            self.assertFalse(policy.coverage(w)['passed'])

    def test_static_routes_take_precedence_over_dynamic_template(self):
        tree = ast.parse((HERE / 'prerender-spa.py').read_text())
        names = {'route_group', 'tanstack_file_routes', 'tanstack_param_routes', 'param_route_patterns'}
        env = {'Path':Path, 're':re, 'CATCHALL_PROBE':'/__404__'}
        exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[]), '<routes>', 'exec'), env)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'src/routes'; p.mkdir(parents=True)
            for f in ['admin.tsx', 'auth.tsx', '$gender.tsx', 'index.tsx']: (p/f).touch()
            static = env['tanstack_file_routes'](Path(tmp), False)
            declared = env['param_route_patterns'](env['tanstack_param_routes'](Path(tmp)))
            for r in ['/admin', '/auth', '/']:
                self.assertEqual(env['route_group'](r, [], {}, declared, static), (None, False))
            self.assertEqual(env['route_group']('/men', [], {}, declared, static), ('/:gender', True))

    def test_capture_failure_does_not_abort_other_routes(self):
        tree = ast.parse((HERE / 'prerender-spa.py').read_text())
        func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_capture_routes')
        class Page:
            def on(self, *args): pass
        class Context:
            def add_init_script(self, *args): pass
            def new_page(self): return Page()
            def close(self): pass
        class Browser:
            def new_context(self, **kwargs): return Context()
            def close(self): pass
        class Driver:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            @property
            def chromium(self): return self
            def launch(self): return Browser()
        with tempfile.TemporaryDirectory() as tmp:
            w=Path(tmp)
            def capture(page, base, route, route_map, runtime, recs, scroll, links, dest, **kwargs):
                if route == '/bad': raise RuntimeError('pending request timeout')
                dest.write_text('captured')
            env={'sync_playwright':Driver,'FORM_RECORDS':{},'FORM_SUCCESS':{},'KNOWN_ROUTES':set(),
                 'guard_context':lambda *a:None,'HELPERS':'','route_to_file':policy.route_file,
                 'report':{'pages':{},'warnings':[]},'REVEAL_PAGES':{},'OUT':w,'dropped_scripts':set(),
                 'capture':capture,'warn':lambda x:None,'time':__import__('time')}
            exec(compile(ast.Module(body=[func],type_ignores=[]),'<capture>', 'exec'),env)
            routes=['/bad','/good'];records={r:([],[],[]) for r in routes}
            report=env['_capture_routes']((routes,'http://test',{},False,records,{}, {},{},0,2))
            self.assertIn('timeout',report['pages']['bad.html']['captureError'])
            self.assertIsNone(report['pages']['good.html']['captureError'])
            self.assertTrue((w/'good.html').is_file())

    def test_parallel_failure_retries_only_failed_routes_once(self):
        from types import SimpleNamespace
        tree = ast.parse((HERE / 'prerender-spa.py').read_text())
        func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'capture_all')
        for fail_again in (False, True):
            calls = []
            def capture(job):
                routes = job[0]; calls.append(list(routes))
                return {'pages': {policy.route_file(r): {'captureError': 'timeout' if r == '/bad' and (len(calls) <= 2 or fail_again) else None} for r in routes},
                        'warnings': [], 'reveals': {}, 'dropped': [], 'times': {r: 1 for r in routes}}
            class Pool:
                def __init__(self, **kwargs): pass
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def map(self, f, jobs): return map(f, jobs)
            env = {'Path': Path, 'args': SimpleNamespace(jobs=2), 'FORM_COUNT': {}, 'TEMPLATE_CHAINS': {},
                   'FORM_RECORDS': {}, 'FORM_SUCCESS': {}, 'REVEAL_PAGES': {}, 'dropped_scripts': set(),
                   'report': {'pages': {}, 'warnings': []}, 'route_to_file': policy.route_file,
                   'progress_note': lambda _: None, '_capture_routes': capture}
            exec(compile(ast.Module(body=[func],type_ignores=[]),'<capture_all>', 'exec'),env)
            with patch('concurrent.futures.ProcessPoolExecutor', Pool):
                env['capture_all'](['/bad','/good'], {'/bad': [], '/good': []}, 'http://test', {}, False, {'routes': {}})
            self.assertEqual(calls, [['/bad'], ['/good'], ['/bad']])
            self.assertTrue(env['report']['pages']['bad.html']['captureRetried'])
            self.assertEqual(bool(env['report']['pages']['bad.html']['captureError']), fail_again)
            self.assertIsNone(env['report']['pages']['good.html']['captureError'])

    def test_publication_rolls_back_between_moves_and_result_writes(self):
        for fail_name in ('astro-project', 'CONVERSION-REPORT.md', 'output-result.json'):
            with self.subTest(fail_name=fail_name), tempfile.TemporaryDirectory() as tmp:
                w=Path(tmp)/'ws'; w.mkdir(); out=w/'out';out.mkdir()
                h=w/'history';h.mkdir();c=h/'candidate';c.mkdir();(c/'out').mkdir()
                (w/'astro-project').mkdir();(w/'astro-project/old.txt').write_text('old')
                (c/'astro-project').mkdir();(c/'astro-project/new.txt').write_text('new')
                (out/'old.zip').write_bytes(b'old archive')
                (out/'CONVERSION-REPORT.md').write_text('old report')
                (w/'result.json').write_text('old workspace result')
                (out/'result.json').write_text('old exported result')
                (c/'out/new.zip').write_bytes(b'new archive')
                (c/'out/CONVERSION-REPORT.md').write_text('new report')
                result={'astro':{'file':'new.zip'},'repairRequestId':'owner'}
                rename=Path.rename; fired=[]
                def fault(src,dest):
                    if src.name==fail_name and (src.parent==c or src.parent.name=='publication') and not fired:
                        fired.append(True);raise OSError('injected publication failure')
                    return rename(src,dest)
                with patch.object(Path,'rename',fault),self.assertRaises(OSError):
                    helper.publish_candidate(w,out,h,c,result,'new-r2.zip')
                self.assertTrue(fired)
                self.assertEqual((w/'astro-project/old.txt').read_text(),'old')
                self.assertEqual((w/'result.json').read_text(),'old workspace result')
                self.assertEqual((out/'result.json').read_text(),'old exported result')
                self.assertEqual((out/'CONVERSION-REPORT.md').read_text(),'old report')
                self.assertEqual((out/'old.zip').read_bytes(),b'old archive')
                self.assertFalse((out/'new-r2.zip').exists())

    def fixture(self, w):
        p = w/'astro-project'; (p/'public').mkdir(parents=True); (p/'dist').mkdir()
        (p/'package.json').write_text('{"dependencies":{"astro":"5.18.2"}}')
        (p/'public/auth.html').write_text('auth'); (p/'dist/auth.html').write_text('auth')
        out=w/'out';out.mkdir();z=out/'test-astro-1.zip';theme_state.zip_astro_project(p,z,'test-astro')
        base={'schema':'h2wp-result/1','target':'astro','status':'delivered','site':{'slug':'test','version':'1'},'astro':{'file':z.name,'sha256':helper.sha(z)},'revision':1}
        helper.write(w/'result.json',base);helper.write(out/'result.json',base)
        helper.write(w/'progress.json',{'mode':'astro','target':'astro','stages':[]})
        helper.write(w/'detect.json',{'root':str(w/'missing-source')})
        return z,base

    def test_repair_commits_change_baseline_and_failed_publication_is_not_fixed(self):
        from types import SimpleNamespace
        apply_spec = importlib.util.spec_from_file_location('apply_change', HERE / 'apply-change.py')
        change = importlib.util.module_from_spec(apply_spec); apply_spec.loader.exec_module(change)
        for fail in (False, True):
            with self.subTest(publication_failure=fail), tempfile.TemporaryDirectory() as tmp:
                w=Path(tmp);z,base=self.fixture(w); before=z.read_bytes()
                source=w/'source';source.mkdir();(source/'package.json').write_text('{}')
                helper.write(w/'detect.json',{'root':str(source)})
                helper.write(w/'.astro-applied.json',{'old': 'old'})
                old_changes={'schema':'h2wp-changes/1','changes':[{'id':1,'applied':True}], 'changedSinceZip':True,'sinceZip':1}
                helper.write(w/'changes.json',old_changes)
                def run(candidate, log, command, **kwargs):
                    c=candidate
                    if not (c/'static-src').exists():
                        (c/'static-src').mkdir();(c/'static-src/auth.html').write_text('fresh auth')
                        helper.write(c/'conversion-manifest.json',{'site':base['site'],'pages':[{'file':'auth.html'}]})
                        helper.write(c/'prerender-report.json',{'routes':['/auth'],'coverage':{'passed':True},'pages':{}})
                        p=c/'astro-project';(p/'public').mkdir(parents=True);(p/'dist').mkdir()
                        (p/'public/auth.html').write_text('fresh auth');(p/'dist/auth.html').write_text('fresh auth')
                        (p/'package.json').write_text('{"dependencies":{"astro":"5.18.2"}}')
                        (c/'out').mkdir()
                    if any(str(x).endswith('write-result.py') for x in command):
                        artifact=c/'out/new.zip';theme_state.zip_astro_project(c/'astro-project',artifact,'test-astro')
                        helper.write(c/'out/result.json',{**base,'verdict':'Astro checked','astro':{'file':'new.zip','sha256':helper.sha(artifact)}})
                    return 0
                rename=Path.rename; fired=[]
                def fault(src,dest):
                    if fail and src.name=='output-result.json' and not fired:
                        fired.append(True);raise OSError('injected final publication failure')
                    return rename(src,dest)
                with patch.dict(os.environ,{'H2WP_MODE':'repair-delivery','H2WP_TURN':'owner-new','H2WP_OUTPUT_DIR':str(w/'out')}),patch.object(helper,'run',run),patch.object(Path,'rename',fault):
                    self.assertEqual(helper.repair(w),0)
                result=helper.read(w/'out/result.json');changes=helper.read(w/'changes.json')
                self.assertEqual(z.read_bytes(),before)
                if fail:
                    self.assertEqual(result['astro'],base['astro'])
                    self.assertEqual(result['repairs'][-1]['outcome'],'failed')
                    self.assertEqual(changes,old_changes)
                    self.assertEqual(helper.read(w/'.astro-applied.json'),{'old':'old'})
                else:
                    self.assertEqual(result['repairs'][-1]['outcome'],'fixed')
                    self.assertFalse(changes['changedSinceZip']);self.assertEqual(changes['sinceZip'],0)
                    self.assertEqual(changes['changes'],old_changes['changes'])
                    self.assertEqual(changes['lastZip']['file'],result['astro']['file'])
                    with patch.object(change.subprocess,'run') as build:
                        self.assertEqual(change.astro_change(w,SimpleNamespace(what='no edit'),result,{}),3)
                        build.assert_not_called()

    def test_failed_repair_keeps_artifact_and_budget_prevents_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            w=Path(tmp);z,base=self.fixture(w);before=z.read_bytes()
            with patch.dict(os.environ,{'H2WP_MODE':'repair-delivery','H2WP_TURN':'owner-1','H2WP_OUTPUT_DIR':str(w/'out')}):
                self.assertEqual(helper.repair(w),0)
                self.assertEqual(z.read_bytes(),before)
                result=json.loads((w/'out/result.json').read_text())
                self.assertEqual(result['astro'],base['astro']);self.assertTrue(result['recovery']['available'])
                self.assertEqual(result['repairRequestId'],'owner-1')
                with self.assertRaises(RuntimeError):helper.repair(w)
                self.assertEqual(z.read_bytes(),before)
                self.assertEqual(len(json.loads((w/'progress.json').read_text())['repairs']),1)

    def test_counter_only_wrappers_are_preserved_with_fresh_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            w=Path(tmp);z,_=self.fixture(w)
            p=w/'astro-project';(p/'src/pages').mkdir(parents=True);(p/'src/html').mkdir()
            text="---\nimport html from '../html/auth.html?raw';\n---\n<Fragment set:html={html} />\n"
            (p/'src/pages/auth.astro').write_text(text);(p/'src/html/auth.html').write_text('auth')
            self.assertEqual(helper.owner_changes(w,z),[])
            c=w/'candidate';(c/'astro-project/public').mkdir(parents=True)
            (c/'astro-project/public/auth.html').write_text('fresh auth')
            helper.merge_mirror_wrappers(w,z,c)
            self.assertEqual((c/'astro-project/src/pages/auth.astro').read_text(),text)
            self.assertEqual((c/'astro-project/src/html/auth.html').read_text(),'fresh auth')
            (p/'src/html/auth.html').write_text('owner changed body')
            self.assertIn('src/html/auth.html',helper.owner_changes(w,z))

    def test_owner_edits_are_preserved_and_refused_before_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            w=Path(tmp);z,_=self.fixture(w)
            page=w/'astro-project/public/auth.html';page.write_text('owner edit')
            with patch.dict(os.environ,{'H2WP_MODE':'repair-delivery','H2WP_TURN':'owner-1','H2WP_OUTPUT_DIR':str(w/'out')}),patch.object(helper,'run') as run:
                helper.repair(w);run.assert_not_called()
            self.assertEqual(page.read_text(),'owner edit')
            self.assertIn('Owner edits',json.loads((w/'out/result.json').read_text())['couldNotFix'][0]['what'])


if __name__=='__main__': unittest.main()
