import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from unicapture.qoe_postprocess.perception import OCRPerception


class WzryPerceptionTests(unittest.TestCase):
    def make_perception(self, app_name, results):
        engine = Mock(return_value=(results, None))
        module = SimpleNamespace(RapidOCR=lambda: engine)
        with patch.dict(sys.modules, {"rapidocr_onnxruntime": module}):
            perception = OCRPerception(app_name=app_name, scene="battle")
        return perception, engine

    def test_wzry_roi_accepts_both_ocr_result_formats(self):
        box = [[0, 0], [10, 0], [10, 10], [0, 10]]
        for result in (["35ms", 0.99], [box, "35ms", 0.99]):
            with self.subTest(result=result):
                perception, engine = self.make_perception("honorofkings", [result])
                observed = perception.analyze(np.zeros((1080, 1920, 3), dtype=np.uint8))
                self.assertEqual(observed.latency_ms, 35)
                self.assertEqual(engine.call_count, 1)
                self.assertEqual(engine.call_args.args[0].shape[0], 96)
                self.assertEqual(engine.call_args.kwargs, {"use_det": False, "use_cls": False})

    def test_missing_or_invalid_roi_value_is_not_imputed(self):
        for results in ([], [[[]]], [["9999ms", 0.99]], [["0ms", 0.99]]):
            perception, engine = self.make_perception("honorofkings", results)
            self.assertIsNone(perception.analyze(np.zeros((1080, 1920, 3), dtype=np.uint8)).latency_ms)
            self.assertEqual(engine.call_count, 1)

    def test_jcc_keeps_full_frame_initial_detection(self):
        perception, engine = self.make_perception("jcc", [])
        frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        perception.analyze(frame)
        self.assertFalse(perception.tracked_only)
        self.assertIs(engine.call_args.args[0], frame)
