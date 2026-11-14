"""Rule-based flow labeling for one finalized MiniPilot SA CSV.

The annotator is deliberately additive: it reads ``*_sa.csv`` and creates a
separate ``*_flow_labeled.csv`` beside it. Existing source and labeled files
are never overwritten.
"""

from __future__ import annotations

import csv
import json
import math
import os
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import yaml


MEDIA_LABELS = {
    "live": "live_media",
    "short_video": "short_video_media",
    "vod": "vod_media",
    "call": "call_media",
    "meeting": "call_media",
    "cloud_game": "cloud_game_media",
}

EMPTY_DOMAINS = {"", "nan", "none", "null", "-", "unknown"}
FLOW_OUTPUT_FIELDS = [
    "scene_label",
    "flow_label",
    "party_type",
    "sdk_vendor",
    "confidence",
    "label_reason",
    "large_flow_threshold",
    "source_file",
]

LIVE_SCENES = {"live", "game_live", "voice_live", "live_shopping"}
SHORT_VIDEO_SCENES = {"short_video", "video_feed", "feed", "story", "following"}
VOD_SCENES = {
    "movie",
    "tv_series",
    "variety",
    "cartoon",
    "video",
    "video_play",
}
CALL_SCENES = {"video_call", "voice_call", "group_video_call"}
MEETING_SCENES = {"video_meeting", "audio_meeting", "voice_meeting"}


def parse_number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return default if math.isnan(number) or math.isinf(number) else number
    except (TypeError, ValueError):
        return default


def normalize_domain(value: Any) -> str:
    domain = str(value or "").strip().lower().rstrip(".")
    return "" if domain in EMPTY_DOMAINS else domain


def suffix_match(domain: str, suffix: str) -> bool:
    suffix = suffix.lower().strip().lstrip(".")
    return bool(
        domain
        and (
            domain == suffix
            or domain.endswith("." + suffix)
            or domain.startswith(suffix + ".")
            or ("." + suffix + ".") in domain
        )
    )


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def normalize_scene(app_type: str, scene: str) -> str:
    """Map MiniPilot's detailed scene names to the flow-label taxonomy."""
    app_type = str(app_type or "unknown").strip().lower()
    scene = str(scene or "unknown").strip().lower()
    if app_type in {"cloudgame", "cloudphone"}:
        return "cloud_game"
    if scene in MEETING_SCENES:
        return "meeting"
    if scene in CALL_SCENES:
        return "call"
    if scene in LIVE_SCENES:
        return "live"
    if app_type == "video" and scene in SHORT_VIDEO_SCENES:
        return "short_video"
    if app_type == "video" and scene in VOD_SCENES:
        return "vod"
    if app_type == "news":
        return "news"
    if app_type == "aigc":
        return "aigc"
    if app_type == "social":
        return "instant_message"
    return scene if scene not in {"", "unknown"} else app_type


def _load_sample_context(sample_dir: Path) -> tuple[str, str, str]:
    yaml_files = sorted((*sample_dir.glob("*.yaml"), *sample_dir.glob("*.yml")))
    if not yaml_files:
        return "unknown", "unknown", "unknown"
    data = yaml.safe_load(yaml_files[0].read_text(encoding="utf-8")) or {}
    app = data.get("app_info") or {}
    app_type = str(app.get("app_type") or "unknown")
    app_name = str(app.get("app_name") or "unknown")
    typed = app.get(app_type) or {}
    scene = str(typed.get("scene") or app.get("scene") or "unknown")
    return app_type, app_name, scene


def _match_sdk(domain: str, rules: dict[str, Any]) -> str:
    for item in rules.get("sdk_rules", []):
        if any(suffix_match(domain, suffix) for suffix in item.get("domains", [])):
            return str(item.get("vendor") or "unknown_sdk")
    return ""


def _match_ad(domain: str, rules: dict[str, Any]) -> bool:
    return any(suffix_match(domain, suffix) for suffix in rules.get("ad_rules", []))


def _is_first_party(domain: str, app_name: str, rules: dict[str, Any]) -> bool:
    suffixes = rules.get("first_party_rules", {}).get(app_name, [])
    return any(suffix_match(domain, suffix) for suffix in suffixes)


def _is_dns(row: dict[str, str]) -> bool:
    protocol = (row.get("egn_sub_protocol") or "").strip().upper()
    dstport = int(parse_number(row.get("dstport"), -1))
    return protocol.startswith("DNS") or dstport == 53


def _decision(
    flow_label: str,
    party_type: str,
    sdk_vendor: str,
    confidence: float,
    reason: str,
) -> dict[str, str]:
    return {
        "flow_label": flow_label,
        "party_type": party_type,
        "sdk_vendor": sdk_vendor,
        "confidence": f"{confidence:.2f}",
        "label_reason": reason,
    }


def label_flow(
    row: dict[str, str],
    scene: str,
    threshold: float,
    rules: dict[str, Any],
) -> dict[str, str]:
    app_name = (row.get("app_name") or "unknown").strip().lower()
    domain = normalize_domain(row.get("domain"))
    flow_bytes = parse_number(row.get("flow_bytes"))
    up_bytes = parse_number(row.get("upper_bytes"))
    down_bytes = parse_number(row.get("down_bytes"))
    packet_num = parse_number(row.get("packet_num"))
    not_in_app = int(parse_number(row.get("not_in_app")))

    if not_in_app == 1:
        return _decision(
            "background",
            "background",
            "",
            0.90,
            "not_in_app=1（MiniPilot当前由packet_num<=2生成）",
        )
    if _is_dns(row):
        return _decision("dns", "unknown", "", 0.95, "协议为DNS或目标端口为53")
    if _match_ad(domain, rules):
        return _decision(
            "ad", "third_party_sdk", "ad_network", 0.95, f"广告域名规则命中: {domain}"
        )
    sdk_vendor = _match_sdk(domain, rules)
    if sdk_vendor:
        return _decision(
            "third_party_sdk",
            "third_party_sdk",
            sdk_vendor,
            0.90,
            f"SDK域名规则命中: {domain}",
        )

    media_candidate = (
        scene in MEDIA_LABELS
        and flow_bytes >= threshold
        and down_bytes > up_bytes * 3
        and packet_num >= 10
    )
    if media_candidate:
        party = (
            "first_party"
            if _is_first_party(domain, app_name, rules)
            else ("third_party_cdn" if domain else "unknown")
        )
        confidence = 0.80 if domain else 0.65
        return _decision(
            MEDIA_LABELS[scene],
            party,
            "",
            confidence,
            f"场景={scene}; 大流阈值={threshold:.0f}; 下行/上行>3; 包数>=10",
        )

    if flow_bytes > 0:
        party = (
            "first_party"
            if _is_first_party(domain, app_name, rules)
            else ("third_party" if domain else "unknown")
        )
        return _decision("business_api", party, "", 0.55, "目标App的非媒体有效流")
    return _decision("unknown", "unknown", "", 0.30, "无有效字节或证据不足")


def _exclusive_atomic_csv(
    output_path: Path,
    fieldnames: list[str],
    rows: list[dict[str, Any]],
) -> bool:
    """Publish a complete CSV without replacing an existing output."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8-sig",
            newline="",
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            dir=output_path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, output_path)
        except FileExistsError:
            return False
        except OSError:
            # FAT/exFAT and some network volumes do not support hard links.
            # Exclusive creation preserves the no-overwrite contract there.
            created = False
            try:
                with temporary.open("rb") as source, output_path.open("xb") as output:
                    created = True
                    shutil.copyfileobj(source, output)
                    output.flush()
                    os.fsync(output.fileno())
            except FileExistsError:
                return False
            except Exception:
                if created:
                    output_path.unlink(missing_ok=True)
                raise
        return True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def label_sa_file(
    sa_path: str | Path,
    *,
    output_path: str | Path | None = None,
    app_type: str | None = None,
    scene: str | None = None,
    rules_path: str | Path | None = None,
) -> dict[str, Any]:
    source = Path(sa_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"SA CSV不存在: {source}")
    if not source.name.endswith("_sa.csv"):
        raise ValueError(f"输入文件必须以_sa.csv结尾: {source.name}")
    destination = (
        Path(output_path).resolve()
        if output_path is not None
        else source.with_name(source.name[:-7] + "_flow_labeled.csv")
    )
    if source == destination:
        raise ValueError("流级标注输出不能覆盖原始SA CSV")
    if destination.exists():
        return {
            "status": "already_exists",
            "source_file": str(source),
            "output_file": str(destination),
        }

    default_rules = Path(__file__).with_name("flow_rules.json")
    resolved_rules = Path(rules_path).resolve() if rules_path else default_rules
    rules = json.loads(resolved_rules.read_text(encoding="utf-8"))

    context_type, context_app, context_scene = _load_sample_context(source.parent)
    effective_type = str(app_type or context_type)
    effective_scene = str(scene or context_scene)
    scene_label = normalize_scene(effective_type, effective_scene)

    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        original_fields = list(reader.fieldnames or [])
        rows = list(reader)
    if not original_fields:
        raise ValueError(f"SA CSV缺少表头: {source}")

    threshold = max(
        percentile([parse_number(row.get("flow_bytes")) for row in rows], 0.75),
        500_000.0,
    )
    labeled_rows: list[dict[str, Any]] = []
    label_counts: Counter[str] = Counter()
    for row in rows:
        result = label_flow(row, scene_label, threshold, rules)
        labeled = dict(row)
        labeled.update(
            {
                "scene_label": scene_label,
                **result,
                "large_flow_threshold": f"{threshold:.0f}",
                "source_file": str(source),
            }
        )
        labeled_rows.append(labeled)
        label_counts[result["flow_label"]] += 1

    fieldnames = original_fields + [
        field for field in FLOW_OUTPUT_FIELDS if field not in original_fields
    ]
    created = _exclusive_atomic_csv(destination, fieldnames, labeled_rows)
    if not created:
        return {
            "status": "already_exists",
            "source_file": str(source),
            "output_file": str(destination),
        }
    return {
        "status": "ok",
        "source_file": str(source),
        "output_file": str(destination),
        "app_type": effective_type,
        "app_name": context_app,
        "source_scene": effective_scene,
        "scene_label": scene_label,
        "flow_count": len(labeled_rows),
        "large_flow_threshold": int(round(threshold)),
        "label_counts": dict(sorted(label_counts.items())),
        "rules_file": str(resolved_rules),
    }


def label_sample_flows(
    sample_dir: str | Path,
    *,
    app_type: str | None = None,
    scene: str | None = None,
    rules_path: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(sample_dir).resolve()
    sources = sorted(root.glob("*_sa.csv"))
    if not sources:
        raise FileNotFoundError(f"样本目录中没有*_sa.csv: {root}")
    if len(sources) > 1:
        raise ValueError(f"样本目录中存在多个*_sa.csv，无法确定输入: {root}")
    return label_sa_file(
        sources[0],
        app_type=app_type,
        scene=scene,
        rules_path=rules_path,
    )
