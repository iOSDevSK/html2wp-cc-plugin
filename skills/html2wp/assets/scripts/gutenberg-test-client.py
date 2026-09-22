#!/usr/bin/env python3
"""Exercise client cache identity/recreation against actual loopback HTTP servers."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import shutil
import tarfile
import tempfile
import threading
import unittest

CLIENT=Path(__file__).with_name('convert-remote.sh')


class Service:
    def __init__(self):
        self.jobs=0
        self.transforms=0
        self.refusals=[]
        self.always=None
        self.put_status=200
        self.no_download=False
        stream=io.BytesIO()
        with tarfile.open(fileobj=stream,mode='w:gz') as archive:
            info=tarfile.TarInfo('theme/demo/style.css')
            data=b'/* Theme Name: Demo */'
            info.size=len(data)
            archive.addfile(info,io.BytesIO(data))
        self.archive=stream.getvalue()
        service=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def reply(self,status,payload):
                raw=json.dumps(payload).encode()
                self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length','0')))
                if self.path=='/v1/jobs':
                    service.jobs+=1
                    self.reply(201,{'job':'job-'+str(service.jobs),'token':'local-token','upload':{'url':service.url+'/upload'},'edition':'pro'})
                elif self.path.endswith('/transform'):
                    service.transforms+=1
                    refusal=service.refusals.pop(0) if service.refusals else service.always
                    if refusal:
                        status,reason=refusal
                        self.reply(status,{'error':'test refusal','reason':reason})
                    elif service.no_download:
                        self.reply(200,{'ok':False,'stage':'make-theme','message':'recorded generator failure'})
                    else:
                        self.reply(200,{'ok':True,'slug':'demo','downloads':{'theme':service.url+'/theme'},'checksums':{'theme':hashlib.sha256(service.archive).hexdigest()}})
                else:self.reply(404,{})
            def do_PUT(self):
                self.rfile.read(int(self.headers.get('Content-Length','0')))
                self.reply(service.put_status,{'ok':service.put_status==200})
            def do_GET(self):
                self.send_response(200);self.send_header('Content-Length',str(len(service.archive)));self.end_headers();self.wfile.write(service.archive)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.url='http://127.0.0.1:'+str(self.server.server_port)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join()


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.ws=Path(self.temp.name);self.services=[]
        (self.ws/'astro-project/dist').mkdir(parents=True)
        (self.ws/'astro-project/dist/index.html').write_text('<h1>Source</h1>')
        (self.ws/'astro-report.json').write_text('{}')
        (self.ws/'conversion-manifest.json').write_text(json.dumps({'site':{'slug':'demo'},'pages':[{'key':'home','file':'index.html','kind':'front'}]}))
    def tearDown(self):
        for service in self.services:service.close()
        self.temp.cleanup()
    def service(self):
        service=Service();self.services.append(service);return service
    def run_client(self,service,ok=True,**extra):
        env=dict(os.environ);env.pop('H2WP_RETRIED',None);env.pop('H2WP_STRICT_JOBS',None);env.pop('H2WP_JOB_STATE',None);env.update(extra)
        result=subprocess.run(['bash',str(CLIENT),str(self.ws),'--api='+service.url,'--key=local-test'],text=True,capture_output=True,env=env,timeout=20)
        self.assertEqual(result.returncode==0,ok,result.stdout+result.stderr)
        return result
    def test_native_rebuild_never_calls_legacy_packager(self):
        scripts=self.ws/'scripts';scripts.mkdir()
        rebuild=scripts/'rebuild-theme.sh'
        shutil.copyfile(CLIENT.with_name('rebuild-theme.sh'),rebuild)
        (scripts/'convert-remote.sh').write_text('#!/usr/bin/env bash\nexit 0\n')
        (scripts/'make-screenshot.py').write_text('raise RuntimeError("legacy screenshot must not run")\n')
        (scripts/'make-zip.sh').write_text('#!/usr/bin/env bash\nexit 99\n')
        manifest=self.ws/'conversion-manifest.json'
        value=json.loads(manifest.read_text());value.update(schema='html2wp/2',target='gutenberg',workspace=str(self.ws));manifest.write_text(json.dumps(value))
        result=subprocess.run(['bash',str(rebuild),'--manifest='+str(manifest),'--api=http://127.0.0.1:1'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('gutenberg-package.py',result.stdout)
        self.assertFalse(list(self.ws.glob('*.zip')))
        result=subprocess.run(['bash',str(rebuild),'--manifest='+str(manifest),'--opts={"stripInFront":["footer"]}'],capture_output=True,text=True)
        self.assertEqual(result.returncode,2,result.stdout+result.stderr)
        self.assertIn('legacy --opts are not supported',result.stderr)

    def test_same_api_reuses_job(self):
        service=self.service();self.run_client(service);self.run_client(service)
        self.assertEqual(service.jobs,1);self.assertEqual(service.transforms,2)
        self.assertEqual(json.loads((self.ws/'.h2wp-job.json').read_text())['api'],service.url)
    def test_different_api_opens_new_job(self):
        first=self.service();second=self.service();self.run_client(first);self.run_client(second)
        self.assertEqual(first.jobs,1);self.assertEqual(second.jobs,1)
    def test_old_state_without_api_is_not_reused(self):
        service=self.service();self.run_client(service)
        path=self.ws/'.h2wp-job.json';state=json.loads(path.read_text());state.pop('api');path.write_text(json.dumps(state))
        self.run_client(service);self.assertEqual(service.jobs,2)
    def test_403_recreates_once(self):
        service=self.service();self.run_client(service);service.refusals=[(403,'expired')]
        self.run_client(service);self.assertEqual(service.jobs,2);self.assertEqual(service.transforms,3)
    def test_superseded_recreates_once(self):
        service=self.service();self.run_client(service);service.refusals=[(409,'superseded')]
        self.run_client(service);self.assertEqual(service.jobs,2);self.assertEqual(service.transforms,3)
    def test_repeated_403_is_bounded(self):
        service=self.service();self.run_client(service);service.always=(403,'expired')
        self.run_client(service,ok=False);self.assertEqual(service.jobs,2);self.assertEqual(service.transforms,3)
    def test_other_409_does_not_recreate(self):
        service=self.service();self.run_client(service);service.always=(409,'other-conflict')
        self.run_client(service,ok=False);self.assertEqual(service.jobs,1);self.assertEqual(service.transforms,2)

    # The desktop app's former patches, now native: strict jobs, the pending
    # marker, a resumed upload, the kept answer and a 200 without a theme.
    def outcome(self):
        return json.loads((self.ws/'.h2wp-result.json').read_text())
    def test_result_records_the_target(self):
        # The service's rule: html unless the v2 schema or target says gutenberg.
        service=self.service();self.run_client(service)
        self.assertEqual(self.outcome()['target'],'html')
        manifest=self.ws/'conversion-manifest.json'
        value=json.loads(manifest.read_text());value['target']='gutenberg';manifest.write_text(json.dumps(value))
        self.run_client(service)
        self.assertEqual(self.outcome()['target'],'gutenberg')
    def test_strict_expired_fails_without_new_job(self):
        service=self.service();self.run_client(service);service.refusals=[(403,'expired')]
        self.run_client(service,ok=False,H2WP_STRICT_JOBS='1')
        self.assertEqual(service.jobs,1);self.assertEqual(self.outcome()['code'],'JOB_EXPIRED')
        self.assertTrue((self.ws/'.h2wp-job.json').exists())
    def test_strict_superseded_fails_without_new_job(self):
        service=self.service();self.run_client(service);service.refusals=[(409,'superseded')]
        self.run_client(service,ok=False,H2WP_STRICT_JOBS='1');self.assertEqual(service.jobs,1)
    def test_pending_marker(self):
        service=self.service();(self.ws/'.h2wp-job.json.pending').write_text('x')
        self.run_client(service,ok=False,H2WP_STRICT_JOBS='1')
        self.assertEqual(service.jobs,0);self.assertEqual(self.outcome()['code'],'JOB_RECOVERY_REQUIRED')
        self.run_client(service);self.assertEqual(service.jobs,1)
        self.assertFalse((self.ws/'.h2wp-job.json.pending').exists())
    def test_interrupted_upload_resumes_same_job(self):
        service=self.service();service.put_status=500
        self.run_client(service,ok=False)
        state=json.loads((self.ws/'.h2wp-job.json').read_text())
        self.assertEqual((state['phase'],state['api']),('uploading',service.url))
        service.put_status=200;self.run_client(service)
        self.assertEqual(service.jobs,1);self.assertEqual(service.transforms,1)
        self.assertNotIn('phase',json.loads((self.ws/'.h2wp-job.json').read_text()))
    def test_resumed_upload_of_a_lost_job_renews_once(self):
        service=self.service();service.put_status=500;self.run_client(service,ok=False)
        service.put_status=404;self.run_client(service,ok=False);self.assertEqual(service.jobs,2)
    def test_200_without_theme_is_a_failure_and_answer_is_kept(self):
        service=self.service();service.no_download=True
        state=self.ws/'private/job.json';state.parent.mkdir()
        self.run_client(service,ok=False,H2WP_JOB_STATE=str(state))
        outcome=self.outcome()
        self.assertEqual((outcome['code'],outcome['stage']),('TRANSFORM_FAILED','make-theme'))
        self.assertEqual(json.loads(Path(str(state)+'.result').read_text())['message'],'recorded generator failure')
        self.assertFalse((self.ws/'.h2wp-job.json').exists())
    def test_unchanged_input_packs_identical_bytes(self):
        service=self.service();self.run_client(service)
        first=json.loads((self.ws/'.h2wp-job.json').read_text())['sha']
        os.utime(self.ws/'astro-report.json',(1,1))
        self.run_client(service);self.assertEqual(service.jobs,1)
        self.assertEqual(json.loads((self.ws/'.h2wp-job.json').read_text())['sha'],first)


if __name__=='__main__':unittest.main()
