"""Extract passive TCP ACK RTT observations from a PCAP/PCAPNG file.

The extractor delegates TCP sequence/ACK analysis to TShark and writes one
row per ``tcp.analysis.ack_rtt`` observation.  RTT values are expressed in
milliseconds and timestamps are relative to the beginning of the capture.

Important: PCAPdroid VPN-mode captures use synthetic L3/L4 headers.  The
script detects the usual 10.215.173.0/24 virtual addresses and emits a clear
warning because ACK RTT from those captures is a local-proxy measurement,
not a reliable Internet RTT.
"""

from __future__ import annotations

import argparse
import csv
import ipaddress
import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path


FIELDS = (
    "frame.time_relative",
    "tcp.stream",
    "ip.src",
    "ip.dst",
    "ipv6.src",
    "ipv6.dst",
    "tcp.analysis.ack_rtt",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pcap", type=Path, help="Input .pcap or .pcapng file")
    parser.add_argument("output", type=Path, help="Output CSV file")
    parser.add_argument("--tshark", default="tshark", help="TShark executable")
    parser.add_argument(
        "--stream",
        type=int,
        action="append",
        help="Keep only this tcp.stream (repeatable)",
    )
    parser.add_argument(
        "--local-ip",
        action="append",
        default=[],
        help="Local/client IP used to label ACK direction (repeatable)",
    )
    parser.add_argument("--max-rtt-ms", type=float, default=10_000.0)
    return parser


def _resolve_tshark(value: str) -> str:
    candidate = Path(value)
    if candidate.exists():
        return str(candidate.resolve())
    resolved = shutil.which(value)
    if not resolved:
        raise FileNotFoundError(f"TShark not found: {value}")
    return resolved


def _is_pcapdroid_virtual(value: str) -> bool:
    try:
        return ipaddress.ip_address(value) in ipaddress.ip_network("10.215.173.0/24")
    except ValueError:
        return False


def extract(args: argparse.Namespace) -> dict[str, object]:
    pcap = args.pcap.resolve()
    if not pcap.is_file():
        raise FileNotFoundError(pcap)
    tshark = _resolve_tshark(args.tshark)
    display_filter = "tcp.analysis.ack_rtt"
    if args.stream:
        streams = " or ".join(f"tcp.stream == {value}" for value in args.stream)
        display_filter = f"tcp.analysis.ack_rtt and ({streams})"
    command = [
        tshark,
        "-r",
        str(pcap),
        "-Y",
        display_filter,
        "-T",
        "fields",
        "-E",
        "separator=/t",
        "-E",
        "occurrence=f",
    ]
    for field in FIELDS:
        command.extend(["-e", field])
    completed = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    local_ips = set(args.local_ip)
    rows: list[dict[str, object]] = []
    stream_counts: Counter[str] = Counter()
    saw_pcapdroid = False
    for line in completed.stdout.splitlines():
        columns = line.split("\t")
        if len(columns) < len(FIELDS):
            columns.extend([""] * (len(FIELDS) - len(columns)))
        time_text, stream, ip4_src, ip4_dst, ip6_src, ip6_dst, rtt_text = columns[:7]
        src = ip4_src or ip6_src
        dst = ip4_dst or ip6_dst
        saw_pcapdroid = saw_pcapdroid or _is_pcapdroid_virtual(src) or _is_pcapdroid_virtual(dst)
        try:
            time_s = float(time_text)
            rtt_ms = float(rtt_text) * 1000.0
        except ValueError:
            continue
        if not (0.0 < rtt_ms <= args.max_rtt_ms):
            continue
        if src in local_ips:
            component = "ack_from_local"
        elif dst in local_ips:
            component = "ack_from_remote"
        else:
            component = "direction_unknown"
        rows.append(
            {
                "time_s": round(time_s, 6),
                "rtt_ms": round(rtt_ms, 6),
                "tcp_stream": int(stream) if stream.isdigit() else stream,
                "ack_src": src,
                "ack_dst": dst,
                "component": component,
            }
        )
        stream_counts[stream] += 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("time_s", "rtt_ms", "tcp_stream", "ack_src", "ack_dst", "component"),
        )
        writer.writeheader()
        writer.writerows(rows)

    warning = (
        "PCAPdroid VPN virtual address detected; tcp.analysis.ack_rtt reflects "
        "synthetic/local-proxy TCP behavior and is not a trustworthy Internet RTT."
        if saw_pcapdroid
        else ""
    )
    return {
        "input": str(pcap),
        "output": str(args.output.resolve()),
        "observations": len(rows),
        "streams": len(stream_counts),
        "top_streams": stream_counts.most_common(10),
        "pcapdroid_vpn_detected": saw_pcapdroid,
        "warning": warning,
    }


def main() -> int:
    args = build_parser().parse_args()
    print(json.dumps(extract(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
