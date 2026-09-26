#!/usr/bin/env python3
"""visual-compare.py: the owner's side-by-side look, on demand, for a UI.

- Every page of the manifest is captured from the source and from the
  WordPress address at 1440 and at 390 (compare-pages.py composites), and the
  index lists each page's key, title, route, both images and a diff %: the
  same page on both sides reads 0%, a changed page more.
- status.json goes running → done; nothing of the run is touched (no
  progress.json, no result.json).
- With no manifest, or no preview WordPress and no address, it fails in its
  own status and exit code only.
- The diff % counts the rows one side has and the other lacks.

A stand-in "WordPress" (a static server) answers the pages, so no Docker is
needed; Playwright is (skipped without it).

  python3 test-visual-compare.py
"""
import functools
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / 'visual-compare.py'
spec = importlib.util.spec_from_file_location('visual_compare', SCRIPT)
vc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vc)

PAGE = ('<!doctype html><html><head><title>{t}</title><style>body{{margin:0;font:20px sans-serif}}'
        'section{{height:600px;background:{bg}}}</style></head><body><h1>{t}</h1><section></section></body></html>')


def composite(path, left, right, width=100):
    from PIL import Image
    board = Image.new('RGB', (2 * width + vc.GAP, vc.CAPTION + max(left[1], right[1])), (24, 24, 24))
    board.paste(Image.new('RGB', (width, left[1]), left[0]), (0, vc.CAPTION))
    board.paste(Image.new('RGB', (width, right[1]), right[0]), (width + vc.GAP, vc.CAPTION))
    board.save(path)
    return path


class Diff(unittest.TestCase):
    def test_partial_failure_keeps_index_and_desktop_only_keeps_mobile(self):
        old={'pages':[{'key':'about','desktop':{'image':'old-d'},'mobile':{'image':'old-m'}},{'key':'home','desktop':{'image':'home'}}]}
        current={'pages':[{'key':'about','desktop':{'image':'new-d'},'mobile':{'error':'failed'}}]}
        with self.assertRaises(RuntimeError):vc.merge_selected(old,current,'about')
        self.assertEqual(old['pages'][0]['mobile']['image'],'old-m')
        current['pages'][0].pop('mobile')
        merged=vc.merge_selected(old,current,'about',('desktop',))
        self.assertEqual(merged['pages'][0]['mobile']['image'],'old-m')
        self.assertEqual(merged['pages'][1],old['pages'][1])

    def test_the_diff_percent(self):
        with tempfile.TemporaryDirectory() as tmp:
            same = composite(Path(tmp) / 's.png', ((250, 250, 250), 200), ((250, 250, 250), 200))
            self.assertEqual(vc.diff_percent(same, 100, (200, 200)), 0.0)
            other = composite(Path(tmp) / 'o.png', ((250, 250, 250), 200), ((10, 10, 200), 200))
            self.assertEqual(vc.diff_percent(other, 100, (200, 200)), 100.0)
            taller = composite(Path(tmp) / 't.png', ((250, 250, 250), 200), ((250, 250, 250), 400))
            self.assertEqual(vc.diff_percent(taller, 100, (200, 400)), 50.0)


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'playwright is not installed')
class OnDemand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws, src, wp = root / 'work space', root / 'source', root / 'wordpress'
        for d in (self.ws, src, wp / 'about'):
            d.mkdir(parents=True)
        (src / 'index.html').write_text(PAGE.format(t='Home', bg='#eee'))
        (src / 'about.html').write_text(PAGE.format(t='About', bg='#eee'))
        (wp / 'index.html').write_text(PAGE.format(t='Home', bg='#eee'))
        (wp / 'about' / 'index.html').write_text(PAGE.format(t='About', bg='#c33'))
        (self.ws / 'conversion-manifest.json').write_text(json.dumps({
            'schema': 'html2wp/1', 'site': {'name': 'T', 'slug': 't'}, 'input': {'dir': str(src)},
            'workspace': str(self.ws), 'pages': [
                {'file': 'index.html', 'key': 'front-page', 'kind': 'front', 'title': 'Home', 'chrome': 'consensus'},
                {'file': 'about.html', 'key': 'about', 'kind': 'page', 'title': 'About', 'chrome': 'consensus'}]}))
        handler = functools.partial(type('Q', (SimpleHTTPRequestHandler,), {'log_message': lambda *a: None}), directory=str(wp))
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.wp = f'http://127.0.0.1:{self.httpd.server_port}'

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def run_it(self, *extra):
        return subprocess.run([sys.executable, str(SCRIPT), str(self.ws), '--jobs', '1', *extra],
                              capture_output=True, text=True, timeout=600)

    def test_every_page_at_both_widths_with_an_index_and_a_status(self):
        done = self.run_it('--wp', self.wp)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        status = json.loads((self.ws / 'visual-review' / 'status.json').read_text())
        self.assertEqual((status['state'], status['index']), ('done', 'visual-review/visual-compare.json'))
        index = json.loads((self.ws / 'visual-review' / 'visual-compare.json').read_text())
        pages = {p['key']: p for p in index['pages']}
        self.assertEqual(set(pages), {'front-page', 'about'})
        self.assertEqual((pages['about']['title'], pages['about']['route']), ('About', self.wp + '/about/'))
        for key in pages:
            for width in ('desktop', 'mobile'):
                self.assertTrue((self.ws / pages[key][width]['image']).is_file(), (key, width))
        self.assertEqual(pages['about']['mobile']['image'], 'visual-review/mobile/about.side-by-side.png')
        self.assertLess(pages['front-page']['desktop']['diffPercent'], 1.0)
        self.assertGreater(pages['about']['desktop']['diffPercent'], 20.0)
        self.assertFalse((self.ws / 'progress.json').exists())
        self.assertFalse((self.ws / 'result.json').exists())

    def test_selected_page_preserves_other_images_and_index(self):
        done = self.run_it('--wp', self.wp, '--desktop-only')
        self.assertEqual(done.returncode, 0, done.stdout+done.stderr)
        path = self.ws/'visual-review/visual-compare.json'
        before=json.loads(path.read_text())
        front=next(p for p in before['pages'] if p['key']=='front-page')
        image=self.ws/front['desktop']['image'];stamp=image.stat().st_mtime_ns;data=image.read_bytes()
        done=self.run_it('--wp',self.wp,'--desktop-only','--page-key','about')
        self.assertEqual(done.returncode,0,done.stdout+done.stderr)
        after=json.loads(path.read_text());self.assertEqual(next(p for p in after['pages'] if p['key']=='front-page'),front)
        self.assertEqual((image.stat().st_mtime_ns,image.read_bytes()),(stamp,data))
        about=next(p for p in after['pages'] if p['key']=='about')
        self.assertIn('/refresh-',about['desktop']['image'])
        stable=path.read_bytes()
        failed=self.run_it('--wp',self.wp,'--page-key','unknown')
        self.assertNotEqual(failed.returncode,0);self.assertEqual(path.read_bytes(),stable)

    def test_astro_selected_page_keeps_other_captures(self):
        import shutil
        manifest=json.loads((self.ws/'conversion-manifest.json').read_text())
        dist=self.ws/'astro-project/dist';shutil.copytree(Path(manifest['input']['dir']),dist)
        done=self.run_it('--target','astro','--desktop-only')
        self.assertEqual(done.returncode,0,done.stdout+done.stderr)
        path=self.ws/'visual-review/visual-compare.json';before=json.loads(path.read_text())
        front=next(p for p in before['pages'] if p['key']=='front-page')
        done=self.run_it('--target','astro','--desktop-only','--page-key','about')
        self.assertEqual(done.returncode,0,done.stdout+done.stderr)
        after=json.loads(path.read_text());self.assertEqual(next(p for p in after['pages'] if p['key']=='front-page'),front)
        self.assertIn('/refresh-',next(p for p in after['pages'] if p['key']=='about')['desktop']['image'])

    def test_it_fails_in_its_own_status_only(self):
        done = self.run_it()   # no --wp and no preview WordPress for this workspace
        self.assertEqual(done.returncode, 1)
        status = json.loads((self.ws / 'visual-review' / 'status.json').read_text())
        self.assertEqual(status['state'], 'failed')
        self.assertIn('no preview WordPress', status['note'])
        (self.ws / 'conversion-manifest.json').unlink()
        self.assertEqual(self.run_it('--wp', self.wp).returncode, 1)
        self.assertIn('nothing converted', json.loads((self.ws / 'visual-review' / 'status.json').read_text())['note'])


if __name__ == '__main__':
    unittest.main()
