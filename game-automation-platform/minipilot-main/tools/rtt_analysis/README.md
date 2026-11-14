# RTT extraction utilities

`extract_tcp_rtt.py` extracts raw `tcp.analysis.ack_rtt` observations from
PCAP/PCAPNG via TShark. It detects the usual PCAPdroid VPN virtual subnet and
warns when the output is a synthetic/local-proxy measurement.

```powershell
python tools/rtt_analysis/extract_tcp_rtt.py capture.pcap tcp_rtt.csv `
  --tshark D:\Wireshark\tshark.exe --local-ip 192.168.1.10
```

`extract_ocr_rtt.py` reads a fixed on-screen `NN ms` region from video. The
ROI coordinates refer to the logical display after `--display-size` scaling.

```powershell
python tools/rtt_analysis/extract_ocr_rtt.py recording.mp4 ocr_rtt.csv `
  --ffmpeg E:\ffmpeg-master-latest-win64-gpl\bin\ffmpeg.exe `
  --fps 5 --display-size 2376x1080 --roi 2175,95,110,45
```

`extract_udp_response_latency.py` estimates an encrypted UDP flow's
uplink-burst to next-downlink delay. It automatically chooses the dominant
long-lived bidirectional flow, or accepts `--stream`. This is an application
response-latency estimate, not protocol-level UDP RTT.

```powershell
python tools/rtt_analysis/extract_udp_response_latency.py capture.pcap udp_latency.csv `
  --tshark D:\Wireshark\tshark.exe --local-ip 10.215.173.1
```

For experimental validity, prefer root, gateway, TAP, or mirror-port PCAPs.
PCAPdroid VPN-mode exports synthesize L3/L4 headers and cannot provide a
trustworthy Internet RTT from TCP ACK timing.
