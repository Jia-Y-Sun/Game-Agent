"""Estimate application response latency from bidirectional UDP traffic.

UDP has no ACK or sequence semantics, so this tool does *not* claim to
measure transport RTT.  It selects a long-lived, bidirectional UDP flow,
groups closely spaced client packets into uplink bursts, and pairs each burst
with the first subsequent downlink packet inside a bounded time window.

The output is useful for comparing timing trends with an application's own
latency display.  With encrypted or continuously streamed traffic (cloud
gaming video is a common example), the estimate may mostly describe the wait
until the next downlink burst rather than network RTT.
"""

from __future__ import annotations

import argparse
import csv
import ipaddress
import json
import math
import shutil
import subprocess
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path


FIELDS = (
    "frame.time_relative",
    "udp.stream",
    "ip.src",
    "ip.dst",
    "ipv6.src",
    "ipv6.dst",
    "udp.srcport",
    "udp.dstport",
    "udp.length",
)


@dataclass(frozen=True)
class Packet:
    time_s: float
    stream: int
    src: str
    dst: str
    src_port: int
    dst_port: int
    udp_length: int


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pcap", type=Path, help="Input .pcap or .pcapng file")
    parser.add_argument("output", type=Path, help="Output CSV file")
    parser.add_argument("--tshark", default="tshark", help="TShark executable")
    parser.add_argument("--local-ip", help="Client/local IP; inferred when omitted")
    parser.add_argument("--stream", type=int, help="Force a udp.stream number")
    parser.add_argument(
        "--max-response-ms",
        type=float,
        default=300.0,
        help="Maximum uplink-to-downlink pairing delay",
    )
    parser.add_argument(
        "--burst-gap-ms",
        type=float,
        default=2.0,
        help="Group consecutive uplink packets separated by at most this gap",
    )
    parser.add_argument(
        "--min-downlink-quiet-ms",
        type=float,
        default=2.0,
        help="Require this quiet gap before the paired downlink packet",
    )
    parser.add_argument(
        "--max-uplink-length",
        type=int,
        default=512,
        help="Ignore larger uplink bursts; control/input packets are usually small",
    )
    return parser


def _resolve_executable(value: str) -> str:
    candidate = Path(value)
    if candidate.exists():
        return str(candidate.resolve())
    resolved = shutil.which(value)
    if not resolved:
        raise FileNotFoundError(f"TShark not found: {value}")
    return resolved


def _read_packets(pcap: Path, tshark: str) -> list[Packet]:
    command = [
        tshark,
        "-r",
        str(pcap),
        "-Y",
        "udp",
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
    packets: list[Packet] = []
    for line in completed.stdout.splitlines():
        columns = line.split("\t")
        columns.extend([""] * (len(FIELDS) - len(columns)))
        time_text, stream_text, ip4_src, ip4_dst, ip6_src, ip6_dst, sport, dport, length = columns[:9]
        try:
            packets.append(
                Packet(
                    time_s=float(time_text),
                    stream=int(stream_text),
                    src=ip4_src or ip6_src,
                    dst=ip4_dst or ip6_dst,
                    src_port=int(sport),
                    dst_port=int(dport),
                    udp_length=int(length),
                )
            )
        except ValueError:
            continue
    return packets


def _is_private(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_private
    except ValueError:
        return False


def _infer_local_ip(packets: list[Packet]) -> str:
    addresses = Counter(packet.src for packet in packets)
    addresses.update(packet.dst for packet in packets)
    pcapdroid_clients = [ip for ip in addresses if ip.startswith("10.215.173.") and ip != "10.215.173.2"]
    if pcapdroid_clients:
        return max(pcapdroid_clients, key=addresses.get)
    private = [ip for ip in addresses if _is_private(ip)]
    if private:
        return max(private, key=addresses.get)
    return addresses.most_common(1)[0][0]


def _flow_summary(packets: list[Packet], local_ip: str) -> list[dict[str, object]]:
    grouped: dict[int, list[Packet]] = defaultdict(list)
    for packet in packets:
        grouped[packet.stream].append(packet)
    summaries: list[dict[str, object]] = []
    for stream, items in grouped.items():
        items.sort(key=lambda packet: packet.time_s)
        outbound = [packet for packet in items if packet.src == local_ip]
        inbound = [packet for packet in items if packet.dst == local_ip]
        if not outbound or not inbound:
            continue
        first = items[0]
        remote = first.dst if first.src == local_ip else first.src
        remote_port = first.dst_port if first.src == local_ip else first.src_port
        duration = max(0.0, items[-1].time_s - items[0].time_s)
        dns_penalty = 0.001 if remote_port == 53 else 1.0
        score = min(len(outbound), len(inbound)) * math.sqrt(len(items)) * math.log1p(duration) * dns_penalty
        summaries.append(
            {
                "stream": stream,
                "local_ip": local_ip,
                "remote_ip": remote,
                "remote_port": remote_port,
                "outbound_packets": len(outbound),
                "inbound_packets": len(inbound),
                "duration_s": round(duration, 6),
                "score": score,
            }
        )
    return sorted(summaries, key=lambda row: float(row["score"]), reverse=True)


def _uplink_bursts(items: list[Packet], local_ip: str, gap_s: float) -> list[list[Packet]]:
    outbound = [packet for packet in items if packet.src == local_ip]
    bursts: list[list[Packet]] = []
    for packet in outbound:
        if not bursts or packet.time_s - bursts[-1][-1].time_s > gap_s:
            bursts.append([packet])
        else:
            bursts[-1].append(packet)
    return bursts


def _estimate(
    items: list[Packet],
    local_ip: str,
    max_response_s: float,
    burst_gap_s: float,
    quiet_s: float,
    max_uplink_length: int,
) -> list[dict[str, object]]:
    inbound = [packet for packet in items if packet.dst == local_ip]
    inbound_times = [packet.time_s for packet in inbound]
    bursts = _uplink_bursts(items, local_ip, burst_gap_s)
    # Several uplink bursts can otherwise select the same first subsequent
    # downlink packet.  That produces a descending latency ramp for one packet
    # and, after time binning, artificial saw-tooth peaks.  Keep only the
    # closest eligible uplink burst for each downlink response.
    best_by_response: dict[int, tuple[list[Packet], Packet, float, float | None]] = {}
    for burst in bursts:
        uplink_end = burst[-1].time_s
        if max(packet.udp_length for packet in burst) > max_uplink_length:
            continue
        index = bisect_right(inbound_times, uplink_end)
        if index >= len(inbound):
            continue
        response = inbound[index]
        response_s = response.time_s - uplink_end
        if response_s <= 0.0 or response_s > max_response_s:
            continue
        previous_downlink = inbound[index - 1].time_s if index else None
        quiet_before_s = response.time_s - previous_downlink if previous_downlink is not None else None
        if quiet_before_s is not None and quiet_before_s < quiet_s:
            continue
        previous = best_by_response.get(index)
        if previous is None or uplink_end > previous[0][-1].time_s:
            best_by_response[index] = (burst, response, response_s, quiet_before_s)

    rows: list[dict[str, object]] = []
    for index in sorted(best_by_response):
        burst, response, response_s, quiet_before_s = best_by_response[index]
        uplink_end = burst[-1].time_s
        rows.append(
            {
                "time_s": round(uplink_end, 6),
                "udp_response_ms": round(response_s * 1000.0, 6),
                "udp_stream": burst[0].stream,
                "local_ip": local_ip,
                "remote_ip": response.src,
                "local_port": burst[0].src_port,
                "remote_port": burst[0].dst_port,
                "uplink_burst_packets": len(burst),
                "uplink_burst_bytes": sum(packet.udp_length for packet in burst),
                "first_uplink_length": burst[0].udp_length,
                "downlink_length": response.udp_length,
                "downlink_quiet_before_ms": round(quiet_before_s * 1000.0, 6) if quiet_before_s is not None else "",
                "pairing_method": "nearest_unique_uplink_before_downlink",
            }
        )
    return rows


def extract(args: argparse.Namespace) -> dict[str, object]:
    pcap = args.pcap.resolve()
    if not pcap.is_file():
        raise FileNotFoundError(pcap)
    packets = _read_packets(pcap, _resolve_executable(args.tshark))
    if not packets:
        raise RuntimeError("No UDP packets found")
    local_ip = args.local_ip or _infer_local_ip(packets)
    summaries = _flow_summary(packets, local_ip)
    if not summaries:
        raise RuntimeError(f"No bidirectional UDP flow found for local IP {local_ip}")
    selected = next((row for row in summaries if row["stream"] == args.stream), None) if args.stream is not None else summaries[0]
    if selected is None:
        raise ValueError(f"udp.stream {args.stream} is not bidirectional for {local_ip}")
    stream = int(selected["stream"])
    items = sorted((packet for packet in packets if packet.stream == stream), key=lambda packet: packet.time_s)
    rows = _estimate(
        items,
        local_ip,
        args.max_response_ms / 1000.0,
        args.burst_gap_ms / 1000.0,
        args.min_downlink_quiet_ms / 1000.0,
        args.max_uplink_length,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = (
        "time_s",
        "udp_response_ms",
        "udp_stream",
        "local_ip",
        "remote_ip",
        "local_port",
        "remote_port",
        "uplink_burst_packets",
        "uplink_burst_bytes",
        "first_uplink_length",
        "downlink_length",
        "downlink_quiet_before_ms",
        "pairing_method",
    )
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    values = sorted(float(row["udp_response_ms"]) for row in rows)
    percentile = lambda p: values[min(len(values) - 1, round((len(values) - 1) * p))] if values else None
    pcapdroid = local_ip.startswith("10.215.173.")
    warning = (
        "UDP has no ACK semantics. Values are encrypted-flow response-latency estimates, not UDP RTT. "
        "PCAPdroid VPN virtual addressing was detected; continuous cloud-game downlink can make the metric "
        "represent wait-to-next-downlink-burst rather than Internet RTT."
    )
    return {
        "input": str(pcap),
        "output": str(args.output.resolve()),
        "local_ip": local_ip,
        "selected_flow": {key: value for key, value in selected.items() if key != "score"},
        "candidate_flows": [{key: value for key, value in row.items() if key != "score"} for row in summaries[:5]],
        "estimates": len(rows),
        "median_ms": percentile(0.5),
        "p95_ms": percentile(0.95),
        "max_ms": values[-1] if values else None,
        "pcapdroid_vpn_detected": pcapdroid,
        "warning": warning,
    }


def main() -> int:
    args = build_parser().parse_args()
    print(json.dumps(extract(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
