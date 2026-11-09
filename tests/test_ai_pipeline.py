import importlib.util, json, tempfile, unittest, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys_ai=ROOT/"plugins/endstone_zp_anticheat/ai"
sys.path.insert(0,str(sys_ai))
def load(name,path):
 spec=importlib.util.spec_from_file_location(name,path); mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod
onnx=load("zp_onnx",sys_ai/"onnx_engine.py")
dataset=load("zp_dataset",sys_ai/"dataset.py")
class PipelineTests(unittest.TestCase):
 def test_collector_writes_pseudonymous_unlabelled_sample(self):
  with tempfile.TemporaryDirectory() as folder:
   c=dataset.BehaviorDatasetCollector(Path(folder)/"d.jsonl",interval_seconds=10)
   f={k:1.0 for k in onnx.FEATURE_ORDER}
   self.assertTrue(c.record("uuid-example",f,1000))
   self.assertFalse(c.record("uuid-example",f,1001))
   row=json.loads((Path(folder)/"d.jsonl").read_text())
   self.assertNotIn("uuid",row); self.assertNotIn("name",row); self.assertIsNone(row["label"])
   self.assertEqual(len(row["player_key"]),64)
 def test_missing_model_is_safe(self):
  with tempfile.TemporaryDirectory() as folder:
   m=onnx.OnnxBehaviorModel(Path(folder)/"missing.onnx")
   self.assertEqual(m.status,"model_missing"); self.assertIsNone(m.predict({}))
if __name__=="__main__": unittest.main()
