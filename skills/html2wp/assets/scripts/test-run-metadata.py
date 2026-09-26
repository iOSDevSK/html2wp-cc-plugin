import sys,tempfile,json,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent/'lib'))
from run_metadata import metadata,section
class Metadata(unittest.TestCase):
 def test_models_and_pause_are_distinguished(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);ws=root/'workspace';ws.mkdir();(root/'.app').mkdir()
   (root/'.app/model-history.json').write_text(json.dumps({'schema':'h2wp-run-context/1','startedAt':'2026-09-26T10:00:00Z','models':[{'model':'gpt-6-luna','effort':'high'},{'model':'gpt-6-sol','effort':'high'}],'turns':[{'startedAt':'2026-09-26T10:00:00Z','endedAt':'2026-09-26T10:01:00Z'},{'startedAt':'2026-09-26T10:05:00Z'}]}))
   m=metadata(ws,'2026-09-26T10:07:00Z');self.assertEqual(m['timing']['wallSeconds'],420);self.assertEqual(m['timing']['agentSeconds'],180);self.assertEqual(len(m['models']),2);self.assertIn('gpt-6-sol',section(m))
 def test_corrupt_optional_metadata_never_blocks_delivery(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);ws=root/'workspace';ws.mkdir();(root/'.app').mkdir()
   for value in (None,[],{'schema':'h2wp-run-context/1','models':True,'turns':[None,7,{}],'startedAt':{} }):
    (root/'.app/model-history.json').write_text(json.dumps(value))
    self.assertIsInstance(metadata(ws),dict)
 def test_old_runs_do_not_invent_a_model(self):
  with tempfile.TemporaryDirectory() as d:
   m=metadata(Path(d),'2026-09-26T10:07:00Z');self.assertEqual(m['models'],[]);self.assertIsNone(m['timing']['wallSeconds']);self.assertIn('not recorded',section(m))
if __name__=='__main__':unittest.main()
