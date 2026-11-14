from __future__ import annotations

import unittest
import json
import tempfile
from datetime import datetime
from pathlib import Path

from unicapture.qoe_postprocess.processor import (
    InternalSecond, _find_business_start, _format_time,
    _has_trusted_runtime_business_timing, _select_business_records,
    _validated_latency,
)
from unicapture.qoe_postprocess.perception import Perception
from unicapture.qoe_postprocess.sample import Sample, load_sample, parse_dataset_time
from unicapture.qoe_postprocess.schema import CSV_FIELDS, metric_policy


class QoEPostprocessTests(unittest.TestCase):
    def test_csv_schema_is_dataset_schema(self):
        self.assertEqual(CSV_FIELDS, (
            "file_name", "time", "rtt", "trust_resolution", "trust_stall",
            "loading_reason",
        ))

    def test_contract_metric_routing(self):
        game = metric_policy("game")
        self.assertTrue(game.latency)
        self.assertFalse(game.resolution)
        self.assertFalse(game.stall)
        cloud_game = metric_policy("cloud_game")
        self.assertFalse(cloud_game.latency)
        self.assertTrue(cloud_game.resolution)
        self.assertTrue(cloud_game.stall)

    def test_dataset_time_preserves_fractional_alignment(self):
        start = parse_dataset_time("20260705T15555466+0800")
        self.assertIsNotNone(start)
        sample = Sample(
            root=None, yaml_path=None, video_path=None, pcap_path=None,
            screenshots=(), name="x", app_type="game", app_name="x",
            package="x", scene="gameplay", activate_start_second=None,
            vlm_business_start_second=None,
            timing_business_start_second=None,
            timing_business_start_source="",
            target_business_duration=None,
            initial_resolution=None,
            source_resolution_series=(), source_stall_series=(),
            start_time=start, duration_seconds=300,
        )
        self.assertEqual(_format_time(sample, 0), "2026-07-05 15:55:54.66+0800")
        self.assertEqual(_format_time(sample, 217), "2026-07-05 15:59:31.66+0800")

    def test_business_start_requires_repeated_evidence(self):
        empty = []
        for second, evidence in ((10, True), (13, True), (30, True)):
            empty.append(InternalSecond(second, None, True, "test", business_evidence=evidence))
        self.assertEqual(_find_business_start(empty), 10)

    def test_effective_output_starts_at_business_and_has_target_length(self):
        records = [
            InternalSecond(i, Perception(), True, "video") for i in range(400)
        ]
        selected = _select_business_records(records, 40, 300)
        self.assertEqual(len(selected), 300)
        self.assertEqual(selected[0].second, 40)
        self.assertEqual(selected[-1].second, 339)

    def test_runtime_marker_supplies_start_and_target_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sample"
            root.mkdir()
            (root / "fixture.yaml").write_text(
                "name: fixture\n"
                "time_zone: '+0800'\n"
                "task_info:\n"
                "  start_time: 20260101T10000000+0800\n"
                "app_info:\n"
                "  app_name: bilibili\n"
                "  app_package: tv.danmaku.bili\n"
                "  app_type: video\n"
                "  scene: video\n"
                "video_record:\n"
                "  start_time: 20260101T10000000+0800\n"
                "  end_time: 20260101T10064000+0800\n"
                "  duration: 400\n",
                encoding="utf-8",
            )
            (root.parent / "business_timing.json").write_text(
                json.dumps({
                    "business_start_time": "2026-01-01T10:00:40+08:00",
                    "source": "vlm",
                    "target_duration_seconds": 300,
                }),
                encoding="utf-8",
            )
            sample = load_sample(root)
            self.assertEqual(sample.timing_business_start_second, 40)
            self.assertEqual(sample.target_business_duration, 300)

    def test_partial_latency_ocr_uses_previous_value(self):
        self.assertIsNone(_validated_latency(4, 44))
        self.assertEqual(_validated_latency(64, 44), 64)

    def test_trusted_runtime_business_timing_skips_duplicate_vlm(self):
        trusted = type("SampleLike", (), {
            "timing_business_start_second": 57,
            "timing_business_start_source": "vlm",
        })()
        fallback = type("SampleLike", (), {
            "timing_business_start_second": 57,
            "timing_business_start_source": "logcat_activate_fallback",
        })()
        game_script = type("SampleLike", (), {
            "timing_business_start_second": 57,
            "timing_business_start_source": "game_script_match_ready",
        })()
        self.assertTrue(_has_trusted_runtime_business_timing(trusted))
        self.assertTrue(_has_trusted_runtime_business_timing(game_script))
        self.assertFalse(_has_trusted_runtime_business_timing(fallback))


if __name__ == "__main__":
    unittest.main()
