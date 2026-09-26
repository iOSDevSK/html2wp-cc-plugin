#!/usr/bin/env python3
"""Exercise actual test-env.sh with a fake Docker; never touch real previews."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'lib'))
from preview_owner import identity, available

FAKE = r'''#!/usr/bin/env python3
import json,os,sys,re
from pathlib import Path
p=Path(os.environ['FAKE_DOCKER_STATE']); db=json.loads(p.read_text()); a=sys.argv[1:]
with open(os.environ['FAKE_DOCKER_LOG'],'a') as f: f.write(json.dumps(a)+'\n')
projects=db['projects']
selected=next((x.split('=',2)[-1] for x in a if x.startswith('label=com.docker.compose.project=')),None)
name_filter=next((x[5:] for x in a if x.startswith('name=')),None)
if a[0]=='ps':
 for project,v in projects.items():
  if selected and project!=selected: continue
  if '{{.Label "com.docker.compose.project"}}' in a: print(project)
  else:
   for service in v.get('services',('wp','db')):
    name=project+'-'+service+'-1'
    if not name_filter or re.search(name_filter,'/'+name):print(name)
elif a[0]=='inspect':
 fmt=a[a.index('--format')+1]
 if 'NetworkSettings' in fmt:
  print(json.dumps({x+'_default':{} for x in db.get('attached',[])}))
 else:
  for name in a[a.index('--format')+2:]:
   project=name.rsplit('-',2)[0]; v=projects[project]
   labels={'com.docker.compose.project':project,'h2wp.state':v.get('state','')}
   owner=v.get('owner','')
   if name.endswith('-db-1'):owner=v.get('dbOwner',owner)
   if owner:labels['h2wp.owner']=owner
   print(json.dumps({'name':'/'+name,'labels':labels}))
elif a[0] in ('volume','network') and a[1]=='ls':
 v=projects.get(selected,{})
 if v.get('resourceOwner') is not None:print(selected+'_'+a[0])
elif a[0] in ('volume','network') and a[1]=='inspect':
 name=a[-1]; project=name.rsplit('_',1)[0]
 print(json.dumps({'h2wp.owner':projects[project]['resourceOwner']}))
elif a[0]=='compose':
 project=a[a.index('-p')+1]
 if 'up' in a:sys.exit(77) # ownership branch verified; don't simulate WordPress installation
 if 'down' in a and not db.get('keepAfterDown'):
  projects.pop(project,None);p.write_text(json.dumps(db))
elif a[0]=='rm':
 for name in a[2:]:projects.pop(name.rsplit('-',2)[0],None)
 p.write_text(json.dumps(db))
'''


class PreviewOwnership(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / 'workspace';self.ws.mkdir()
        self.bin = self.root / 'bin';self.bin.mkdir()
        script = self.bin / 'docker';script.write_text(FAKE);script.chmod(0o755)
        self.data = self.root / 'docker.json';self.log = self.root / 'calls.jsonl'
        self.container = 'h2wpd-project-A-agent'
        self.project = 'h2wp-maison-aaaaaa';self.other='h2wp-maison-bbbbbb'
        self.state = self.ws / '.test-env-maison.json'
        self.env = {**os.environ, 'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                    'H2WP_WORKSPACE': str(self.ws), 'H2WP_CONTAINER': self.container,
                    'FAKE_DOCKER_STATE': str(self.data), 'FAKE_DOCKER_LOG': str(self.log)}
        self.db = {'projects': {}};self.save()

    def save(self):self.data.write_text(json.dumps(self.db))

    def add(self, project=None, owner=None, path='/project/workspace/.test-env-maison.json', **extras):
        self.db['projects'][project or self.project]={'owner':owner if owner is not None else identity(self.ws,self.container),
                                                    'state':path,**extras};self.save()

    def write_state(self, project=None, owner=True, container=None):
        project=project or self.project
        d={'slug':'maison','project':project,'wpContainer':project+'-wp-1','dbContainer':project+'-db-1',
           'network':project+'_default','url':'http://localhost:12345',
           'relay':{'container':container or self.container}}
        if owner:d['owner']=identity(self.ws,self.container)
        self.state.write_text(json.dumps(d));return d

    def run_cmd(self, action, *args):
        return subprocess.run(['bash',str(HERE/'test-env.sh'),action,'maison',*args],
                              env=self.env,cwd=self.root,capture_output=True,text=True,timeout=10)

    def calls(self):return [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []

    def destructive(self):
        return [a for a in self.calls() if (a[0]=='compose' and 'down' in a) or a[0]=='rm'
                or (a[0] in ('network','volume') and a[1] in ('rm','disconnect'))]

    def test_up_does_not_remove_another_container_with_identical_state_alias(self):
        self.add(self.other,owner=identity(self.ws,'h2wpd-project-B-agent'))
        r=self.run_cmd('up')
        self.assertEqual(r.returncode,77,r.stdout+r.stderr)
        self.assertFalse(self.destructive())
        self.assertIn(self.other,json.loads(self.data.read_text())['projects'])

    def test_up_legacy_orphan_with_same_alias_is_left_alone(self):
        self.add(self.other,owner='')
        r=self.run_cmd('up')
        self.assertEqual(r.returncode,77,r.stdout+r.stderr)
        self.assertFalse(self.destructive())

    def test_down_missing_state_only_removes_new_proven_owner(self):
        self.add();self.add(self.other,owner=identity(self.ws,'other-agent'))
        r=self.run_cmd('down')
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(set(json.loads(self.data.read_text())['projects']),{self.other})

    def test_down_preserves_legacy_orphan_even_with_canonical_host_path(self):
        self.env.pop('H2WP_CONTAINER')
        self.add(owner='',path=str(self.state))
        self.assertEqual(self.run_cmd('down').returncode,0)
        self.assertFalse(self.destructive())

    def test_up_replaces_only_own_orphan(self):
        self.add();self.add(self.other,owner='foreign')
        r=self.run_cmd('up')
        self.assertEqual(r.returncode,77,r.stdout+r.stderr)
        self.assertEqual(set(json.loads(self.data.read_text())['projects']),{self.other})

    def test_workspace_env_wins_over_project_root_cwd(self):
        self.add();self.write_state()
        r=self.run_cmd('down')
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertFalse(self.state.exists())
        self.assertFalse((self.root/'.test-env-maison.json').exists())

    def test_shop_manifest_uses_workspace_even_when_called_from_project_root(self):
        import re
        source=(HERE/'test-env.sh').read_text()
        functions='\n'.join(re.search(r'^'+name+r'\(\) \{.*?^\}',source,re.M|re.S).group(0)
                            for name in ('workspace_root','manifest_path'))
        (self.ws/'conversion-manifest.json').write_text('{"shop":{"present":true}}')
        (self.root/'conversion-manifest.json').write_text('{"shop":{"present":false}}')
        env=dict(self.env);env.pop('TEST_ENV_MANIFEST',None)
        r=subprocess.run(['bash','-c',functions+'\ncat "$(manifest_path)"'],env=env,
                         cwd=self.root,capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertTrue(json.loads(r.stdout)['shop']['present'])
        env['TEST_ENV_MANIFEST']='conversion-manifest.json'
        r=subprocess.run(['bash','-c',functions+'\ncat "$(manifest_path)"'],env=env,
                         cwd=self.root,capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertFalse(json.loads(r.stdout)['shop']['present'])

    def test_bad_explicit_workspace_never_falls_back_to_cwd(self):
        self.env['H2WP_WORKSPACE']=str(self.root/'missing')
        self.add()
        self.assertNotEqual(self.run_cmd('down').returncode,0)
        self.assertFalse(self.calls())

    def test_state_pointing_at_other_owner_refuses_up_down_reset_check_clone(self):
        self.add(owner='foreign');self.write_state()
        for action,args in [('up',()),('down',()),('reset',()),('check',()),('clone',('maison-b',))]:
            with self.subTest(action=action):
                r=self.run_cmd(action,*args)
                self.assertNotEqual(r.returncode,0,r.stdout+r.stderr)
                self.assertIn('REFUSING',r.stderr)
        self.assertFalse(self.destructive());self.assertTrue(self.state.exists())

    def test_conflicting_wp_db_owners_refuse(self):
        self.add(dbOwner='foreign');self.write_state()
        self.assertNotEqual(self.run_cmd('down').returncode,0)
        self.assertFalse(self.destructive())

    def test_foreign_volume_or_network_owner_refuses(self):
        self.add(resourceOwner='foreign');self.write_state()
        self.assertNotEqual(self.run_cmd('down').returncode,0)
        self.assertFalse(self.destructive())

    def test_copied_state_owner_and_container_names_refuse(self):
        self.add();d=self.write_state()
        d['owner']=identity(self.ws,'other');self.state.write_text(json.dumps(d))
        self.assertNotEqual(self.run_cmd('down').returncode,0)
        d['owner']=identity(self.ws,self.container);d['wpContainer']=self.other+'-wp-1'
        self.state.write_text(json.dumps(d))
        self.assertNotEqual(self.run_cmd('down').returncode,0)
        self.assertFalse(self.destructive())

    def test_legacy_with_state_and_matching_runtime_reuses_without_recreation(self):
        self.add(owner='',path=str(self.state));self.write_state(owner=False)
        self.db['attached']=[self.project];self.save()
        r=self.run_cmd('up')
        self.assertEqual(r.returncode,77,r.stdout+r.stderr)
        up=next(a for a in self.calls() if a[0]=='compose' and 'up' in a)
        self.assertIn('--no-recreate',up);self.assertFalse(self.destructive())

    def test_legacy_wrong_runtime_is_not_adopted(self):
        self.add(owner='',path=str(self.state));self.write_state(owner=False,container='another-agent')
        self.assertNotEqual(self.run_cmd('up').returncode,0)
        self.assertFalse(any(a[0]=='compose' for a in self.calls()))

    def test_state_kept_when_teardown_does_not_finish(self):
        self.add();self.write_state();self.db['keepAfterDown']=True;self.save()
        self.assertNotEqual(self.run_cmd('down').returncode,0)
        self.assertTrue(self.state.exists())

    def test_info_uses_workspace_not_conflicting_cwd_state(self):
        self.write_state()
        (self.root/'.test-env-maison.json').write_text('{"url":"http://localhost:99999"}')
        r=self.run_cmd('info','url')
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(r.stdout.strip(),'http://localhost:12345')

    def test_legacy_reuse_after_lost_network_attachment(self):
        self.add(owner='',path=str(self.state));self.write_state(owner=False)
        self.db['attached']=[];self.save()
        self.assertEqual(self.run_cmd('up').returncode,77)
        self.assertFalse(self.destructive())

    def test_allocation_collision_retries_instead_of_adopting_foreign_preview(self):
        # Invoke the actual shell allocation function with a deterministic ID stream.
        self.add(owner='foreign')
        source=(HERE/'test-env.sh').read_text()
        import re
        functions='\n'.join(re.search(r'^'+name+r'\(\) \{.*?^\}',source,re.M|re.S).group(0)
                            for name in ('workspace_root','project_available','new_project'))
        functions+='\ngen_run_id() { if [ ! -f "$SEQ" ]; then touch "$SEQ"; echo aaaaaa; else echo cccccc; fi; }\nnew_project maison\n'
        env={**self.env,'SCRIPT_DIR':str(HERE),'SEQ':str(self.root/'sequence')}
        r=subprocess.run(['bash','-c',functions],env=env,cwd=self.root,capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(r.stdout,'h2wp-maison-cccccc')
        self.assertFalse(self.destructive())

    def test_allocation_checks_surviving_volumes_and_networks(self):
        with patch('preview_owner.docker',side_effect=['','leftover-volume']):
            self.assertFalse(available(self.project))
        with patch('preview_owner.docker',side_effect=['','','leftover-network']):
            self.assertFalse(available(self.project))

    def test_absent_project_state_cannot_reference_foreign_live_containers(self):
        self.add(self.other,owner='foreign');d=self.write_state()
        d['wpContainer']=self.other+'-wp-1';d['dbContainer']=self.other+'-db-1'
        self.state.write_text(json.dumps(d))
        for action,args in [('reset',()),('clone',('maison-b',))]:
            self.assertNotEqual(self.run_cmd(action,*args).returncode,0)
        self.assertFalse(any(a[0]=='exec' for a in self.calls()))
        self.assertFalse(self.destructive())

    def test_partial_modern_preview_can_recreate_missing_own_container(self):
        self.add(services=['db']);self.write_state()
        r=self.run_cmd('up')
        self.assertEqual(r.returncode,77,r.stdout+r.stderr)
        self.assertFalse(self.destructive())

    def test_named_reference_under_wrong_compose_label_is_refused(self):
        import preview_owner
        self.write_state()
        calls=['',self.project+'-wp-1','', '', '',
               json.dumps({'name':'/'+self.project+'-wp-1', 'labels':{'com.docker.compose.project':self.other,'h2wp.owner':'foreign'}})]
        with patch('preview_owner.docker',side_effect=calls):
            self.assertFalse(preview_owner.owned(self.ws,self.container,self.project,self.state))

    def test_owner_uses_physical_workspace_but_also_runtime_identity(self):
        alias=self.root/'alias';alias.symlink_to(self.ws,target_is_directory=True)
        self.assertEqual(identity(alias,'A'),identity(self.ws,'A'))
        self.assertNotEqual(identity(alias,'A'),identity(alias,'B'))


if __name__=='__main__':unittest.main()
