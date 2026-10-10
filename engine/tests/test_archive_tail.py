import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from archive_tail import retained_tail_paths

class ArchiveTail(unittest.TestCase):
 def test_cursor_preserves_complete_suffix_and_skips_uncreated_payload(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d)
   for i in range(1,7):
    p=root/f'{i:08d}';p.mkdir()
    if i!=6:(p/'inputs.sli').write_bytes(b'frame')
   self.assertEqual([p.parent.name for p in retained_tail_paths(root)],['00000004','00000005'])
   self.assertEqual([p.parent.name for p in retained_tail_paths(root,'00000003')],['00000003','00000004','00000005'])
   (root/'00000006/inputs.sli').write_bytes(b'next')
   self.assertEqual([p.parent.name for p in retained_tail_paths(root,'00000005')],['00000005','00000006'])
 def test_old_segment_payloads_are_never_statted_or_opened(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d)
   for i in range(1,401):
    p=root/f'{i:08d}';p.mkdir();(p/'inputs.sli').touch()
   seen=[];original=Path.is_file
   def probe(p):seen.append(p.parent.name);return original(p)
   with patch.object(Path,'is_file',probe):paths=retained_tail_paths(root,'00000399')
   self.assertEqual(seen,['00000399','00000400']);self.assertEqual(len(paths),2)
 def test_initial_tail_ignores_foreign_names_and_symlink_directories(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d)
   for name in ['00000001','00000002','00000003','foreign']:
    p=root/name;p.mkdir();(p/'inputs.sli').touch()
   (root/'00000004').symlink_to(root/'00000001',target_is_directory=True)
   self.assertEqual([p.parent.name for p in retained_tail_paths(root)],['00000002','00000003'])
