from __future__ import annotations

import struct
import tempfile
import unittest
from datetime import timezone
from pathlib import Path

from tools.rebuild_sample import _classic_pcap_bounds, discover_samples


class RebuildSampleTests(unittest.TestCase):
    def test_discovers_nested_run_sample(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample = root / "capture" / "douyin-300-20260101T010101"
            sample.mkdir(parents=True)
            (sample / "fixture.mp4").write_bytes(b"video")
            (sample / "fixture.pcap").write_bytes(b"pcap")
            found = discover_samples(root)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].root, sample.resolve())
            self.assertEqual(found[0].prefix, "fixture")

    def test_rejects_ambiguous_video_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.mp4").write_bytes(b"a")
            (root / "b.mp4").write_bytes(b"b")
            (root / "a.pcap").write_bytes(b"pcap")
            with self.assertRaisesRegex(RuntimeError, "只能有一个MP4"):
                discover_samples(root)

    def test_reads_classic_pcap_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.pcap"
            global_header = b"\xd4\xc3\xb2\xa1" + struct.pack(
                "<HHIIII", 2, 4, 0, 0, 65535, 1
            )
            packets = b"".join([
                struct.pack("<IIII", 100, 250_000, 1, 1) + b"a",
                struct.pack("<IIII", 102, 750_000, 1, 1) + b"b",
            ])
            path.write_bytes(global_header + packets)
            bounds = _classic_pcap_bounds(path, timezone.utc)
            self.assertIsNotNone(bounds)
            assert bounds is not None
            self.assertEqual(bounds[0].timestamp(), 100.25)
            self.assertEqual(bounds[1].timestamp(), 102.75)


if __name__ == "__main__":
    unittest.main()
