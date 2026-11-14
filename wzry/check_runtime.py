"""Check the WZRY runtime without connecting to a device or starting a game."""

import argparse
import hashlib
import importlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def check_assets(root, model_path, verify_hashes=False):
    manifest = json.loads((root / "assets.example.json").read_text(encoding="utf-8"))
    model = Path(model_path)
    if not model.is_absolute():
        model = root / model
    files = [root / "models/start.onnx", root / "models/death.onnx", model]
    errors = []
    for path in files:
        if not path.is_file():
            errors.append("Missing asset: " + str(path))
            continue
        if path.stat().st_size == 0:
            errors.append("Empty asset: " + str(path))
            continue
        if verify_hashes:
            entry = next((item for item in manifest["assets"] if root / item["path"] == path), None)
            if entry is None:
                errors.append("No recorded checksum for: " + str(path))
                continue
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != entry["sha256"]:
                errors.append("Checksum mismatch: " + str(path))
    return errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", default="src/wzry_ai_bc.pt")
    parser.add_argument("--compute-device", default="cuda:0")
    parser.add_argument("--verify-hashes", action="store_true")
    args = parser.parse_args()
    errors = check_assets(ROOT, args.model_path, args.verify_hashes)
    for module in ("onnxruntime", "torch", "numpy", "cv2", "ppocronnx", "PyQt5", "win32gui"):
        try:
            importlib.import_module(module)
        except Exception as exc:
            errors.append("Dependency unavailable: " + module + " (" + type(exc).__name__ + ")")
    if "torch" in sys.modules:
        torch = sys.modules["torch"]
        if args.compute_device.startswith("cuda") and not torch.cuda.is_available():
            errors.append("CUDA unavailable; install a compatible PyTorch build or explicitly select cpu (not performance-validated).")
    print("\n".join(errors) if errors else "WZRY runtime checks passed; no game or device actions performed.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
