"""整场对局的实时人为辅助层。

`wzry` 原版只有“开局走线”这一步是人为示范的：先离线采一段路线，
`build_lane_profile.py` 压成 `src/lane_profile.json`，运行时 `lane_macro.py`
在开局前几十步回放这条路线，之后完全交给模型。

本模块把人为辅助推广到整场对局：

* **assist 模式**：AI 一直在打，人随时可以伸手接管（推一下摇杆、点一下技能、
  或者干脆自己打完一波团），程序立刻停止注入任何操作，把控制权干净地让给人；
  松手 `--assist_release_grace_steps` 步后自动还给 AI。
* **manual 模式**：整局都由人来打，AI 只观测、只算参考动作，不注入任何输入。
  用来一次录完一整场高质量示范，直接喂给行为克隆。
* **partial 模式**（`--assist_partial`）：只接管人碰到的那几个分支。
  例如人只碰摇杆，就只接管移动，普攻/技能仍由 AI 补，适合“只教走位”。

所有模式下，每一步都会记录 AI 想做什么、人做了什么、执行了什么，
供 `assist_dataset.py` 落盘，再由行为克隆（监督）和 DQN 的辅助模仿损失（强化）共同使用。

实现要点：

1. 触摸来源是 `adb shell getevent`。`adb shell input` 走 InputManager 注入，
   不会回灌到 evdev，所以程序自己的注入不会被误判成人为操作；
   只有 `persistent_minitouch` 这种 uinput 方案才需要 `--assist_echo_guard`。
2. 是否“人在操作”按“当前有没有手指按下”判定，延迟只取决于 getevent 的读线程，
   和截图/推理耗时无关。
3. 接管期间强制 `stop_persistent_move()` 并且不注入任何 tap，
   避免程序和人的手指抢同一个摇杆。
"""

import json
import os
import subprocess
import threading
import time
from collections import deque

import assist_dataset
from argparses import args
from touch_event_parser import MultiTouchState, TouchActionMapper

ASSIST_MODES = ("off", "assist", "manual")
STATE_IDLE = "idle"
STATE_HUMAN = "human"
STATE_RELEASING = "releasing"

ZERO_ACTION = [0, 0, 0, 0, 0, 0, 0, 0]

#: 人碰了哪个分支，就只接管哪个分支
BRANCH_MOVE = "move"
BRANCH_INFO = "info"
BRANCH_ATTACK = "attack"


class HumanTouchStream:
    """后台读取 `adb shell getevent -lt`，维护一份近期的触摸样本环形缓冲。"""

    def __init__(self, adb_path, device_serial, touch_device="", capacity=4000, auto_start=True,
                 debug=False, debug_limit=40, max_restarts=50, restart_interval=2.0):
        self.adb_path = adb_path
        self.device_serial = device_serial
        self.touch_device = touch_device or ""
        self.parser = MultiTouchState()
        self.samples = deque(maxlen=max(256, int(capacity)))
        self.active_samples = {}
        self.lock = threading.Lock()
        self.start_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.process = None
        self.thread = None
        self.raw_line_count = 0
        self.parsed_sample_count = 0
        self.last_touch_ts = 0.0
        self.last_error = ""
        self.started = False
        self.closed = False
        self.restart_count = 0
        self.max_restarts = max(0, int(max_restarts))
        self.restart_interval = max(0.2, float(restart_interval))
        self.last_restart_at = 0.0
        self.debug = bool(debug)
        self.debug_limit = max(0, int(debug_limit))

        if auto_start:
            self.start()

    # -- 生命周期 -----------------------------------------------------------
    def start(self):
        with self.start_lock:
            if self.started and self.process is not None and self.process.poll() is None:
                return True
            self.started = False
            command = [self.adb_path, "-s", self.device_serial, "shell", "getevent", "-lt"]
            if self.touch_device:
                command.append(self.touch_device)
            try:
                self.process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
            except (OSError, ValueError) as exc:
                self.last_error = f"启动 getevent 失败: {exc}"
                return False

            self.thread = threading.Thread(target=self._read_loop, name="getevent", daemon=True)
            self.thread.start()
            self.started = True
            time.sleep(0.2)
            if self.process.poll() is not None:
                self.last_error = (
                    f"getevent 立即退出 returncode={self.process.poll()}；"
                    "请先运行 touch_event_diagnose.py 确认触摸设备"
                )
                self.started = False
                return False
            return True

    def ensure_alive(self):
        """触摸流健康检查：getevent 意外退出时自动重启。

        `adb shell getevent` 在长时间运行、宿主 adb 重启、模拟器负载高等情况下
        会静默退出；没有自愈的话人为辅助会在某一刻之后再也没反应。
        """
        if self.closed or not getattr(self, "start_lock", None):
            return self.started
        alive = self.started and self.process is not None and self.process.poll() is None
        if alive:
            return True
        if self.restart_count >= self.max_restarts:
            return False
        now = time.time()
        if now - self.last_restart_at < self.restart_interval:
            return False
        self.last_restart_at = now
        self.restart_count += 1
        print(f"[assist] 触摸流已中断，正在重启 getevent（第 {self.restart_count} 次）")
        ok = self.start()
        if not ok:
            print(f"[assist] getevent 重启失败: {self.last_error}")
        return ok

    def _read_loop(self):
        while not self.stop_event.is_set():
            process = self.process
            if process is None or process.stdout is None:
                break
            line = process.stdout.readline()
            if not line:
                # 宿主 adb 断开时 readline 会一直返回空串，靠 poll 判断真死
                if process.poll() is not None:
                    break
                time.sleep(0.01)
                continue
            self.feed_line(line, time.time())
        self.started = False
        self.last_error = self.last_error or "getevent reader 已退出"

    def feed_line(self, line, host_ts):
        """解析一行 getevent 输出；单元测试可以直接调用它注入数据。"""
        self.raw_line_count += 1
        if self.debug and self.raw_line_count <= self.debug_limit:
            print(f"[touch] raw#{self.raw_line_count}: {line.rstrip()!r}")
        parsed = self.parser.process_line(line.rstrip("\r\n"), host_ts)
        if not parsed:
            return []
        with self.lock:
            for sample in parsed:
                self.parsed_sample_count += 1
                self.samples.append(sample)
                key = (sample.get("slot", 0), sample.get("tracking_id") or sample.get("slot", 0))
                if sample.get("active"):
                    self.active_samples[key] = sample
                else:
                    self.active_samples.pop(key, None)
                self.last_touch_ts = host_ts
        return parsed

    def stop(self):
        self.closed = True
        self.stop_event.set()
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=1)
            except (OSError, subprocess.SubprocessError):
                try:
                    self.process.kill()
                except OSError:
                    pass
        if self.thread is not None:
            self.thread.join(timeout=1)
        self.started = False

    # -- 查询 ---------------------------------------------------------------
    def has_active_touch(self):
        with self.lock:
            return bool(self.active_samples)

    def active_touch_count(self):
        with self.lock:
            return len(self.active_samples)

    def snapshot_window(self, start_ts, end_ts):
        """取时间窗内的样本，并为“一直按着但没新事件”的手指补一条 hold 样本。"""
        with self.lock:
            window = [dict(sample) for sample in self.samples if start_ts <= sample.get("host_ts", 0.0) <= end_ts]
            existing = {
                (sample.get("slot", 0), sample.get("tracking_id") or sample.get("slot", 0))
                for sample in window
            }
            for key, sample in self.active_samples.items():
                if key in existing or sample.get("host_ts", 0.0) > end_ts:
                    continue
                held = dict(sample)
                held["host_ts"] = start_ts
                held["phase"] = "hold"
                window.append(held)
            window.sort(key=lambda item: item.get("host_ts", 0.0))
            return window

    def stats(self):
        return {
            "raw_lines": self.raw_line_count,
            "parsed_samples": self.parsed_sample_count,
            "active_touches": self.active_touch_count(),
            "started": self.started,
            "restarts": self.restart_count,
            "last_error": self.last_error,
        }


class HumanObservation:
    """一次轮询的结果。"""

    __slots__ = ("present", "action", "mask", "sample_count", "hold_ms", "active_touches", "diagnostics")

    def __init__(self, present=False, action=None, mask=None, sample_count=0, hold_ms=0, active_touches=0, diagnostics=None):
        self.present = bool(present)
        self.action = list(action) if action is not None else list(ZERO_ACTION)
        self.mask = dict(mask or {BRANCH_MOVE: False, BRANCH_INFO: False, BRANCH_ATTACK: False})
        self.sample_count = int(sample_count)
        self.hold_ms = int(hold_ms)
        self.active_touches = int(active_touches)
        self.diagnostics = diagnostics or {}

    def touched_branches(self):
        return [name for name, hit in self.mask.items() if hit]

    def __repr__(self):
        return (
            f"HumanObservation(present={self.present}, action={self.action}, "
            f"mask={self.mask}, hold_ms={self.hold_ms})"
        )


class LiveHumanAssist:
    """把“人随时可以接管”封装成 env.step 里的一次仲裁调用。"""

    def __init__(self, tool=None, screen_size=None, mode=None, start_stream=True):
        self.tool = tool
        self.mode = str(mode if mode is not None else getattr(args, "assist_mode", "off")).lower()
        if self.mode not in ASSIST_MODES:
            self.mode = "off"
        self.enabled = self.mode != "off"

        self.window_ms = max(60, int(getattr(args, "assist_touch_window_ms", 400)))
        self.release_grace_steps = max(0, int(getattr(args, "assist_release_grace_steps", 2)))
        self.partial = bool(getattr(args, "assist_partial", False))
        self.auto_resume = bool(getattr(args, "assist_auto_resume", True))
        self.echo_guard_ms = int(getattr(args, "assist_echo_guard", 0) or 0)

        self.state = STATE_IDLE
        self.release_steps = 0
        self.episode_step = 0
        self.episode_human_steps = 0
        self.episode_override_steps = 0
        self.episode_takeovers = 0
        self.last_observation = HumanObservation()
        self.last_human_action = None
        self.resume_steps_left = 0
        self.last_info = {}
        self.started_at = time.time()
        self.stream = None
        self.touch_error = ""
        self.dataset = None
        self.episode_id = ""

        if not self.enabled:
            self.last_info = self._info(False, "", None)
            return

        size = screen_size
        if size is None and tool is not None:
            size = (tool.actual_width, tool.actual_height)
        size = size or (2800, 1260)
        self.mapper = TouchActionMapper(screen_width=size[0], screen_height=size[1])

        if start_stream:
            self._start_stream(size)

        # minitouch 走 uinput，会回灌到 getevent，必须开回声保护
        if self.echo_guard_ms <= 0 and str(getattr(args, "move_control_mode", "")).startswith("persistent_minitouch"):
            self.echo_guard_ms = 120
            print("[assist] persistent_minitouch 会回灌 getevent，已自动开启回声保护")

        self.last_info = self._info(False, "", None)

    # -- 触摸流 -------------------------------------------------------------
    def _start_stream(self, size):
        creator = getattr(self.tool, "create_touch_stream", None)
        if callable(creator):
            # 双端模式：程序端不 spawn adb，触摸行由设备端转发过来
            try:
                self.stream = creator(
                    debug=bool(getattr(args, "assist_debug_touch", False)),
                    debug_limit=int(getattr(args, "assist_debug_touch_limit", 40) or 40),
                    touch_device=getattr(args, "touch_device", ""),
                )
                if self.stream is not None and not self.stream.started:
                    self.stream.start()
            except Exception as exc:  # noqa: BLE001 - 触摸不可用不应中断训练
                self.stream = None
                self.touch_error = f"设备端触摸流不可用: {exc}"
            if self.stream is None or not getattr(self.stream, "started", False):
                self.touch_error = self.touch_error or "设备端未转发触摸"
                self.stream = None
                self._print_stream_fallback()
            return

        adb_path = os.path.join(getattr(self.tool, "scrcpy_dir", "scrcpy-win64-v2.0"), "adb")
        device_serial = getattr(self.tool, "device_serial", None) or args.iphone_id
        self.stream = HumanTouchStream(
            adb_path=adb_path,
            device_serial=device_serial,
            touch_device=getattr(args, "touch_device", ""),
            debug=bool(getattr(args, "assist_debug_touch", False)),
            debug_limit=int(getattr(args, "assist_debug_touch_limit", 40) or 40),
        )
        if not self.stream.started:
            self.touch_error = self.stream.last_error or "触摸监听未启动"
            self.stream = None
            self._print_stream_fallback()

    def _print_stream_fallback(self):
        print("")
        print("[assist] 未能启动 adb getevent 触摸监听，人为辅助降级为“仅记录 AI 动作”。")
        print(f"[assist] 原因: {self.touch_error}")
        print("[assist] 排查: 先跑 python touch_event_diagnose.py --seconds 8，确认哪个 /dev/input/eventX 有输出。")
        print("")

    def stop(self):
        if self.stream is not None:
            self.stream.stop()

    # -- 轮询与仲裁 ---------------------------------------------------------
    def poll(self):
        """读取当前人为操作状态。低开销，每步调用一次。"""
        if not self.enabled or self.stream is None:
            self.last_observation = HumanObservation()
            return self.last_observation

        now = time.time()
        present = self.stream.has_active_touch()
        action = list(ZERO_ACTION)
        mask = {BRANCH_MOVE: False, BRANCH_INFO: False, BRANCH_ATTACK: False}
        sample_count = 0
        diagnostics = {}
        hold_ms = 0

        recent_enough = present or (now - self.stream.last_touch_ts) * 1000.0 <= self.window_ms
        if recent_enough and self._echo_allowed():
            samples = self.stream.snapshot_window(now - self.window_ms / 1000.0, now)
            sample_count = len(samples)
            if samples:
                action, diagnostics = self.mapper.samples_to_action(samples)
                mask = {
                    BRANCH_MOVE: bool(diagnostics.get("matched_move")),
                    BRANCH_INFO: bool(action[2]),
                    BRANCH_ATTACK: bool(action[3]),
                }
            if present:
                hold_ts = min(
                    (sample.get("host_ts", now) for sample in self.stream.active_samples.values()),
                    default=now,
                )
                hold_ms = int(max(0.0, (now - hold_ts) * 1000.0))

        observation = HumanObservation(
            present=present,
            action=action,
            mask=mask,
            sample_count=sample_count,
            hold_ms=hold_ms,
            active_touches=self.stream.active_touch_count(),
            diagnostics=diagnostics,
        )
        self.last_observation = observation
        return observation

    def _echo_allowed(self):
        """挡住程序自己注入的触摸（minitouch / uinput 方案）。"""
        if self.echo_guard_ms <= 0 or self.tool is None:
            return True
        last_input_ts = getattr(self.tool, "last_input_ts", 0.0)
        if not last_input_ts:
            return True
        return (time.time() - last_input_ts) * 1000.0 > self.echo_guard_ms

    def holds_control(self):
        if not self.enabled:
            return False
        if self.mode == "manual":
            return True
        return self.state in (STATE_HUMAN, STATE_RELEASING)

    def will_take_over(self, observation=None):
        """预判这一步会不会由人接管。

        `env.step` 需要在跑宏/阶段控制器**之前**就知道要不要冻结它们，
        所以这里单独给一个不修改状态机的预测；判定条件与 `arbitrate()`
        里的状态迁移完全一致。
        """
        if not self.enabled:
            return False
        if self.mode == "manual":
            return True
        observation = observation if observation is not None else self.last_observation
        if observation.present:
            return True
        if not self.auto_resume and self.episode_takeovers > 0:
            return True
        if self.state in (STATE_HUMAN, STATE_RELEASING):
            return True
        return bool(self.resume_steps_left > 0)

    # -- 数据集 -------------------------------------------------------------
    def attach_dataset(self, writer):
        self.dataset = writer
        self.episode_id = getattr(writer, "episode_id", "")

    def detach_dataset(self, close=True):
        writer = getattr(self, "dataset", None)
        self.dataset = None
        if writer is not None and close:
            try:
                writer.close()
            except Exception:
                pass
        return writer

    def record_step(
        self,
        step,
        frame,
        learned_action,
        ai_action,
        human_action,
        human_present,
        human_control,
        phase_info,
        lane_info,
        minimap_state,
        info,
        reward,
        done,
    ):
        """把这一步写进整场示范数据集；没有挂数据集时直接返回。"""
        writer = getattr(self, "dataset", None)
        if writer is None:
            return None
        try:
            record = assist_dataset.make_record(
                step=step,
                ts=time.time(),
                act=learned_action,
                ai_act=ai_action,
                human_act=human_action,
                human_present=human_present,
                human_override=human_control,
                assist_mode=self.mode,
                hold_ms=self.last_observation.hold_ms,
                phase=(phase_info or {}).get("phase", ""),
                lane_phase=(lane_info or {}).get("lane_macro_phase", ""),
                macro=(lane_info or {}).get("lane_macro_name", ""),
                phase_action_name=(phase_info or {}).get("phase_action_name", ""),
                reward=reward,
                done=done,
                event=(info or {}).get("event"),
                obs=assist_dataset.observation_summary(info, minimap_state, phase_info),
                episode_id=self.episode_id,
            )
            return writer.write(record, frame=frame)
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"[assist] 示范数据写入失败: {exc}")
            return None

    def arbitrate(self, ai_action, observation=None):
        """决定这一步实际学什么动作、注入什么动作。

        返回 `(learned_action, inject_action, human_control, info)`：

        * `learned_action`：这一步在游戏里真实发生的动作，用来写经验/做模仿；
          人为接管时就是人的动作。
        * `inject_action`：程序要注入的 adb 操作；人为接管时是全 0，
          因为人的手指已经把事情做了。
        * `human_control`：True 表示这一步必须禁止任何自动注入（包括 combat_assist）。
        """
        ai_action = [int(v) for v in ai_action]
        observation = observation if observation is not None else self.last_observation

        if not self.enabled:
            info = self._info(False, STATE_IDLE, None)
            self._advance()
            return ai_action, ai_action, False, info

        # 状态机
        if self.mode == "manual":
            human_control = True
            if observation.present:
                self.state = STATE_HUMAN
        elif observation.present:
            if self.state != STATE_HUMAN:
                self.episode_takeovers += 1
            self.state = STATE_HUMAN
            self.release_steps = 0
        elif self.state == STATE_HUMAN:
            self.state = STATE_RELEASING
            self.release_steps = 0

        if self.state == STATE_RELEASING:
            self.release_steps += 1
            if self.release_steps > self.release_grace_steps:
                self.state = STATE_IDLE
                self.resume_steps_left = max(0, int(getattr(args, "assist_resume_grace_steps", 1)))

        if not self.auto_resume and self.episode_takeovers > 0:
            # "我先打一段，后面别让 AI 插手"：第一次接管之后本局不再交还
            human_control = True
        elif self.state == STATE_IDLE and self.resume_steps_left > 0:
            self.resume_steps_left -= 1
            human_control = True
        else:
            human_control = self.holds_control()

        if self.mode == "manual":
            human_control = True

        learned_action = ai_action
        inject_action = ai_action
        human_act_for_log = None

        if human_control:
            inject_action = list(ZERO_ACTION)
            if observation.present:
                human_act_for_log = list(observation.action)
                if self.partial:
                    learned_action = self._compose_partial(ai_action, observation)
                else:
                    learned_action = list(observation.action)
                self.last_human_action = list(observation.action)
            else:
                # 松手后的释放窗口：游戏里这一步没有任何操作
                learned_action = list(ZERO_ACTION)

        info = self._info(human_control, self.state, human_act_for_log, learned_action, ai_action, observation)
        self._advance()
        return learned_action, inject_action, human_control, info

    def _compose_partial(self, ai_action, observation):
        """只接管人碰到的分支，没碰的分支继续用 AI 的输出。"""
        composed = [int(v) for v in ai_action]
        if observation.mask.get(BRANCH_MOVE):
            composed[0] = observation.action[0]
            composed[1] = observation.action[1]
        if observation.mask.get(BRANCH_INFO) or observation.action[2]:
            composed[2] = observation.action[2]
        if observation.mask.get(BRANCH_ATTACK) or observation.action[3]:
            composed[3] = observation.action[3]
            composed[4] = observation.action[4]
            composed[5] = observation.action[5]
            composed[6] = observation.action[6]
            composed[7] = observation.action[7]
        if self.mode == "manual" and not observation.present:
            # 整场示范模式下人手离开屏幕，就当作“什么都不做”，不要用 AI 的动作补
            return list(ZERO_ACTION)
        return composed

    def _advance(self):
        self.episode_step += 1
        if self.last_observation.present:
            self.episode_human_steps += 1
        if self.state in (STATE_HUMAN, STATE_RELEASING) or self.mode == "manual":
            self.episode_override_steps += 1

    # -- 对外信息 -----------------------------------------------------------
    def _info(self, human_control, state, human_act, learned_action=None, ai_action=None, observation=None):
        observation = observation or self.last_observation
        self.last_info = {
            "assist_enabled": bool(self.enabled),
            "assist_mode": self.mode,
            "assist_state": state,
            "assist_human_control": bool(human_control),
            "assist_human_present": bool(observation.present),
            "assist_human_hold_ms": int(observation.hold_ms),
            "assist_human_samples": int(observation.sample_count),
            "assist_active_touches": int(observation.active_touches),
            "assist_branch_mask": dict(observation.mask),
            "assist_partial": bool(getattr(self, "partial", False)),
            "assist_human_act": list(human_act) if human_act is not None else None,
            "assist_learned_act": list(learned_action) if learned_action is not None else None,
            "assist_ai_act": list(ai_action) if ai_action is not None else None,
            "assist_episode_step": int(self.episode_step),
            "assist_episode_human_steps": int(self.episode_human_steps),
            "assist_episode_override_steps": int(self.episode_override_steps),
            "assist_episode_takeovers": int(self.episode_takeovers),
            "assist_touch_error": self.touch_error,
            "assist_touch_stats": self.stream.stats() if self.stream is not None else {},
        }
        return self.last_info

    def info(self):
        return dict(self.last_info)

    def reset_episode(self):
        self.state = STATE_IDLE
        self.release_steps = 0
        self.episode_step = 0
        self.episode_human_steps = 0
        self.episode_override_steps = 0
        self.episode_takeovers = 0
        self.resume_steps_left = 0
        self.last_observation = HumanObservation()
        self.last_human_action = None

    def suggest_burst_ms(self, base_ms):
        """人为辅助开启时压缩单步 burst，降低接管延迟。"""
        if not self.enabled:
            return base_ms
        fast_ms = int(getattr(args, "assist_preempt_burst_duration_ms", 0) or 0)
        if fast_ms <= 0:
            return base_ms
        return max(120, min(int(base_ms), fast_ms))

    def summary(self):
        stats = self.stream.stats() if self.stream is not None else {}
        return {
            "mode": self.mode,
            "enabled": self.enabled,
            "state": self.state,
            "human_steps": self.episode_human_steps,
            "override_steps": self.episode_override_steps,
            "takeovers": self.episode_takeovers,
            "partial": self.partial,
            "auto_resume": self.auto_resume,
            "touch_error": self.touch_error,
            **stats,
        }

    def describe(self):
        return json.dumps(self.summary(), ensure_ascii=False)
