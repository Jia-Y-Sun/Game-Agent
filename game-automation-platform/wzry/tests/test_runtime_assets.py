import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from check_runtime import check_assets


class RuntimeAssetTests(unittest.TestCase):
    def test_missing_empty_and_changed_model_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ("models/start.onnx", "models/death.onnx", "src/wzry_ai_bc.pt")
            assets = [{"path": path, "sha256": hashlib.sha256(b"fixture").hexdigest()} for path in paths]
            (root / "assets.example.json").write_text(json.dumps({"assets": assets}), encoding="utf-8")
            self.assertEqual(len(check_assets(root, paths[2])), 3)
            for path in paths:
                target = root / path
                target.parent.mkdir(exist_ok=True)
                target.write_bytes(b"fixture")
            self.assertEqual(check_assets(root, paths[2], verify_hashes=True), [])
            (root / paths[2]).write_bytes(b"changed")
            self.assertIn("Checksum mismatch", check_assets(root, paths[2], True)[0])
            (root / paths[2]).write_bytes(b"")
            self.assertIn("Empty asset", check_assets(root, paths[2])[0])
