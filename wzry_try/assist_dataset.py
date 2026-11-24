"""整场对局人机协作示范数据集的读写层。

`wzry` 原版的人为数据只有两种形态：

* `human_data_collector.py` 采到的 `samples.jsonl`（离线、单独开一个进程）；
* `src/lane_profile.json`（只覆盖开局走线）。

本模块把“一步对局状态 + AI 想做什么 + 人实际做了什么 + 当时的阶段/血量/压力”
统一成一种紧凑的 JSONL 记录，供整场对局的人为辅助训练使用：

* 采集端：`live_human_assist.py` 在 `train.py` 里实时写这种记录；
* 训练端：`train_behavior_clone.py` 直接读这种记录做行为克隆；
* 蒸馏端：`build_game_profile.py` 把它压成整场行为 profile。

同时保持对旧格式（`samples.jsonl` + `parsed_action`）的完全兼容，
旧数据不需要重新采集。
"""

import datetime
import json
import os
from collections import Counter, deque

import cv2
import numpy as np

from asset_paths import local_path

ASSIST_SCHEMA = 2
ACTION_LEN = 8
ACTION_SIZES = [2, 360, 9, 11, 3, 360, 100, 5]

ZERO_ACTION = [0] * ACTION_LEN

#: 新格式文件名；读取时也会自动识别旧格式
ASSIST_FILE_NAME = "assist_samples.jsonl"
LEGACY_FILE_NAMES = ("samples.jsonl", "samples_relabel.jsonl")
ALL_FILE_NAMES = (ASSIST_FILE_NAME,) + LEGACY_FILE_NAMES

FRAME_POLICIES = ("human", "all", "none")


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def clamp_action(action, sizes=None):
    """把任意长度/越界的动作裁剪成合法的 8 维动作。"""
    sizes = sizes or ACTION_SIZES
    if action is None:
        return list(ZERO_ACTION)
    try:
        values = [int(value) for value in list(action)[:ACTION_LEN]]
    except (TypeError, ValueError):
        return list(ZERO_ACTION)
    while len(values) < ACTION_LEN:
        values.append(0)
    return [max(0, min(size - 1, value)) for value, size in zip(values, sizes)]


def is_zero_action(action):
    return not any(int(value) != 0 for value in (action or []))


def action_dict(action):
    """把 8 维动作展开成可读字段，便于人看日志。"""
    action = clamp_action(action)
    return {
        "move": action[0],
        "angle": action[1],
        "info": action[2],
        "attack": action[3],
        "type": action[4],
        "arg1": action[5],
        "arg2": action[6],
        "arg3": action[7],
    }


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# 写入端
# --------------------------------------------------------------------------- #
class AssistDatasetWriter:
    """把每一步对局写成一行 JSONL，并按策略缓存关键帧。

    关键帧采用“延迟 N 步落盘”的做法：只有当某一步之后（含自身）出现了人为操作时，
    才把这一步的帧写进磁盘。这样既能拿到人为接管前几帧的上下文，
    又不会因为全程存图把磁盘写爆。
    """

    def __init__(
        self,
        episode_dir,
        episode_id=None,
        frame_policy="human",
        frame_max_width=640,
        jpeg_quality=78,
        save_frames=True,
        preroll_steps=4,
        metadata=None,
        flush_every=24,
    ):
        self.episode_dir = os.path.abspath(local_path(episode_dir))
        os.makedirs(self.episode_dir, exist_ok=True)

        policy = str(frame_policy or "human").lower()
        self.frame_policy = policy if policy in FRAME_POLICIES else "human"
        self.save_frames = bool(save_frames) and self.frame_policy != "none"
        self.frame_max_width = max(0, int(frame_max_width))
        self.jpeg_quality = int(max(30, min(100, int(jpeg_quality))))
        self.preroll_steps = max(0, int(preroll_steps))
        self.flush_every = max(1, int(flush_every))

        self.episode_id = episode_id or os.path.basename(self.episode_dir)
        self.samples_path = os.path.join(self.episode_dir, ASSIST_FILE_NAME)
        self.frames_dir = os.path.join(self.episode_dir, "frames")
        if self.save_frames:
            os.makedirs(self.frames_dir, exist_ok=True)

        self._file = open(self.samples_path, "a", encoding="utf-8")
        self._pending = deque()
        self._frame_index = 0
        self._written = 0
        self._human_steps = 0
        self._override_steps = 0
        self._frames_written = 0
        self._closed = False

        self.metadata = dict(metadata or {})
        self.metadata.update({
            "episode_id": self.episode_id,
            "schema": ASSIST_SCHEMA,
            "created_at": self.metadata.get("created_at") or _now_iso(),
            "frame_policy": self.frame_policy,
            "save_frames": bool(self.save_frames),
            "frame_max_width": self.frame_max_width,
            "jpeg_quality": self.jpeg_quality,
            "preroll_steps": self.preroll_steps,
            "action_format": [
                "move_action",
                "angle",
                "info_action",
                "attack_action",
                "action_type",
                "arg1",
                "arg2",
                "arg3",
            ],
        })
        self._write_metadata()

    # -- 元信息 -------------------------------------------------------------
    def _write_metadata(self):
        path = os.path.join(self.episode_dir, "metadata.json")
        payload = dict(self.metadata)
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as file:
                    existing = json.load(file)
                if isinstance(existing, dict):
                    payload = {**existing, **payload}
        except (OSError, ValueError):
            pass
        try:
            with open(path, "w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def update_metadata(self, **kwargs):
        self.metadata.update(kwargs)
        self._write_metadata()

    # -- 帧处理 -------------------------------------------------------------
    def _prepare_frame(self, frame):
        if frame is None or getattr(frame, "size", 0) == 0:
            return None
        width = frame.shape[1]
        if self.frame_max_width > 0 and width > self.frame_max_width:
            scale = self.frame_max_width / float(width)
            new_size = (self.frame_max_width, max(1, int(round(frame.shape[0] * scale))))
            frame = cv2.resize(frame, new_size, interpolation=cv2.INTER_AREA)
        return frame

    def encode_frame(self, frame):
        if not self.save_frames:
            return None
        prepared = self._prepare_frame(frame)
        if prepared is None:
            return None
        ok, buffer = cv2.imencode(
            ".jpg",
            prepared,
            [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality],
        )
        if not ok:
            return None
        return buffer.tobytes()

    def _flush_frame(self, frame_bytes):
        if not frame_bytes:
            return ""
        name = f"{self._frame_index:08d}.jpg"
        path = os.path.join(self.frames_dir, name)
        try:
            with open(path, "wb") as file:
                file.write(frame_bytes)
        except OSError:
            return ""
        self._frame_index += 1
        self._frames_written += 1
        return os.path.join("frames", name)

    def _keep_frame(self, record, human_ahead):
        if not self.save_frames:
            return False
        if self.frame_policy == "all":
            return True
        if self.frame_policy == "none":
            return False
        return bool(record.get("human_present") or record.get("human_override") or human_ahead)

    # -- 写入 ---------------------------------------------------------------
    def write(self, record, frame=None, frame_bytes=None):
        """写入一步记录。

        记录会先进入缓冲，等“未来 `preroll_steps` 步”都确定后才知道
        这一段是否值得保留帧，因此调用方不需要自己处理预卷。
        """
        if self._closed:
            raise RuntimeError("AssistDatasetWriter is closed")

        record = dict(record)
        record.setdefault("schema", ASSIST_SCHEMA)
        record.setdefault("episode_id", self.episode_id)
        if frame_bytes is None:
            frame_bytes = self.encode_frame(frame)

        item = {
            "record": record,
            "frame_bytes": frame_bytes,
            "human_present": bool(record.get("human_present")),
            "human_override": bool(record.get("human_override")),
        }
        self._pending.append(item)

        while len(self._pending) > self.preroll_steps:
            self._finalize_one()

        return record

    def _finalize_one(self):
        item = self._pending.popleft()
        record = item["record"]
        human_ahead = any(entry["human_present"] for entry in self._pending)
        if self._keep_frame(record, human_ahead):
            relative = self._flush_frame(item["frame_bytes"])
            if relative:
                record["frame_path"] = os.path.join(self.episode_dir, relative)
                record["frame_saved"] = True
            else:
                record["frame_path"] = ""
                record["frame_saved"] = False
        else:
            record["frame_path"] = record.get("frame_path", "")
            record["frame_saved"] = False

        # 预卷帧已经用完，及时释放
        item["frame_bytes"] = None

        self._file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._written += 1
        if record.get("human_present"):
            self._human_steps += 1
        if record.get("human_override"):
            self._override_steps += 1
        if self._written % self.flush_every == 0:
            self.flush()

    def flush(self):
        try:
            self._file.flush()
        except (OSError, ValueError):
            pass

    def close(self):
        if self._closed:
            return
        while self._pending:
            self._finalize_one()
        self.flush()
        try:
            self._file.close()
        except (OSError, ValueError):
            pass
        self._closed = True
        self.metadata["closed_at"] = _now_iso()
        self._write_metadata()

    def stats(self):
        return {
            "episode_dir": self.episode_dir,
            "samples_path": self.samples_path,
            "written_steps": self._written,
            "human_steps": self._human_steps,
            "override_steps": self._override_steps,
            "frames_written": self._frames_written,
            "pending": len(self._pending),
            "frame_policy": self.frame_policy,
        }

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def write_latest_pointers(human_data_dir, episode_dir, save_frames=True):
    """维护 `latest_*_episode.txt` 指针，保持与旧脚本的约定一致。"""
    human_data_dir = os.path.abspath(local_path(human_data_dir))
    os.makedirs(human_data_dir, exist_ok=True)

    for name in ("latest_episode.txt", "latest_action_episode.txt"):
        with open(os.path.join(human_data_dir, name), "w", encoding="utf-8") as file:
            file.write(episode_dir)
    if save_frames:
        with open(os.path.join(human_data_dir, "latest_frame_episode.txt"), "w", encoding="utf-8") as file:
            file.write(episode_dir)


# --------------------------------------------------------------------------- #
# 读取端
# --------------------------------------------------------------------------- #
def resolve_sample_files(source):
    """把“文件 / 目录 / 逗号分隔列表”统一解析成 jsonl 文件列表。"""
    if not source:
        return []

    if isinstance(source, (list, tuple, set)):
        files = []
        for item in source:
            files.extend(resolve_sample_files(item))
        return _dedupe(files)

    raw = str(source)
    if "," in raw or ";" in raw:
        files = []
        for part in raw.replace(";", ",").split(","):
            part = part.strip()
            if part:
                files.extend(resolve_sample_files(part))
        return _dedupe(files)

    path = local_path(raw)
    if os.path.isfile(path):
        return [os.path.abspath(path)]

    if os.path.isdir(path):
        found = []
        for name in ALL_FILE_NAMES:
            candidate = os.path.join(path, name)
            if os.path.isfile(candidate):
                found.append(os.path.abspath(candidate))
        if found:
            return found

        # 目录下可能还有子目录（每个 episode 一个子目录）
        for entry in sorted(os.listdir(path)):
            child = os.path.join(path, entry)
            if os.path.isdir(child):
                for name in ALL_FILE_NAMES:
                    candidate = os.path.join(child, name)
                    if os.path.isfile(candidate):
                        found.append(os.path.abspath(candidate))
                        break
        return _dedupe(found)

    return []


def _dedupe(items):
    seen = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def latest_episode_dir(human_data_dir="human_data", prefer_frames=False):
    human_data_dir = os.path.abspath(local_path(human_data_dir))
    names = ["latest_frame_episode.txt", "latest_episode.txt"] if prefer_frames else [
        "latest_action_episode.txt",
        "latest_episode.txt",
    ]
    for name in names:
        pointer = os.path.join(human_data_dir, name)
        if not os.path.exists(pointer):
            continue
        try:
            with open(pointer, "r", encoding="utf-8") as file:
                episode_dir = file.read().strip()
        except OSError:
            continue
        if episode_dir:
            return os.path.abspath(episode_dir)
    return ""


def _load_metadata(samples_path):
    metadata_path = os.path.join(os.path.dirname(os.path.abspath(samples_path)), "metadata.json")
    if not os.path.exists(metadata_path):
        return {}
    try:
        with open(metadata_path, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def normalize_record(record, samples_path="", metadata=None):
    """把新旧两种格式统一成同一套字段。

    旧格式（`samples.jsonl`）里每一帧都来自真人操作，因此 `human_act` 直接取
    `parsed_action`；新格式里 `human_act` 只有真人操作的那几步才有值。
    """
    metadata = metadata or {}
    if not isinstance(record, dict):
        return None

    schema = int(record.get("schema", 1) or 1)
    legacy = schema < ASSIST_SCHEMA or "act" not in record

    if legacy:
        action = clamp_action(record.get("parsed_action") or record.get("action"))
        # 旧格式每一帧都来自真人操作，因此动作本身就是人的标签
        human_act = action
        ai_act = clamp_action(record.get("ai_act")) if record.get("ai_act") is not None else None
        human_present = bool(record.get("human_present", True))
        human_override = bool(record.get("human_override", True))
        timestamp = record.get("timestamp", record.get("ts", 0.0))
        frame_path = record.get("frame_path") or ""
        obs = record.get("obs") or {}
        phase = record.get("phase") or ""
        pattern = "legacy_human"
    else:
        action = clamp_action(record.get("act"))
        human_act = clamp_action(record["human_act"]) if record.get("human_act") is not None else None
        ai_act = clamp_action(record["ai_act"]) if record.get("ai_act") is not None else None
        human_present = bool(record.get("human_present", False))
        human_override = bool(record.get("human_override", False))
        timestamp = record.get("ts", record.get("timestamp", 0.0))
        frame_path = record.get("frame_path") or ""
        obs = record.get("obs") or {}
        phase = record.get("phase") or ""
        pattern = "assist"

    try:
        timestamp = float(timestamp)
    except (TypeError, ValueError):
        timestamp = 0.0

    return {
        "schema": schema,
        "pattern": pattern,
        "source": os.path.abspath(samples_path) if samples_path else "",
        "episode_id": record.get("episode_id") or metadata.get("episode_id") or "",
        "step": int(record.get("step", record.get("frame_id", -1)) if record.get("step", record.get("frame_id")) is not None else -1),
        "timestamp": timestamp,
        "acts": action,
        "human_act": human_act,
        "ai_act": ai_act,
        "human_present": human_present,
        "human_override": human_override,
        "assist_mode": record.get("assist_mode", ""),
        "hold_ms": int(record.get("hold_ms", 0) or 0),
        "phase": phase,
        "lane_phase": record.get("lane_phase", ""),
        "macro": record.get("macro", ""),
        "phase_action_name": record.get("phase_action_name", ""),
        "reward": float(record.get("reward", 0.0) or 0.0),
        "done": int(record.get("done", 0) or 0),
        "event": record.get("event"),
        "obs": {
            "hp": _maybe_float(obs.get("hp")),
            "pressure": _maybe_float(obs.get("pressure"), 0.0),
            "target_dist": _maybe_float(obs.get("target_dist")),
            "target_visible": bool(obs.get("target_visible", False)),
            "mm_lane_arrived": bool(obs.get("mm_lane_arrived", False)),
            "mm_enemy_visible": bool(obs.get("mm_enemy_visible", False)),
            "dead": bool(obs.get("dead", False)),
        },
        "frame_path": frame_path,
        "frame_saved": bool(record.get("frame_saved", bool(frame_path))),
        "metadata": metadata,
        "raw": record,
    }


def _maybe_float(value, default=None):
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def iter_records(source, require_frame=False, limit=0):
    """逐个产出规范化记录。"""
    emitted = 0
    for samples_path in resolve_sample_files(source):
        metadata = _load_metadata(samples_path)
        with open(samples_path, "r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                normalized = normalize_record(record, samples_path, metadata)
                if normalized is None:
                    continue
                if require_frame:
                    frame_path = normalized.get("frame_path")
                    if not frame_path or not os.path.exists(frame_path):
                        continue
                yield normalized
                emitted += 1
                if limit and emitted >= limit:
                    return


def load_records(source, require_frame=False, limit=0):
    return list(iter_records(source, require_frame=require_frame, limit=limit))


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #
def summarize_records(records):
    total = 0
    human_steps = 0
    override_steps = 0
    saved_frames = 0
    episodes = set()
    phases = Counter()
    attack_actions = Counter()
    info_actions = Counter()
    move_actions = Counter()
    events = Counter()
    first_ts = None
    last_ts = None

    for record in records:
        total += 1
        if record["human_present"]:
            human_steps += 1
        if record["human_override"]:
            override_steps += 1
        if record.get("frame_saved"):
            saved_frames += 1
        if record.get("episode_id"):
            episodes.add(record["episode_id"])
        phases[record.get("phase") or "unknown"] += 1
        action = record["human_act"] if record["human_act"] is not None else record["acts"]
        move_actions[action[0]] += 1
        info_actions[action[2]] += 1
        attack_actions[action[3]] += 1
        if record.get("event"):
            events[record["event"]] += 1
        timestamp = record.get("timestamp") or 0.0
        if timestamp > 0:
            first_ts = timestamp if first_ts is None else min(first_ts, timestamp)
            last_ts = timestamp if last_ts is None else max(last_ts, timestamp)

    duration = 0.0 if first_ts is None else max(0.0, last_ts - first_ts)
    return {
        "total_steps": total,
        "human_steps": human_steps,
        "override_steps": override_steps,
        "human_ratio": round(human_steps / total, 4) if total else 0.0,
        "saved_frames": saved_frames,
        "episodes": sorted(episodes),
        "episode_count": len(episodes),
        "duration_sec": round(duration, 1),
        "phases": dict(phases),
        "move": dict(move_actions),
        "info": dict(info_actions),
        "attack": dict(attack_actions),
        "events": dict(events),
    }


def make_record(
    step,
    ts,
    act,
    ai_act=None,
    human_act=None,
    human_present=False,
    human_override=False,
    assist_mode="",
    hold_ms=0,
    phase="",
    lane_phase="",
    macro="",
    phase_action_name="",
    reward=0.0,
    done=0,
    event=None,
    obs=None,
    episode_id="",
):
    """构造一条符合 schema 2 的记录，避免各处手写字典漏字段。"""
    return {
        "schema": ASSIST_SCHEMA,
        "episode_id": episode_id,
        "step": int(step),
        "ts": round(float(ts), 4),
        "act": clamp_action(act),
        "ai_act": clamp_action(ai_act) if ai_act is not None else None,
        "human_act": clamp_action(human_act) if human_act is not None else None,
        "human_present": bool(human_present),
        "human_override": bool(human_override),
        "assist_mode": str(assist_mode or ""),
        "hold_ms": int(hold_ms or 0),
        "phase": str(phase or ""),
        "lane_phase": str(lane_phase or ""),
        "macro": str(macro or ""),
        "phase_action_name": str(phase_action_name or ""),
        "reward": round(float(reward or 0.0), 4),
        "done": int(done or 0),
        "event": event,
        "obs": dict(obs or {}),
        "frame_path": "",
        "frame_saved": False,
    }


def observation_summary(info, minimap_state=None, phase_info=None):
    """从 `env.step` 的 info 里提取少量关键观测量，供 profile 分析与日志使用。"""
    info = info or {}
    phase_info = phase_info or {}
    minimap_state = minimap_state or {}
    hp = phase_info.get("phase_own_hp_ratio")
    if hp is None:
        hp = info.get("lane_laning_own_hp_ratio")
    return {
        "hp": _maybe_float(hp),
        "pressure": round(float(phase_info.get("phase_center_pressure_score", 0.0) or 0.0), 4),
        "target_dist": _maybe_float(phase_info.get("phase_target_distance_ratio")),
        "target_visible": bool(phase_info.get("phase_target_visible", False)),
        "mm_lane_arrived": bool(minimap_state.get("minimap_lane_arrived", False)),
        "mm_enemy_visible": bool(minimap_state.get("minimap_enemy_visible", False)),
        "dead": bool(minimap_state.get("dead_screen_dark", False)),
    }


def numpy_frame_to_jpeg(frame, max_width=0, quality=78):
    """给外部（例如采集脚本）复用的一次性编码工具。"""
    if frame is None or getattr(frame, "size", 0) == 0:
        return None
    if max_width and frame.shape[1] > max_width:
        scale = max_width / float(frame.shape[1])
        frame = cv2.resize(
            frame,
            (max_width, max(1, int(round(frame.shape[0] * scale)))),
            interpolation=cv2.INTER_AREA,
        )
    ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    return buffer.tobytes() if ok else None


def decode_jpeg(data):
    if not data:
        return None
    array = np.frombuffer(data, np.uint8)
    return cv2.imdecode(array, cv2.IMREAD_COLOR)


def create_episode_writer(
    root_dir,
    episode_id=None,
    frame_policy="human",
    frame_max_width=640,
    jpeg_quality=78,
    preroll_steps=4,
    pointer_dir=None,
    metadata=None,
):
    """为一局对局创建一个新的示范数据集目录。

    目录命名 `<root_dir>/<YYYYmmdd_HHMMSS>_assist`，同时更新
    `latest_*_episode.txt` 指针，保证 `build_game_profile.py` /
    `train_behavior_clone.py` 不传参数也能找到最新数据。
    """
    root_dir = os.path.abspath(local_path(root_dir))
    os.makedirs(root_dir, exist_ok=True)

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = episode_id or f"{stamp}_assist"
    name = base_name
    suffix = 0
    # 目录已存在且里面已经有数据时自动加序号，避免多局示范混在同一个文件里
    while os.path.exists(os.path.join(root_dir, name, ASSIST_FILE_NAME)):
        suffix += 1
        name = f"{base_name}_{suffix}"

    episode_dir = os.path.join(root_dir, name)
    writer = AssistDatasetWriter(
        episode_dir=episode_dir,
        episode_id=name,
        frame_policy=frame_policy,
        frame_max_width=frame_max_width,
        jpeg_quality=jpeg_quality,
        save_frames=frame_policy != "none",
        preroll_steps=preroll_steps,
        metadata=metadata,
    )

    save_frames = writer.save_frames
    write_latest_pointers(root_dir, episode_dir, save_frames=save_frames)
    if pointer_dir:
        pointer_dir = os.path.abspath(local_path(pointer_dir))
        if pointer_dir != root_dir:
            write_latest_pointers(pointer_dir, episode_dir, save_frames=save_frames)
    return writer

