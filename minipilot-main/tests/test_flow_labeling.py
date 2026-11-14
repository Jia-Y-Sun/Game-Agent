from __future__ import annotations

import csv
import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from unicapture.postprocess.flow_labeler import (
    FLOW_OUTPUT_FIELDS,
    label_sa_file,
    normalize_scene,
)


SA_FIELDS = [
    "uri",
    "app_type",
    "app_name",
    "packet_num",
    "upper_packet_num",
    "down_packet_num",
    "syn_flag",
    "flow_bytes",
    "upper_bytes",
    "down_bytes",
    "srcip",
    "dstip",
    "srcport",
    "dstport",
    "l4proto",
    "domain",
    "flow_start_time",
    "not_in_app",
    "egn_sub_protocol",
]


def flow_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "uri": "fixture.pcap",
        "app_type": "video",
        "app_name": "bilibili",
        "packet_num": 100,
        "upper_packet_num": 20,
        "down_packet_num": 80,
        "syn_flag": 1,
        "flow_bytes": 10_000,
        "upper_bytes": 2_000,
        "down_bytes": 8_000,
        "srcip": "10.0.0.1",
        "dstip": "1.1.1.1",
        "srcport": 12345,
        "dstport": 443,
        "l4proto": 6,
        "domain": "api.bilibili.com",
        "flow_start_time": "2026/08/21 10:00",
        "not_in_app": 0,
        "egn_sub_protocol": "HTTPS",
    }
    row.update(overrides)
    return row


def write_sa(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SA_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


class FlowLabelingTests(unittest.TestCase):
    def test_labels_one_sample_without_modifying_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture_sa.csv"
            write_sa(
                source,
                [
                    flow_row(
                        flow_bytes=2_000_000,
                        upper_bytes=100_000,
                        down_bytes=1_900_000,
                        domain="video.bilivideo.com",
                    ),
                    flow_row(domain="xs.gdt.qq.com"),
                    flow_row(domain="www.google-analytics.com"),
                    flow_row(dstport=53, egn_sub_protocol="DNS"),
                    flow_row(packet_num=2, not_in_app=1),
                ],
            )
            original = source.read_bytes()

            summary = label_sa_file(source, app_type="video", scene="live")
            output = Path(summary["output_file"])

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(source.read_bytes(), original)
            self.assertTrue(output.exists())
            with output.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
                fields = list(rows[0])
            self.assertEqual(len(rows), 5)
            self.assertTrue(set(SA_FIELDS).issubset(fields))
            self.assertTrue(set(FLOW_OUTPUT_FIELDS).issubset(fields))
            self.assertEqual(rows[0]["flow_label"], "live_media")
            self.assertEqual(rows[1]["flow_label"], "ad")
            self.assertEqual(rows[2]["flow_label"], "third_party_sdk")
            self.assertEqual(rows[3]["flow_label"], "dns")
            self.assertEqual(rows[4]["flow_label"], "background")

    def test_existing_labeled_file_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture_sa.csv"
            output = root / "fixture_flow_labeled.csv"
            write_sa(source, [flow_row()])
            output.write_text("keep-me\n", encoding="utf-8")

            summary = label_sa_file(source, output_path=output, scene="live")

            self.assertEqual(summary["status"], "already_exists")
            self.assertEqual(output.read_text(encoding="utf-8"), "keep-me\n")

    def test_volume_without_hardlinks_uses_exclusive_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "fixture_sa.csv"
            write_sa(source, [flow_row()])

            with patch(
                "unicapture.postprocess.flow_labeler.os.link",
                side_effect=OSError("hard links unavailable"),
            ):
                summary = label_sa_file(source, app_type="video", scene="movie")

            output = Path(summary["output_file"])
            self.assertEqual(summary["status"], "ok")
            self.assertTrue(output.exists())
            self.assertEqual(len(output.read_text(encoding="utf-8-sig").splitlines()), 2)

    def test_empty_sa_creates_header_only_labeled_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "fixture_sa.csv"
            write_sa(source, [])

            summary = label_sa_file(source, app_type="video", scene="movie")

            with Path(summary["output_file"]).open(
                "r", encoding="utf-8-sig", newline=""
            ) as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
            self.assertEqual(rows, [])
            self.assertTrue(set(FLOW_OUTPUT_FIELDS).issubset(reader.fieldnames or []))

    def test_minipilot_scenes_are_normalized(self) -> None:
        self.assertEqual(normalize_scene("video", "short_video"), "short_video")
        self.assertEqual(normalize_scene("video", "movie"), "vod")
        self.assertEqual(normalize_scene("video", "live"), "live")
        self.assertEqual(normalize_scene("social", "video_call"), "call")
        self.assertEqual(normalize_scene("social", "video_meeting"), "meeting")
        self.assertEqual(normalize_scene("cloudgame", "cloud_fps"), "cloud_game")

    @unittest.skipUnless(
        importlib.util.find_spec("cv2"),
        "postprocess integration requires opencv-python",
    )
    def test_flow_failure_is_recorded_without_raising(self) -> None:
        from unicapture.postprocess_runner import run_sample_postprocess

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch(
                    "unicapture.postprocess_runner.annotate_business_times",
                    return_value=0,
                ),
                patch(
                    "unicapture.postprocess_runner.load_sample",
                    return_value=SimpleNamespace(root=root, name="fixture"),
                ),
                patch(
                    "unicapture.postprocess_runner.process_sample",
                    return_value={"business_start_second": None, "rows": 0},
                ),
                patch(
                    "unicapture.postprocess_runner.label_sample_flows",
                    side_effect=RuntimeError("fixture flow failure"),
                ),
            ):
                summary = run_sample_postprocess(root, enable_vlm=False)

            self.assertEqual(summary["status"], "partial_error")
            self.assertEqual(summary["flow_labeling"]["status"], "error")
            self.assertIn("fixture flow failure", summary["flow_labeling"]["error"])
            self.assertTrue((root / "postprocess" / "summary.json").exists())

    @unittest.skipUnless(
        importlib.util.find_spec("cv2"),
        "postprocess integration requires opencv-python",
    )
    def test_runner_creates_flow_output_and_records_summary(self) -> None:
        from unicapture.postprocess_runner import run_sample_postprocess

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture_sa.csv"
            write_sa(
                source,
                [
                    flow_row(
                        flow_bytes=2_000_000,
                        upper_bytes=100_000,
                        down_bytes=1_900_000,
                        domain="video.bilivideo.com",
                    )
                ],
            )
            with (
                patch(
                    "unicapture.postprocess_runner.annotate_business_times",
                    return_value=0,
                ),
                patch(
                    "unicapture.postprocess_runner.load_sample",
                    return_value=SimpleNamespace(root=root, name="fixture"),
                ),
                patch(
                    "unicapture.postprocess_runner.process_sample",
                    return_value={"business_start_second": None, "rows": 0},
                ),
            ):
                summary = run_sample_postprocess(
                    root,
                    enable_vlm=False,
                    flow_app_type="video",
                    flow_scene="live",
                )

            output = root / "fixture_flow_labeled.csv"
            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["flow_labeling"]["status"], "ok")
            self.assertEqual(summary["flow_labeling"]["flow_count"], 1)
            self.assertTrue(output.exists())
            self.assertTrue((root / "postprocess" / "summary.json").exists())


if __name__ == "__main__":
    unittest.main()
