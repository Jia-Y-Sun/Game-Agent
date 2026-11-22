"""双端通信协议：长度前缀帧 + JSON 头 + 可选二进制负载。

只依赖标准库，设备端和程序端共用同一份实现，避免两边序列化不一致。

帧格式：

    ┌──────────┬──────┬────────────┬──────────────┬───────────┐
    │ 总长 4B  │ 类型 │ 头长 4B    │ 头 JSON      │ 二进制负载│
    └──────────┴──────┴────────────┴──────────────┴───────────┘

* 总长 = 1 + 4 + len(头) + len(负载)，不含自身 4 字节；
* 类型：`J` 只有头；`B` 头 + 二进制；
* 头里至少有 `kind` 字段；请求/响应消息用 `id` 关联。

约定：
* 一帧最大 64MB（`raw` 格式下 2400×1080×3 约 7.8MB，留足余量）；
* 读失败（对端关闭）返回 `None`，由调用方决定重连或放弃。
"""

import json
import socket
import struct

#: 协议版本，两端不一致时设备端会拒绝握手
PROTOCOL_VERSION = 1

#: 单帧上限 64MB
MAX_FRAME_BYTES = 64 * 1024 * 1024

_KIND_JSON = b"J"
_KIND_BINARY = b"B"

_LEN = struct.Struct("!I")
_HEAD_LEN = struct.Struct("!I")


class Message:
    """一条解析好的消息。"""

    __slots__ = ("kind", "meta", "blob")

    def __init__(self, kind, meta, blob=b""):
        self.kind = kind
        self.meta = meta or {}
        self.blob = blob or b""

    def __getitem__(self, key):
        return self.meta[key]

    def get(self, key, default=None):
        return self.meta.get(key, default)

    def __repr__(self):
        return f"Message(kind={self.kind!r}, meta_keys={sorted(self.meta)}, blob={len(self.blob)}B)"


def encode_frame(meta, blob=b""):
    """把一条消息编码成字节串（便于单测，不经过 socket）。"""
    payload_meta = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    kind = _KIND_BINARY if blob else _KIND_JSON
    body = bytearray()
    body += kind
    body += _HEAD_LEN.pack(len(payload_meta))
    body += payload_meta
    if blob:
        body += blob
    return _LEN.pack(len(body)) + bytes(body)


def decode_frame(frame_bytes):
    """把 `encode_frame` 的结果解回来，返回 `Message`。

    既接受完整帧（带 4 字节长度前缀），也接受去掉前缀的 body，方便单测直接喂数据。
    """
    if len(frame_bytes) >= 4:
        declared = _LEN.unpack(frame_bytes[:4])[0]
        if declared == len(frame_bytes) - 4:
            frame_bytes = frame_bytes[4:]

    if len(frame_bytes) < 5:
        raise ValueError("帧太短")
    kind = frame_bytes[0:1]
    meta_len = _HEAD_LEN.unpack(frame_bytes[1:5])[0]
    meta_end = 5 + meta_len
    if len(frame_bytes) < meta_end:
        raise ValueError("头长度超出帧长")
    meta = json.loads(frame_bytes[5:meta_end].decode("utf-8"))
    blob = frame_bytes[meta_end:]
    return Message(kind, meta, blob)


def send_message(sock, meta, blob=b"", lock=None):
    """发送一条消息；`lock` 用于多线程共用同一 socket 时串行化写入。"""
    data = encode_frame(meta, blob)
    if lock is not None:
        with lock:
            sock.sendall(data)
    else:
        sock.sendall(data)


def recv_exact(sock, size):
    """读满 `size` 字节；对端关闭时返回 `None`。"""
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_message(sock):
    """阻塞读一条消息；对端关闭返回 `None`，帧损坏抛 `ValueError`。"""
    head = recv_exact(sock, 4)
    if head is None:
        return None
    total = _LEN.unpack(head)[0]
    if total <= 0 or total > MAX_FRAME_BYTES:
        raise ValueError(f"帧长度异常: {total}")
    body = recv_exact(sock, total)
    if body is None:
        return None
    return decode_frame(body)


def parse_endpoint(endpoint, default_port=5599):
    """把 `host:port` / `host` 解析成 `(host, port)`。"""
    text = str(endpoint or "").strip()
    if not text:
        raise ValueError("endpoint 为空")
    if text.startswith("["):  # IPv6: [::1]:5599
        host, _, rest = text[1].partition("]")
        port = rest.lstrip(":") or default_port
        return host, int(port)
    if ":" in text:
        host, _, port = text.rpartition(":")
        return host or "127.0.0.1", int(port or default_port)
    return text, int(default_port)


def make_connection(host, port, timeout_sec=3.0):
    """建立 TCP 连接，顺手打开 TCP_NODELAY（每步都有小包，省 40ms 的 Nagle 延迟）。"""
    sock = socket.create_connection((host, int(port)), timeout=timeout_sec)
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    sock.settimeout(None)
    return sock
