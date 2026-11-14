from __future__ import annotations

from dataclasses import dataclass


CSV_FIELDS = (
    "file_name",
    "time",
    "rtt",
    "trust_resolution",
    "trust_stall",
    "loading_reason",
)

NOT_APPLICABLE = -4
UNKNOWN = -128


@dataclass(frozen=True)
class MetricPolicy:
    latency: bool = False
    resolution: bool = False
    stall: bool = False


def metric_policy(app_type: str) -> MetricPolicy:
    """Route a dataset business type to the QoE fields required by contract."""
    value = (app_type or "").strip().lower().replace("-", "_").replace(" ", "_")

    # Cloud gaming must be checked before the generic game branch.
    if value in {"cloud_game", "cloudgame", "cloudgaming", "cloud_gaming", "云游戏"}:
        return MetricPolicy(resolution=True, stall=True)
    if value in {"game", "mobile_game", "mobilegame", "手游", "移动游戏"}:
        return MetricPolicy(latency=True)

    visual_streaming = {
        "short_video", "shortvideo", "短视频",
        "start_live", "watch_live", "live", "live_stream", "直播",
        "video", "long_video", "在线视频",
        "meeting", "video_meeting", "会议",
        "voip", "video_call", "音视频通话",
        "cloud_phone", "cloudphone", "云手机",
    }
    if value in visual_streaming:
        return MetricPolicy(resolution=True, stall=True)
    return MetricPolicy()
