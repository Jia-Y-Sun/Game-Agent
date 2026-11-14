from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


UNICAPTURE_DIR = Path(__file__).resolve().parents[1] / "unicapture"
if str(UNICAPTURE_DIR) not in sys.path:
    sys.path.insert(0, str(UNICAPTURE_DIR))

from generate_metadata import (  # noqa: E402
    build_pcap_filename,
    generate_empty_qoe_csv,
    generate_qoe_csv,
)
from qoe_postprocess.sample import _first_pcap  # noqa: E402


class PcapFilenameTests(unittest.TestCase):
    def test_builds_plain_pcap_and_pcapng_names(self) -> None:
        self.assertEqual(build_pcap_filename("sample"), "sample.pcap")
        self.assertEqual(build_pcap_filename("sample", ".pcapng"), "sample.pcapng")
        self.assertEqual(build_pcap_filename("sample", "pcapng"), "sample.pcapng")

    def test_generated_qoe_rows_reference_plain_pcap_name(self) -> None:
        timestamp = datetime(2026, 1, 1, 10, 0, 0)

        qoe_rows = generate_qoe_csv("sample", timestamp, timestamp)
        empty_rows = generate_empty_qoe_csv(
            "sample", timestamp, timestamp, pcap_ext=".pcapng"
        )

        self.assertEqual(qoe_rows[1][0], "sample.pcap")
        self.assertEqual(empty_rows[1][0], "sample.pcapng")

    def test_postprocess_prefers_plain_name_but_reads_legacy_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "cut_sample.pcap"
            legacy.write_bytes(b"legacy")
            self.assertEqual(_first_pcap(root), legacy)

            current = root / "sample.pcap"
            current.write_bytes(b"current")
            self.assertEqual(_first_pcap(root), current)


if __name__ == "__main__":
    unittest.main()
