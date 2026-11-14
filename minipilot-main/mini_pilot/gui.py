"""Tkinter GUI wrapper for MiniPilot v1.5 CLI."""

from __future__ import annotations

import argparse
import json
import importlib.util
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, TOP, BOTTOM, X, Y, Button, Entry, Frame, Label, PanedWindow, Radiobutton, StringVar, Text, Tk, Toplevel
from tkinter import messagebox, simpledialog
from tkinter.ttk import Combobox


AUTO_APP_SELECTION_MODE = "auto"
MANUAL_APP_SELECTION_MODE = "manual"


class MiniPilotGui:
    """A small GUI that wraps the existing v1.5 command-line runner."""

    def __init__(self, device_id: str | None = None) -> None:
        self.device_id = device_id
        self.root = Tk()
        title = "MiniPilot v1.5 Console"
        if device_id:
            title += f" [{device_id}]"
        self.root.title(title)
        self.root.geometry("1200x820")

        self.process: subprocess.Popen[str] | None = None
        self.output_queue: queue.Queue[str] = queue.Queue()
        self.live_command_file: Path | None = None
        self.model_capture_active = False
        self.capture_catalog = _load_unicapture_catalog()
        self.capture_display_to_key: dict[str, str] = {}
        self.repeat_mode_enabled = False
        self.repeat_total = 1
        self.repeat_active = False
        self.repeat_completed = 0
        self.repeat_after_id: str | None = None
        self.repeat_settings: dict[str, object] | None = None

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain_output_queue)

    def run(self) -> None:
        self.root.mainloop()

    def _build_ui(self) -> None:
        top = Frame(self.root)
        top.pack(side=TOP, fill=X, padx=8, pady=8)

        Label(top, text="Goal").pack(side=LEFT)
        self.goal_entry = Entry(top)
        self.goal_entry.pack(side=LEFT, fill=X, expand=True, padx=8)
        self.goal_entry.insert(0, "打开抖音刷五分钟")

        Label(top, text="Duration").pack(side=LEFT)
        self.duration_entry = Entry(top, width=7)
        self.duration_entry.insert(0, "300")
        self.duration_entry.pack(side=LEFT, padx=(6, 8))

        self.repeat_button = Button(top, text="重复模式", command=self._configure_repeat_mode)
        self.repeat_button.pack(side=LEFT, padx=4)
        self.start_button = Button(top, text="Start", command=self._start_task)
        self.start_button.pack(side=LEFT, padx=4)
        self.stop_button = Button(top, text="Stop", command=self._send_stop, state="disabled")
        self.stop_button.pack(side=LEFT, padx=4)

        Label(top, text="Device").pack(side=LEFT, padx=(12, 0))
        self.device_values = _list_adb_devices()
        self.device_id, self.device_values = _initial_device_selection(
            self.device_id,
            self.device_values,
        )
        self.device_combo = Combobox(
            top,
            values=self.device_values,
            state="readonly" if self.device_values else "disabled",
            width=18,
        )
        if self.device_id:
            self.device_combo.set(self.device_id)
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_changed)
        self.device_combo.pack(side=LEFT, padx=4)

        capture_row = Frame(self.root)
        capture_row.pack(side=TOP, fill=X, padx=8, pady=(0, 8))

        Label(capture_row, text="Capture").pack(side=LEFT)
        self.capture_mode = Combobox(
            capture_row,
            values=["unicapture", "none"],
            state="readonly",
            width=12,
        )
        self.capture_mode.set("unicapture")
        self.capture_mode.pack(side=LEFT, padx=(6, 12))

        Label(capture_row, text="App mode").pack(side=LEFT)
        self.app_selection_mode = StringVar(value=AUTO_APP_SELECTION_MODE)
        Radiobutton(
            capture_row,
            text="自动推断",
            variable=self.app_selection_mode,
            value=AUTO_APP_SELECTION_MODE,
            command=self._on_app_selection_mode_changed,
        ).pack(side=LEFT, padx=(4, 0))
        Radiobutton(
            capture_row,
            text="手动选择",
            variable=self.app_selection_mode,
            value=MANUAL_APP_SELECTION_MODE,
            command=self._on_app_selection_mode_changed,
        ).pack(side=LEFT, padx=(0, 12))

        Label(capture_row, text="Category").pack(side=LEFT)
        category_values = _capture_category_values(self.capture_catalog)
        self.capture_category = Combobox(
            capture_row,
            values=category_values,
            state="disabled",
            width=14,
        )
        default_category = "短视频" if "短视频" in category_values else (category_values[0] if category_values else "")
        if default_category:
            self.capture_category.set(default_category)
        self.capture_category.pack(side=LEFT, padx=(6, 12))
        self.capture_category.bind("<<ComboboxSelected>>", self._on_capture_category_changed)

        Label(capture_row, text="App").pack(side=LEFT)
        app_values = self._capture_app_display_values(default_category)
        self.capture_app = Combobox(
            capture_row,
            values=app_values,
            state="disabled",
            width=24,
        )
        self.capture_app.pack(side=LEFT, padx=(6, 12))
        self.capture_app.bind("<<ComboboxSelected>>", self._on_capture_app_changed)

        Label(capture_row, text="Scene").pack(side=LEFT)
        self.capture_scene = Combobox(
            capture_row,
            values=[],
            state="readonly" if app_values else "disabled",
            width=20,
        )
        self.capture_scene.pack(side=LEFT, padx=(6, 12))
        self._refresh_capture_scene(self._selected_capture_app_key())

        self.add_capture_button = Button(
            capture_row,
            text="Add",
            command=self._open_capture_add_dialog,
        )
        self.add_capture_button.pack(side=LEFT, padx=(0, 4))
        self.refresh_capture_button = Button(
            capture_row,
            text="Refresh",
            command=self._refresh_capture_options,
        )
        self.refresh_capture_button.pack(side=LEFT, padx=(0, 4))

        vertical = PanedWindow(self.root, orient="vertical", sashrelief="raised")
        vertical.pack(fill=BOTH, expand=True, padx=8, pady=(0, 8))

        log_frame = Frame(vertical)
        Label(log_frame, text="Main Log").pack(anchor="w")
        self.main_log = Text(log_frame, wrap="word")
        self.main_log.pack(fill=BOTH, expand=True)
        vertical.add(log_frame, minsize=260)

        bottom = PanedWindow(vertical, orient="horizontal", sashrelief="raised")
        vertical.add(bottom, minsize=240)

        model_frame = Frame(bottom)
        Label(model_frame, text="Model Output / Reasoning").pack(anchor="w")
        self.model_log = Text(model_frame, wrap="word")
        self.model_log.pack(fill=BOTH, expand=True)
        bottom.add(model_frame, minsize=420)

        command_frame = Frame(bottom)
        Label(command_frame, text="Live Command").pack(anchor="w")
        self.command_text = Text(command_frame, wrap="word", height=8)
        self.command_text.pack(side=TOP, fill=BOTH, expand=True)

        buttons = Frame(command_frame)
        buttons.pack(side=BOTTOM, fill=X, pady=(6, 0))
        Button(buttons, text="Send", command=self._send_persistent).pack(side=LEFT, padx=3)
        Button(buttons, text="Once", command=self._send_once).pack(side=LEFT, padx=3)
        Button(buttons, text="Clear", command=self._send_clear).pack(side=LEFT, padx=3)
        Button(buttons, text="Stop", command=self._send_stop).pack(side=LEFT, padx=3)
        Button(buttons, text="Clear Input", command=self._clear_command_input).pack(side=RIGHT, padx=3)
        bottom.add(command_frame, minsize=360)

    def _start_task(self) -> None:
        if self.process and self.process.poll() is None:
            self._append_main("[GUI] Task is already running.\n")
            return

        settings = self._collect_run_settings()
        if settings is None:
            return

        if self.repeat_mode_enabled:
            self.repeat_active = True
            self.repeat_completed = 0
            self.repeat_settings = dict(settings)
            self._append_main(f"[GUI] Repeat mode enabled: total runs={self.repeat_total}, delay=10s.\n")
        else:
            self.repeat_active = False
            self.repeat_completed = 0
            self.repeat_settings = None

        self.main_log.delete("1.0", END)
        self.model_log.delete("1.0", END)
        self._launch_task(settings, clear_logs=False, repeat_index=1 if self.repeat_mode_enabled else None)

    def _collect_run_settings(self) -> dict[str, object] | None:
        goal = self.goal_entry.get().strip()
        if not goal:
            self._append_main("[GUI] Goal cannot be empty.\n")
            return None
        duration_text = self.duration_entry.get().strip()
        try:
            duration_value = int(duration_text)
        except ValueError:
            self._append_main("[GUI] Duration must be seconds, for example 300.\n")
            return None
        if duration_value <= 0:
            self._append_main("[GUI] Duration must be greater than 0.\n")
            return None

        return {
            "goal": goal,
            "duration": duration_value,
            "capture_mode": self.capture_mode.get().strip(),
            "capture_app": self._selected_capture_app_key(),
            "capture_scene": self.capture_scene.get().strip(),
        }

    def _launch_task(
        self,
        settings: dict[str, object],
        *,
        clear_logs: bool,
        repeat_index: int | None = None,
    ) -> None:
        goal = str(settings["goal"])
        duration_value = int(settings["duration"])
        capture_mode = str(settings.get("capture_mode") or "")
        capture_app = str(settings.get("capture_app") or "")
        capture_scene = str(settings.get("capture_scene") or "")

        live_dir = Path(tempfile.mkdtemp(prefix="minipilotv1.5-live-"))
        self.live_command_file = live_dir / "live_commands.queue.jsonl"
        self.live_command_file.touch(exist_ok=True)

        command = _build_executor_command(
            settings,
            live_command_file=self.live_command_file,
            device_id=self.device_id,
        )

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        if clear_logs:
            self.main_log.delete("1.0", END)
            self.model_log.delete("1.0", END)
        if repeat_index is not None:
            self._append_main(
                f"\n[GUI] Repeat run {repeat_index}/{self.repeat_total}\n"
            )
        self._append_main("[GUI] Starting MiniPilot v1.5...\n")
        self._append_main("[GUI] Live command file: " + str(self.live_command_file) + "\n")
        self._append_main(f"[GUI] Run duration: {duration_value}s\n")
        self._append_main(
            "[GUI] Capture: "
            + (capture_mode or "config")
            + (f" app={capture_app} scene={capture_scene}" if capture_mode != "none" else "")
            + "\n"
        )

        self.process = subprocess.Popen(
            command,
            cwd=str(Path(__file__).resolve().parents[1]),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        self.start_button.config(state="disabled")
        self.stop_button.config(state="normal")
        self.device_combo.config(state="disabled")
        threading.Thread(target=self._reader_thread, daemon=True).start()

    def _on_device_changed(self, _event: object | None = None) -> None:
        if self.process and self.process.poll() is None:
            return
        selected = self.device_combo.get().strip()
        self.device_id = selected or None

    def _configure_repeat_mode(self) -> None:
        if self.process and self.process.poll() is None:
            self._append_main("[GUI] Cannot change repeat mode while a task is running.\n")
            return

        count = simpledialog.askinteger(
            "重复模式",
            "请输入重复次数：",
            parent=self.root,
            initialvalue=self.repeat_total if self.repeat_mode_enabled else 15,
            minvalue=1,
            maxvalue=999,
        )
        if count is None:
            return

        self.repeat_total = int(count)
        self.repeat_mode_enabled = self.repeat_total > 1
        self.repeat_button.config(
            text=f"重复模式({self.repeat_total})" if self.repeat_mode_enabled else "重复模式"
        )
        if self.repeat_mode_enabled:
            self._append_main(f"[GUI] Repeat mode configured: total runs={self.repeat_total}.\n")
        else:
            self._append_main("[GUI] Repeat mode disabled because repeat count is 1.\n")

    def _on_capture_category_changed(self, _event: object | None = None) -> None:
        app_values = self._capture_app_display_values(self.capture_category.get().strip())
        self.capture_app.configure(
            values=app_values,
            state="readonly" if app_values else "disabled",
        )
        self.capture_app.set(app_values[0] if app_values else "")
        self._refresh_capture_scene(self._selected_capture_app_key())

    def _on_app_selection_mode_changed(self) -> None:
        if self.app_selection_mode.get() == AUTO_APP_SELECTION_MODE:
            self.capture_category.configure(state="disabled")
            self.capture_app.configure(state="disabled")
            self.capture_app.set("")
            self._refresh_capture_scene("")
            return

        category_values = _capture_category_values(self.capture_catalog)
        self.capture_category.configure(
            state="readonly" if category_values else "disabled"
        )
        category = self.capture_category.get().strip()
        if category not in category_values:
            category = "短视频" if "短视频" in category_values else (category_values[0] if category_values else "")
            self.capture_category.set(category)
        app_values = self._capture_app_display_values(category)
        self.capture_app.configure(
            values=app_values,
            state="readonly" if app_values else "disabled",
        )
        if self.capture_app.get().strip() not in app_values:
            self.capture_app.set(app_values[0] if app_values else "")
        self._refresh_capture_scene(self._selected_capture_app_key())

    def _on_capture_app_changed(self, _event: object | None = None) -> None:
        self._refresh_capture_scene(self._selected_capture_app_key())

    def _refresh_capture_scene(self, app_name: str) -> None:
        scenes = self.capture_catalog.get(app_name, {}).get("scenes", [])
        self.capture_scene.configure(
            values=scenes,
            state="readonly" if scenes else "disabled",
        )
        if scenes:
            current = self.capture_scene.get().strip()
            self.capture_scene.set(current if current in scenes else scenes[0])
        else:
            self.capture_scene.set("")

    def _capture_app_display_values(self, category: str) -> list[str]:
        self.capture_display_to_key = {}
        values: list[str] = []
        for app_key, meta in self.capture_catalog.items():
            if meta.get("category") != category:
                continue
            display = _display_for_app_key(app_key, self.capture_catalog)
            if display in self.capture_display_to_key:
                display = f"{display} ({app_key})"
            self.capture_display_to_key[display] = app_key
            values.append(display)
        return values

    def _selected_capture_app_key(self) -> str:
        if self.app_selection_mode.get() == AUTO_APP_SELECTION_MODE:
            return ""
        display = self.capture_app.get().strip()
        return self.capture_display_to_key.get(display, display)

    def _open_capture_add_dialog(self) -> None:
        dialog = Toplevel(self.root)
        dialog.title("Add Unicapture App")
        dialog.transient(self.root)
        dialog.resizable(False, False)

        Label(dialog, text="应用大类").grid(row=0, column=0, sticky="e", padx=8, pady=(10, 6))
        category_combo = Combobox(
            dialog,
            values=_capture_category_values(self.capture_catalog),
            state="readonly",
            width=33,
        )
        category_combo.set(self.capture_category.get().strip() or "AIGC")
        category_combo.grid(row=0, column=1, sticky="we", padx=8, pady=(10, 6))

        Label(dialog, text="应用名称").grid(row=1, column=0, sticky="e", padx=8, pady=6)
        display_entry = Entry(dialog, width=36)
        display_entry.grid(row=1, column=1, sticky="we", padx=8, pady=6)

        Label(dialog, text="应用包名").grid(row=2, column=0, sticky="e", padx=8, pady=6)
        package_entry = Entry(dialog, width=36)
        package_entry.grid(row=2, column=1, sticky="we", padx=8, pady=6)

        Label(dialog, text="场景名").grid(row=3, column=0, sticky="e", padx=8, pady=6)
        scene_combo = Combobox(
            dialog,
            values=["chat", "feed", "browse", "search", "video_play", "live", "short_video"],
            width=33,
        )
        scene_combo.set("chat")
        scene_combo.grid(row=3, column=1, sticky="we", padx=8, pady=6)

        buttons = Frame(dialog)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e", padx=8, pady=(8, 10))
        Button(
            buttons,
            text="OK",
            command=lambda: self._add_capture_app(
                category=category_combo.get(),
                display_name=display_entry.get(),
                package=package_entry.get(),
                scene=scene_combo.get(),
                dialog=dialog,
            ),
        ).pack(side=LEFT, padx=(0, 6))
        Button(buttons, text="Cancel", command=dialog.destroy).pack(side=LEFT)

        dialog.bind(
            "<Return>",
            lambda _event: self._add_capture_app(
                category=category_combo.get(),
                display_name=display_entry.get(),
                package=package_entry.get(),
                scene=scene_combo.get(),
                dialog=dialog,
            ),
        )
        display_entry.focus_set()

    def _refresh_capture_options(self, selected_app: str | None = None) -> None:
        preferred_key = selected_app or self._selected_capture_app_key()
        self.capture_catalog = _load_unicapture_catalog()
        category_values = _capture_category_values(self.capture_catalog)
        self.capture_category.configure(
            values=category_values,
            state=(
                "readonly"
                if category_values and self.app_selection_mode.get() == MANUAL_APP_SELECTION_MODE
                else "disabled"
            ),
        )

        preferred_category = self.capture_catalog.get(preferred_key, {}).get("category")
        if preferred_category in category_values:
            category = str(preferred_category)
        elif "短视频" in category_values:
            category = "短视频"
        else:
            category = category_values[0] if category_values else ""
        self.capture_category.set(category)

        app_values = self._capture_app_display_values(category)
        self.capture_app.configure(
            values=app_values,
            state=(
                "readonly"
                if app_values and self.app_selection_mode.get() == MANUAL_APP_SELECTION_MODE
                else "disabled"
            ),
        )

        preferred_display = _display_for_app_key(preferred_key, self.capture_catalog)
        if self.app_selection_mode.get() == AUTO_APP_SELECTION_MODE:
            display_name = ""
        elif preferred_display in app_values:
            display_name = preferred_display
        else:
            display_name = app_values[0] if app_values else ""

        self.capture_app.set(display_name)
        self._refresh_capture_scene(self._selected_capture_app_key())
        self._append_main(
            f"[GUI] Refreshed Unicapture apps from local file: {len(self.capture_catalog)} app(s).\n"
        )

    def _add_capture_app(
        self,
        *,
        category: str,
        display_name: str,
        package: str,
        scene: str,
        dialog: Toplevel,
    ) -> None:
        category = category.strip()
        display_name = display_name.strip()
        package = package.strip()
        scene = scene.strip() or "chat"
        parent = dialog if dialog.winfo_exists() else self.root

        if category not in _category_to_source_map():
            messagebox.showerror(
                "Invalid Category",
                "请选择有效的应用大类。",
                parent=parent,
            )
            return

        if not display_name:
            messagebox.showerror(
                "Invalid App Name",
                "应用名称不能为空。",
                parent=parent,
            )
            return

        if not _is_valid_package_name(package):
            messagebox.showerror(
                "Invalid Package",
                "应用包名应类似 com.example.app。",
                parent=parent,
            )
            return
        if not _is_valid_scene_name(scene):
            messagebox.showerror(
                "Invalid Scene",
                "场景名只能使用小写字母、数字、下划线。",
                parent=parent,
            )
            return
        existing_app = _find_unicapture_app_by_package(package)
        if existing_app:
            messagebox.showinfo(
                "Already Exists",
                f"应用包名 '{package}' 已存在于本地配置文件。点击 Refresh 后可在 App 下拉框中查看。",
                parent=parent,
            )
            dialog.destroy()
            return

        try:
            _append_unicapture_app(category, package, package, scene)
            _save_capture_app_display_name(package, display_name)
        except Exception as exc:
            messagebox.showerror(
                "Add Failed",
                f"Could not update unicapture app config:\n{exc}",
                parent=parent,
            )
            return

        dialog.destroy()
        self._append_main(
            f"[GUI] Added Unicapture app to local file: {display_name} category={category} package={package} scene={scene}. Click Refresh to reload the App list.\n"
        )

    def _reader_thread(self) -> None:
        assert self.process is not None
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.output_queue.put(line)
        code = self.process.wait()
        self.output_queue.put(f"__MINIPILOT_PROCESS_EXIT__:{code}\n")

    def _drain_output_queue(self) -> None:
        while True:
            try:
                line = self.output_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_output_line(line)
        self.root.after(100, self._drain_output_queue)

    def _handle_output_line(self, line: str) -> None:
        if line.startswith("__MINIPILOT_PROCESS_EXIT__:"):
            code_text = line.split(":", 1)[1].strip()
            try:
                code = int(code_text)
            except ValueError:
                code = -1
            self._handle_process_exit(code)
            return

        if "Model output:" in line:
            self.model_capture_active = True
            self._append_model(line)
            action_line = _extract_model_action_line(line)
            if action_line:
                self._append_main(f"[Model action] {action_line}\n")
            return
        if self.model_capture_active:
            if line.startswith("[Loop ") and "Model output:" not in line:
                self.model_capture_active = False
            else:
                self._append_model(line)
                action_line = _extract_model_action_line(line)
                if action_line:
                    self._append_main(f"[Model action] {action_line}\n")
                return

        self._append_main(line)

    def _handle_process_exit(self, code: int) -> None:
        self._append_main(f"[GUI] Process exited with code {code}\n")
        self.stop_button.config(state="disabled")

        if self.repeat_active:
            if code == 0:
                self.repeat_completed += 1
                if self.repeat_completed < self.repeat_total:
                    next_index = self.repeat_completed + 1
                    self._append_main(
                        f"[GUI] Repeat run {self.repeat_completed}/{self.repeat_total} finished normally. "
                        f"Restarting run {next_index}/{self.repeat_total} in 10 seconds...\n"
                    )
                    self.repeat_after_id = self.root.after(
                        10000,
                        lambda: self._start_next_repeat_run(next_index),
                    )
                    self.stop_button.config(state="normal")
                    return
                self._append_main(
                    f"[GUI] Repeat mode completed: {self.repeat_completed}/{self.repeat_total} run(s).\n"
                )
            else:
                self._append_main(
                    f"[GUI] Repeat mode stopped because run exited with code {code}.\n"
                )

        self.repeat_active = False
        self.repeat_after_id = None
        self.start_button.config(state="normal")
        self.device_combo.config(
            state="readonly" if self.device_values else "disabled"
        )

    def _start_next_repeat_run(self, repeat_index: int) -> None:
        self.repeat_after_id = None
        if not self.repeat_active or self.repeat_settings is None:
            self.start_button.config(state="normal")
            self.device_combo.config(
                state="readonly" if self.device_values else "disabled"
            )
            return
        if self.process and self.process.poll() is None:
            self._append_main("[GUI] Repeat start skipped because a task is already running.\n")
            self.start_button.config(state="normal")
            self.device_combo.config(
                state="readonly" if self.device_values else "disabled"
            )
            return
        self._launch_task(
            dict(self.repeat_settings),
            clear_logs=False,
            repeat_index=repeat_index,
        )

    def _send_persistent(self) -> None:
        text = self._command_text()
        if text:
            self._append_command(_normalize_gui_command(text))
            self._append_main(f"[GUI] Sent live command: {text}\n")
            self._clear_command_input()

    def _send_once(self) -> None:
        text = self._command_text()
        if text:
            self._append_command("/once " + text)
            self._append_main(f"[GUI] Sent one-shot command: {text}\n")
            self._clear_command_input()

    def _send_clear(self) -> None:
        self._append_command("/clear")
        self._append_main("[GUI] Sent clear command.\n")

    def _send_stop(self) -> None:
        if self.repeat_after_id is not None:
            try:
                self.root.after_cancel(self.repeat_after_id)
            except Exception:
                pass
            self.repeat_after_id = None
            self.repeat_active = False
            self.start_button.config(state="normal")
            self.stop_button.config(state="disabled")
            self.device_combo.config(
                state="readonly" if self.device_values else "disabled"
            )
            self._append_main("[GUI] Repeat mode cancelled before next run.\n")
            return
        self._append_command("/stop")
        self._append_main("[GUI] Sent stop command.\n")

    def _append_command(self, text: str) -> None:
        if self.live_command_file is None:
            self._append_main("[GUI] No live command file yet. Start a task first.\n")
            return
        payload = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "text": text,
        }
        with self.live_command_file.open("a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _command_text(self) -> str:
        return self.command_text.get("1.0", END).strip()

    def _clear_command_input(self) -> None:
        self.command_text.delete("1.0", END)

    def _append_main(self, text: str) -> None:
        self.main_log.insert(END, text)
        self.main_log.see(END)

    def _append_model(self, text: str) -> None:
        self.model_log.insert(END, text)
        self.model_log.see(END)

    def _on_close(self) -> None:
        if self.repeat_after_id is not None:
            try:
                self.root.after_cancel(self.repeat_after_id)
            except Exception:
                pass
            self.repeat_after_id = None
            self.repeat_active = False
        if self.process and self.process.poll() is None:
            try:
                self._append_command("/stop GUI closed")
            except Exception:
                pass
        self.root.destroy()


def _normalize_gui_command(text: str) -> str:
    lowered = text.lower()
    if (
        lowered.startswith("/send ")
        or lowered.startswith("/also ")
        or lowered.startswith("/keep ")
        or lowered.startswith("/once ")
        or lowered == "/clear"
        or lowered.startswith("/stop")
    ):
        return text
    return "/send " + text


def _extract_model_action_line(line: str) -> str | None:
    text = line.strip()
    if "Model output:" in text:
        text = text.split("Model output:", 1)[1].strip()

    starts = [index for index in (text.find("do("), text.find("finish(")) if index >= 0]
    if not starts:
        return None
    return text[min(starts):].strip()


def _build_executor_command(
    settings: dict[str, object],
    *,
    live_command_file: Path,
    device_id: str | None,
) -> list[str]:
    """Build the GUI-to-Executor command without changing capture behavior."""
    command = [
        sys.executable,
        "-u",
        "-m",
        "mini_pilot.main",
        "--goal",
        str(settings["goal"]),
        "--live-command-file",
        str(live_command_file),
        "--no-live-console",
        "--run-duration",
        str(int(settings["duration"])),
    ]
    if device_id:
        command.extend(["--device-id", device_id])

    capture_mode = str(settings.get("capture_mode") or "")
    if capture_mode == "none":
        command.extend(["--capture", "none"])
        return command

    command.extend(["--capture", "unicapture"])
    capture_app = str(settings.get("capture_app") or "")
    capture_scene = str(settings.get("capture_scene") or "")
    if capture_app:
        command.extend(["--capture-app", capture_app])
        if capture_scene:
            command.extend(["--capture-scene", capture_scene])
    return command


CAPTURE_CATEGORY_SOURCES: list[tuple[str, str]] = [
    ("短视频", "SHORT_VIDEO_APPS"),
    ("开/看直播", "LIVE_APPS"),
    ("视频点播", "VOD_APPS"),
    ("会议", "MEETING_APPS"),
    ("（云）游戏", "GAME_APPS"),
    ("云游戏", "CLOUDGAME_APPS"),
    ("音视频通话", "CALL_APPS"),
    ("云手机/云电脑", "CLOUDPHONE_APPS"),
    ("资讯浏览", "NEWS_APPS"),
    ("AIGC", "AIGC_APPS"),
    ("文件传输/云存储", "FILETRANSFER_APPS"),
    ("即时通信", "IM_APPS"),
    ("购物", "SHOPPING_APPS"),
    ("音频", "AUDIO_APPS"),
]


def _load_unicapture_catalog() -> dict[str, dict[str, object]]:
    """Load app choices, display names, categories, packages, and scenes."""
    configs_path = _unicapture_configs_path()
    if not configs_path.exists():
        return {}

    spec = importlib.util.spec_from_file_location(
        "_minipilot_gui_unicapture_app_configs",
        configs_path,
    )
    if spec is None or spec.loader is None:
        return {}

    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return {}

    app_to_category: dict[str, str] = {}
    for category, variable_name in CAPTURE_CATEGORY_SOURCES:
        group = getattr(module, variable_name, {})
        if isinstance(group, dict):
            for app_name in group:
                app_to_category[app_name] = category

    display_names = dict(getattr(module, "CAPTURE_APP_DISPLAY_NAMES", {}))
    display_names.update(_load_capture_app_display_names())
    catalog: dict[str, dict[str, object]] = {}
    for app_name in module.list_all_apps():
        app_config = module.get_app_config(app_name)
        if not app_config:
            continue
        scenes = [scene.name for scene in app_config.scenes]
        catalog[app_name] = {
            "display": display_names.get(app_name, _humanize_app_key(app_name)),
            "category": app_to_category.get(app_name, _category_from_app_type(app_config.app_type)),
            "package": getattr(app_config, "package", ""),
            "scenes": scenes,
        }
    return catalog


def _capture_category_values(catalog: dict[str, dict[str, object]]) -> list[str]:
    present = {str(meta.get("category") or "") for meta in catalog.values()}
    ordered = [name for name, _source in CAPTURE_CATEGORY_SOURCES if name in present]
    extras = sorted(item for item in present if item and item not in ordered)
    return ordered + extras


def _display_for_app_key(app_key: str, catalog: dict[str, dict[str, object]]) -> str:
    meta = catalog.get(app_key)
    if not meta:
        return app_key
    return str(meta.get("display") or app_key)


def _category_from_app_type(app_type: str) -> str:
    return {
        "video": "视频点播",
        "game": "（云）游戏",
        "cloudgame": "云游戏",
        "social": "即时通信",
        "shopping": "购物",
        "cloudphone": "云手机/云电脑",
        "audio": "音频",
        "aigc": "AIGC",
        "news": "资讯浏览",
        "filetransfer": "文件传输/云存储",
    }.get(app_type, app_type or "其他")


def _humanize_app_key(app_key: str) -> str:
    if "." in app_key:
        return app_key
    return " ".join(part.capitalize() for part in app_key.split("_") if part)


def _capture_app_names_path() -> Path:
    return Path(__file__).resolve().parent / "capture_app_names.json"


def _load_capture_app_display_names() -> dict[str, str]:
    path = _capture_app_names_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in data.items()
        if isinstance(key, str) and isinstance(value, str) and value.strip()
    }


def _save_capture_app_display_name(app_key: str, display_name: str) -> None:
    names = _load_capture_app_display_names()
    names[app_key] = display_name
    _capture_app_names_path().write_text(
        json.dumps(names, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _append_unicapture_app(
    category: str,
    app_key: str,
    package: str,
    scene: str,
    duration: int = 300,
) -> None:
    """Persist a simple custom app into the selected unicapture category."""
    configs_path = _unicapture_configs_path()
    text = configs_path.read_text(encoding="utf-8")
    if f'"{app_key}"' in text:
        raise ValueError(f"app already exists: {app_key}")

    source_name = _category_to_source_map().get(category)
    if not source_name:
        raise ValueError(f"unknown app category: {category}")

    start = text.find(f"{source_name} = {{")
    if start < 0:
        raise ValueError(f"{source_name} block was not found")
    end = _find_dict_block_end(text, start)
    if end < 0:
        raise ValueError(f"{source_name} closing marker was not found")

    app_type = _app_type_for_category(category)

    block = (
        f'\n    "{app_key}": AppConfig(\n'
        f'        name="{app_key}",\n'
        f'        app_type="{app_type}",\n'
        f'        package="{package}",\n'
        f'        scenes=[\n'
        f'            SceneConfig(name="{scene}", description="Custom scene", duration={duration}),\n'
        f'        ],\n'
        f'        qoe_metrics=[]\n'
        f'    ),\n'
    )
    configs_path.write_text(text[:end] + block + text[end:], encoding="utf-8")


def _category_to_source_map() -> dict[str, str]:
    return {category: source for category, source in CAPTURE_CATEGORY_SOURCES}


def _app_type_for_category(category: str) -> str:
    return {
        "短视频": "video",
        "开/看直播": "video",
        "视频点播": "video",
        "会议": "meeting",
        "（云）游戏": "game",
        "云游戏": "cloudgame",
        "音视频通话": "social",
        "云手机/云电脑": "cloudphone",
        "资讯浏览": "news",
        "AIGC": "aigc",
        "文件传输/云存储": "filetransfer",
        "即时通信": "social",
        "购物": "shopping",
        "音频": "audio",
    }.get(category, "aigc")


def _find_dict_block_end(text: str, start: int) -> int:
    """Find the closing brace line for a top-level dict assignment."""
    brace_start = text.find("{", start)
    if brace_start < 0:
        return -1
    depth = 0
    index = brace_start
    while index < len(text):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                line_start = text.rfind("\n", 0, index)
                return line_start if line_start >= 0 else index
        index += 1
    return -1


def _unicapture_configs_path() -> Path:
    return Path(__file__).resolve().parents[1] / "unicapture" / "app_configs.py"


def _find_unicapture_app_by_package(package: str) -> str | None:
    configs_path = _unicapture_configs_path()
    if not configs_path.exists():
        return None
    spec = importlib.util.spec_from_file_location(
        "_minipilot_gui_unicapture_lookup",
        configs_path,
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    for app_name in module.list_all_apps():
        app_config = module.get_app_config(app_name)
        if app_config and getattr(app_config, "package", None) == package:
            return app_name
    return None


def _is_valid_scene_name(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9_]+", value))


def _is_valid_package_name(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+", value))


def _list_adb_devices() -> list[str]:
    """Return serials for devices currently ready in ADB."""
    try:
        result = subprocess.run(
            ["adb", "devices"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except Exception:
        return []
    devices: list[str] = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


def _initial_device_selection(
    requested: str | None,
    ready_devices: list[str],
) -> tuple[str | None, list[str]]:
    """Never replace an explicitly requested device with another serial."""
    devices = list(dict.fromkeys(ready_devices))
    if requested:
        if requested not in devices:
            devices.insert(0, requested)
        return requested, devices
    if len(devices) == 1:
        return devices[0], devices
    return None, devices


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mini-pilot-gui")
    parser.add_argument("--device-id", default=None)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    MiniPilotGui(device_id=args.device_id).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
