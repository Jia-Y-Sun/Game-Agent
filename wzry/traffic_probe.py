import subprocess
import time
from dataclasses import dataclass

from argparses import args


@dataclass
class TrafficSnapshot:
    timestamp: float
    rx_bytes: int = 0
    tx_bytes: int = 0
    rx_packets: int = 0
    tx_packets: int = 0
    interfaces: tuple = ()
    available: bool = False


class NullTrafficProbe:
    def reset_episode(self):
        return None

    def begin_step(self):
        return TrafficSnapshot(timestamp=time.perf_counter())

    def end_step(self, start_snapshot):
        return {
            "enabled": False,
            "available": False,
            "duration_ms": 0.0,
            "interfaces": (),
            "rx_bytes": 0,
            "tx_bytes": 0,
            "total_bytes": 0,
            "rx_packets": 0,
            "tx_packets": 0,
            "total_packets": 0,
        }


class AdbProcNetDevTrafficProbe:
    def __init__(self, adb_path, device_serial, interfaces=None, timeout_sec=1.0):
        self.adb_path = adb_path
        self.device_serial = device_serial
        self.interfaces = tuple(interfaces or ())
        self.timeout_sec = timeout_sec

    def reset_episode(self):
        return None

    def _read_proc_net_dev(self):
        result = subprocess.run(
            [self.adb_path, "-s", self.device_serial, "shell", "cat", "/proc/net/dev"],
            capture_output=True,
            text=True,
            timeout=self.timeout_sec,
        )
        if result.returncode != 0:
            return {}

        counters = {}
        for line in result.stdout.splitlines():
            if ":" not in line:
                continue
            interface_name, raw_values = line.split(":", 1)
            values = raw_values.split()
            if len(values) < 10:
                continue

            interface_name = interface_name.strip()
            if interface_name == "lo":
                continue

            counters[interface_name] = {
                "rx_bytes": int(values[0]),
                "rx_packets": int(values[1]),
                "tx_bytes": int(values[8]),
                "tx_packets": int(values[9]),
            }
        return counters

    def _select_interfaces(self, counters):
        if self.interfaces:
            return [interface_name for interface_name in self.interfaces if interface_name in counters]
        return sorted(counters.keys())

    def snapshot(self):
        timestamp = time.perf_counter()
        try:
            counters = self._read_proc_net_dev()
        except (OSError, subprocess.SubprocessError, ValueError):
            return TrafficSnapshot(timestamp=timestamp)

        selected_interfaces = self._select_interfaces(counters)
        if not selected_interfaces:
            return TrafficSnapshot(timestamp=timestamp)

        return TrafficSnapshot(
            timestamp=timestamp,
            rx_bytes=sum(counters[name]["rx_bytes"] for name in selected_interfaces),
            tx_bytes=sum(counters[name]["tx_bytes"] for name in selected_interfaces),
            rx_packets=sum(counters[name]["rx_packets"] for name in selected_interfaces),
            tx_packets=sum(counters[name]["tx_packets"] for name in selected_interfaces),
            interfaces=tuple(selected_interfaces),
            available=True,
        )

    def begin_step(self):
        return self.snapshot()

    def end_step(self, start_snapshot):
        end_snapshot = self.snapshot()
        duration_ms = max(0.0, (end_snapshot.timestamp - start_snapshot.timestamp) * 1000)
        available = start_snapshot.available and end_snapshot.available
        if not available:
            return {
                "enabled": True,
                "available": False,
                "duration_ms": round(duration_ms, 2),
                "interfaces": (),
                "rx_bytes": 0,
                "tx_bytes": 0,
                "total_bytes": 0,
                "rx_packets": 0,
                "tx_packets": 0,
                "total_packets": 0,
            }

        rx_bytes = max(0, end_snapshot.rx_bytes - start_snapshot.rx_bytes)
        tx_bytes = max(0, end_snapshot.tx_bytes - start_snapshot.tx_bytes)
        rx_packets = max(0, end_snapshot.rx_packets - start_snapshot.rx_packets)
        tx_packets = max(0, end_snapshot.tx_packets - start_snapshot.tx_packets)
        return {
            "enabled": True,
            "available": True,
            "duration_ms": round(duration_ms, 2),
            "interfaces": end_snapshot.interfaces,
            "rx_bytes": rx_bytes,
            "tx_bytes": tx_bytes,
            "total_bytes": rx_bytes + tx_bytes,
            "rx_packets": rx_packets,
            "tx_packets": tx_packets,
            "total_packets": rx_packets + tx_packets,
        }


def build_traffic_probe(android_controller):
    if args.traffic_probe_mode == "disabled":
        return NullTrafficProbe()

    interfaces = [item.strip() for item in args.traffic_probe_interfaces.split(",") if item.strip()]
    return AdbProcNetDevTrafficProbe(
        adb_path=f"{android_controller.scrcpy_dir}/adb",
        device_serial=android_controller.device_serial,
        interfaces=interfaces,
        timeout_sec=args.traffic_probe_timeout_sec,
    )
