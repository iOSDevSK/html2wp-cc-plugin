#!/usr/bin/env python3
import ast
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parent/'lib'))
from preview_editor import ensure
from editor_install import install

class EditorTests(unittest.TestCase):
    def run_for(self, before, after=None, failed=False):
        def run(args, **kwargs):
            if 'activate' in args:
                return subprocess.CompletedProcess(args, 1 if failed else 0, '', '')
            items=before if run.count==0 or after is None else after
            run.count+=1
            return subprocess.CompletedProcess(args,0,json.dumps([{'name':k,'status':v} for k,v in items.items()]),'')
        run.count=0
        return Mock(side_effect=run)
    def test_active_is_idempotent(self):
        run=self.run_for({'visual-edit-lite':'active'})
        self.assertFalse(ensure('own-wp',run)['changed']);self.assertEqual(run.call_count,1)
    def test_inactive_becomes_verified_active(self):
        run=self.run_for({'visual-edit-lite':'inactive'},{'visual-edit-lite':'active'})
        self.assertTrue(ensure('own-wp',run)['changed']);self.assertEqual(run.call_count,3)
    def test_pro_is_preserved(self):
        run=self.run_for({'visual-edit':'active','visual-edit-lite':'inactive'})
        self.assertEqual(ensure('own-wp',run)['plugin'],'visual-edit');self.assertEqual(run.call_count,1)
    def test_missing_is_not_a_command_failure(self):
        self.assertEqual(ensure('own-wp',self.run_for({}))['status'],'not_installed')
        with self.assertRaises(RuntimeError):ensure('own-wp',Mock(return_value=subprocess.CompletedProcess([],1,'','database error')))
    def test_activation_failure_and_false_success(self):
        for run in (self.run_for({'visual-edit-lite':'inactive'},failed=True),self.run_for({'visual-edit-lite':'inactive'})):
            with self.assertRaises(RuntimeError):ensure('own-wp',run)
    def test_unowned_preview_never_reaches_activation(self):
        import importlib.util,tempfile
        from unittest.mock import patch
        path=Path(__file__).parent/'ensure-preview-editor.py'
        spec=importlib.util.spec_from_file_location('ensure_preview',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as d:
            state=Path(d)/'.test-env-site.json';state.write_text('{"project":"h2wp-site-aaaaaa","wpContainer":"foreign"}')
            with patch.object(sys,'argv',['ensure','--env',str(state)]),patch.object(module,'owned',return_value=False),patch.object(module,'ensure') as action:
                with self.assertRaises(RuntimeError):module.main()
                action.assert_not_called()

    def test_comparison_has_no_plugin_mutations(self):
        tree=ast.parse((Path(__file__).parent/'visual-compare.py').read_text())
        values={n.value for n in ast.walk(tree) if isinstance(n,ast.Constant) and isinstance(n.value,str)}
        self.assertNotIn('deactivate',values);self.assertNotIn('activate',values)
    def test_admin_ui_active_inactive_and_missing(self):
        for initial in ('active','inactive','absent'):
            page=FakePage(initial)
            def step(page,name,ok,*args,**kwargs):self.assertTrue(ok,name)
            install(page,'http://localhost:1234','editor.zip',step)
            self.assertEqual(page.state,'active')
            self.assertEqual(page.uploads,1 if initial=='absent' else 0)
            install(page,'http://localhost:1234','editor.zip',step)
            self.assertEqual(page.uploads,1 if initial=='absent' else 0)

class FakePage:
    def __init__(self,state):self.state=state;self.uploads=0
    def goto(self,url):pass
    def locator(self,selector):return Locator(self,selector)
    def set_input_files(self,*args):self.uploads+=1
    def expect_navigation(self,**kwargs):return self
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def click(self,selector):self.state='inactive'
    def content(self):return 'Plugin installed successfully'
class Locator:
    def __init__(self,page,selector):self.page=page;self.selector=selector;self.first=self
    def locator(self,s):return Locator(self.page,self.selector+' '+s)
    def count(self):
        if 'data-slug="visual-edit"' in self.selector:return 0
        if 'action=deactivate' in self.selector:return int(self.page.state=='active')
        if 'action=activate' in self.selector:return int(self.page.state=='inactive')
        return int(self.page.state!='absent')
    def click(self):self.page.state='active'
if __name__=='__main__':unittest.main()
