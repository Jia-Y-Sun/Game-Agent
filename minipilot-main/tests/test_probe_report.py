import csv
import json
import tempfile
import unittest
from pathlib import Path

from integration.probe_report import build_report


class ProbeReportTests(unittest.TestCase):
    def test_report_uses_manifest_events_and_qoe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "run"
            events_dir = root / "events"
            capture_dir = root / "capture" / "sample"
            events_dir.mkdir(parents=True)
            capture_dir.mkdir(parents=True)
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "game_id": "wzry",
                        "device_id": "emulator-5554",
                        "status": "ok",
                        "capture_status": "ok",
                        "postprocess_status": "ok",
                        "match_result": "practice",
                        "match_result_source": "training_settlement",
                        "match_finish_source": "match_exit_detected",
                    }
                ),
                encoding="utf-8",
            )
            (events_dir / "game_driver.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "event": "MATCH_READY",
                                "ts": "2026-01-01T10:00:00+08:00",
                            }
                        ),
                        json.dumps(
                            {
                                "event": "MATCH_FINISHED",
                                "ts": "2026-01-01T10:00:30+08:00",
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            qoe = capture_dir / "run_qoe.csv"
            with qoe.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "file_name",
                        "time",
                        "rtt",
                        "trust_resolution",
                        "trust_stall",
                        "loading_reason",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "file_name": "run.pcap",
                        "time": "2026-01-01 10:00:00+0800",
                        "rtt": "30",
                        "trust_resolution": "-4",
                        "trust_stall": "0",
                        "loading_reason": "",
                    }
                )
            summary = capture_dir / "postprocess" / "summary.json"
            summary.parent.mkdir(parents=True)
            summary.write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "qoe": {"business_duration_seconds": 30},
                        "video_timing": {"actual_duration_seconds": 35},
                    }
                ),
                encoding="utf-8",
            )

            report = build_report(root)
            self.assertIn("`practice`", report)
            self.assertIn("`training_settlement`", report)
            self.assertIn("30 秒", report)
            self.assertIn("30 / 30 / 30 ms", report)
            self.assertIn("产物缺失", report)

    def test_game_title_and_failed_postprocess(self):
        with tempfile.TemporaryDirectory() as directory:
            for game_id, name in (("jcc", "金铲铲之战"), ("wzry", "王者荣耀")):
                with self.subTest(game_id=game_id):
                    report = build_report(directory, {
                        "game_id": game_id, "status": "ok", "postprocess_status": "failed",
                    })
                    self.assertTrue(report.startswith("# " + name))
                    self.assertIn("后处理缺失或未全部成功", report)
                    self.assertNotIn("链路完整", report)


if __name__ == "__main__":
    unittest.main()
