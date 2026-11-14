"""MiniPilot agent loop.

The agent layer coordinates screenshot capture, model requests, action parsing,
and action execution. It is the outer control loop of the project.
"""

from __future__ import annotations

import re
import math
import json
import time
from dataclasses import dataclass
from typing import Callable

from PIL import Image, ImageStat

from mini_pilot.actions import Action, ActionError, ActionResult
from mini_pilot.actions import execute_action, parse_action
from mini_pilot.apps import APP_PACKAGES
from mini_pilot.model import ModelClient, ModelError, ModelHistoryItem
from mini_pilot.prompts import PLANNING_SYSTEM_PROMPT, SYSTEM_PROMPT
from mini_pilot.device import DeviceError, get_current_app, wake_screen
from mini_pilot.screenshot import (
    Screenshot,
    ScreenshotError,
    capture_screenshot,
    compare_screenshots,
)


@dataclass(frozen=True)
class AgentConfig:
    """Runtime configuration for MiniPilot."""

    max_loops: int = 30
    device_id: str | None = None
    screenshot_dir: str | None = None
    verbose: bool = True
    history_steps: int = 3
    image_max_side: int = 2048
    duration_budget_seconds: int | None = None
    duration_started_at: float | None = None
    duration_started_at_provider: Callable[[], float | None] | None = None
    target_app_package: str | None = None
    loop_status_provider: Callable[[], str | None] | None = None
    operation_style_text: str | None = None
    live_command_provider: Callable[[], dict[str, object] | None] | None = None
    max_wait_seconds: float = 1.5


@dataclass(frozen=True)
class LoopRecord:
    """Debug information for one agent loop."""

    loop: int
    screenshot: Screenshot
    model_output: str
    action: Action
    result: ActionResult


@dataclass
class InstructionState:
    """Runtime-owned progress for an explicitly ordered user instruction."""

    epoch: int = 0
    source_instruction: str = ""
    steps: list[str] | None = None
    completed: set[int] | None = None
    current_step: int = 0
    repeat_mode: str = "once"
    cycle_count: int = 0
    cycle_start: int = 0
    termination: str = "semantic_completion"
    pending_completion: tuple[int, int] | None = None
    last_verification: str | None = None

    def reset(self, instruction: str) -> None:
        self.epoch += 1
        self.source_instruction = instruction.strip()
        self.steps = None
        self.completed = set()
        self.current_step = 0
        self.repeat_mode = "once"
        self.cycle_count = 0
        self.cycle_start = 0
        self.termination = "semantic_completion"
        self.pending_completion = None
        self.last_verification = "A new instruction epoch needs a short ordered plan."

    def set_plan(
        self,
        steps: list[str],
        *,
        repeat_mode: str = "once",
        cycle_start: int | None = None,
        termination: str = "semantic_completion",
    ) -> None:
        cleaned = [_compact_text(step, limit=260) for step in steps if step.strip()][:8]
        if not cleaned:
            return
        self.steps = cleaned[:6]
        self.completed = set()
        self.current_step = 0
        self.repeat_mode = "cycle" if repeat_mode == "cycle" else "once"
        self.cycle_count = 0
        if self.repeat_mode == "cycle" and isinstance(cycle_start, int):
            self.cycle_start = max(0, min(cycle_start, len(self.steps) - 1))
        else:
            self.cycle_start = 0
        self.termination = termination
        self.pending_completion = None
        self.last_verification = "Plan accepted; step 1 is active."

    def observe(self, records: list[LoopRecord], screenshot: Screenshot) -> None:
        if self.pending_completion is None or not records:
            return
        step_index, action_loop = self.pending_completion
        last_record = records[-1]
        if last_record.loop != action_loop:
            return
        change = compare_screenshots(last_record.screenshot, screenshot)
        changed = change.mean_difference >= 0.08 or change.changed_ratio >= 0.12
        self.pending_completion = None
        if not changed:
            self.last_verification = (
                f"Step {step_index + 1} completion was not verified: the next screen did not change enough."
            )
            return
        if self.completed is None:
            self.completed = set()
        self.completed.add(step_index)
        if self.steps and step_index >= len(self.steps) - 1 and self.repeat_mode == "cycle":
            self.cycle_count += 1
            self.completed = set(range(self.cycle_start))
            self.current_step = self.cycle_start
            self.last_verification = (
                f"Cycle {self.cycle_count} was visually verified after loop {action_loop}; "
                "the next cycle starts from step 1 on the newly reached content."
            )
            return
        if self.steps:
            self.current_step = min(step_index + 1, len(self.steps))
        self.last_verification = (
            f"Step {step_index + 1} was visually verified as completed after loop {action_loop}."
        )

    def stage_completion(self, *, loop: int, action: Action, proposal: dict[str, object]) -> None:
        if not self.steps or not action.name or not proposal.get("complete_current_step"):
            return
        if self.current_step >= len(self.steps):
            return
        self.pending_completion = (self.current_step, loop)
        self.last_verification = (
            f"Waiting to verify the model's proposed completion of step {self.current_step + 1}."
        )


@dataclass(frozen=True)
class AgentRunResult:
    """Final result of an agent run."""

    success: bool
    message: str
    loops: list[LoopRecord]


class AgentError(RuntimeError):
    """Raised when the agent loop cannot continue."""


class MiniPilotAgent:
    """Coordinate MiniPilot's screenshot-model-action loop."""

    def __init__(
        self,
        model_client: ModelClient,
        config: AgentConfig | None = None,
        system_prompt: str = SYSTEM_PROMPT,
    ):
        self.model_client = model_client
        self.config = config or AgentConfig()
        self.system_prompt = system_prompt

    def run(self, task: str) -> AgentRunResult:
        """Run a task until the model finishes or max_loops is reached."""
        if not task.strip():
            raise AgentError("Task cannot be empty.")

        records: list[LoopRecord] = []
        conversation: list[dict[str, object]] = [
            {"role": "system", "content": self.system_prompt}
        ]
        previous_action_result: str | None = None
        pending_finish_confirmation: ActionResult | None = None
        consecutive_model_errors = 0
        consecutive_parse_errors = 0
        started_at = time.monotonic()
        duration_started_at: float | None = self.config.duration_started_at
        external_duration_anchor_logged = False
        duration_budget_seconds = (
            self.config.duration_budget_seconds
            or _extract_duration_budget_seconds(task)
        )
        last_live_command_signature: tuple[str, ...] = ()
        instruction_state = InstructionState()
        instruction_state.reset(task)

        for loop in range(1, self.config.max_loops + 1):
            live_commands = self._read_live_commands()
            if live_commands.get("stop"):
                message = str(live_commands.get("stop_message") or "Stopped by live user command.")
                self._log(f"[Loop {loop}] Result: {message}")
                return AgentRunResult(success=False, message=message, loops=records)
            live_command_signature = tuple(
                _live_command_items(live_commands)
                + _string_list(live_commands.get("once"))
            )
            live_command_changed = (
                bool(live_command_signature)
                and live_command_signature != last_live_command_signature
            )
            last_live_command_signature = live_command_signature
            active_instruction = _active_instruction_text_for_progress(task, live_commands)
            if live_command_changed:
                instruction_state.reset(active_instruction)
            self._log_live_command_status(loop, live_commands)

            self._log(f"\n[Loop {loop}/{self.config.max_loops}] Capturing screenshot...")

            try:
                screenshot = capture_screenshot(
                    device_id=self.config.device_id,
                    output_dir=self.config.screenshot_dir,
                    max_model_side=self.config.image_max_side,
                )
            except ScreenshotError as exc:
                raise AgentError(f"Screenshot failed: {exc}") from exc

            self._log(
                f"[Loop {loop}] Screenshot: {screenshot.path} "
                f"({screenshot.width}x{screenshot.height})"
            )
            self._log_loop_status()
            if screenshot.is_mostly_black:
                self._log(
                    f"[Loop {loop}] Screenshot is mostly black; trying to wake the device..."
                )
                try:
                    wake_screen(device_id=self.config.device_id)
                    screenshot = capture_screenshot(
                        device_id=self.config.device_id,
                        output_dir=self.config.screenshot_dir,
                        max_model_side=self.config.image_max_side,
                        black_screen_retries=1,
                    )
                except (DeviceError, ScreenshotError) as exc:
                    message = (
                        "Screenshot remained mostly black and wake recovery failed. "
                        f"The phone may be locked, disconnected, or protected. Details: {exc}"
                    )
                    self._log(f"[Loop {loop}] Result: {message}")
                    return AgentRunResult(
                        success=False,
                        message=message,
                        loops=records,
                    )

                if screenshot.is_mostly_black:
                    message = (
                        "Screenshot remained mostly black after wake recovery. "
                        "The phone may still be locked, screen-off, or on a protected page."
                    )
                    self._log(f"[Loop {loop}] Result: {message}")
                    return AgentRunResult(
                        success=False,
                        message=message,
                        loops=records,
                    )

                self._log(
                    f"[Loop {loop}] Wake recovery screenshot: {screenshot.path} "
                    f"({screenshot.width}x{screenshot.height})"
                )
                self._log_loop_status()

            instruction_state.observe(records, screenshot)

            if duration_budget_seconds is not None:
                duration_wait_reason = None
                if (
                    duration_started_at is None
                    and self.config.duration_started_at_provider is not None
                ):
                    duration_started_at = self.config.duration_started_at_provider()
                if duration_started_at is None:
                    if self.config.duration_started_at_provider is not None:
                        duration_wait_reason = (
                            "Runtime VLM/activate monitor has not confirmed business "
                            "start yet."
                        )
                    else:
                        duration_wait_reason = _duration_timer_wait_reason(
                            task=task,
                            records=records,
                            screenshot=screenshot,
                            device_id=self.config.device_id,
                        )
                if duration_wait_reason:
                    previous_action_result = (
                        "Duration timer has not started yet. "
                        f"{duration_wait_reason} "
                        "Continue the current active user goal. Original goal for context: "
                        f"{_compact_text(task.strip(), limit=700)}"
                    )
                else:
                    if duration_started_at is None:
                        duration_started_at = time.monotonic()
                        self._log(
                            "[Loop {loop}] Duration timer started after valid "
                            "browsing screen was detected.".format(loop=loop)
                        )
                    elif (
                        duration_started_at == self.config.duration_started_at
                        and not external_duration_anchor_logged
                    ):
                        external_duration_anchor_logged = True
                        self._log(
                            "[Loop {loop}] Duration timer is anchored to capture "
                            "start time.".format(loop=loop)
                        )
                    duration_message = self._maybe_finish_duration_budget(
                        duration_started_at,
                        records,
                        duration_budget_seconds,
                    )
                    if duration_message:
                        self._log(f"[Loop {loop}] Result: {duration_message}")
                        return AgentRunResult(
                            success=True,
                            message=duration_message,
                            loops=records,
                        )
            else:
                duration_message = self._maybe_finish_duration_budget(
                    started_at,
                    records,
                    duration_budget_seconds,
                )
                if duration_message:
                    self._log(f"[Loop {loop}] Result: {duration_message}")
                    return AgentRunResult(
                        success=True,
                        message=duration_message,
                        loops=records,
                    )

            if instruction_state.steps is None:
                plan = self._request_instruction_plan(
                    task=task,
                    active_instruction=active_instruction,
                    screenshot=screenshot,
                    duration_budget_seconds=duration_budget_seconds,
                    live_commands=live_commands,
                )
                instruction_state.set_plan(
                    plan["steps"],
                    repeat_mode=str(plan["mode"]),
                    cycle_start=plan.get("cycle_start"),
                    termination=str(plan["termination"]),
                )
                self._log(
                    f"[Loop {loop}] Runtime accepted model plan: "
                    f"mode={instruction_state.repeat_mode}, "
                    f"steps={len(instruction_state.steps)}, "
                    f"cycle_start={instruction_state.cycle_start + 1 if instruction_state.repeat_mode == 'cycle' else 'none'}."
                )

            early_finish_message = self._maybe_finish_completed_task(
                task=task,
                records=records,
                current_screenshot=screenshot,
                duration_budget_seconds=duration_budget_seconds,
            )
            if early_finish_message:
                self._log(f"[Loop {loop}] Result: {early_finish_message}")
                return AgentRunResult(
                    success=True,
                    message=early_finish_message,
                    loops=records,
                )

            previous_action_result = self._build_previous_action_feedback(
                task=task,
                records=records,
                current_screenshot=screenshot,
                previous_action_result=previous_action_result,
            )
            previous_action_result = self._append_duration_feedback(
                previous_action_result,
                duration_started_at,
                duration_budget_seconds,
            )
            if live_command_changed:
                live_notice = (
                    "A new live user command was just accepted and is now the "
                    "current active task direction: "
                    f"{'; '.join(live_command_signature)}. "
                    "Use it as the newest user correction for the ongoing task."
                )
                previous_action_result = (
                    f"{previous_action_result} {live_notice}"
                    if previous_action_result
                    else live_notice
                )

            conversation.append(
                _build_conversation_user_message(
                    task=task,
                    active_goal=_active_user_goal_text(
                        task=task,
                        commands=live_commands,
                        records=records,
                        duration_budget_seconds=duration_budget_seconds,
                    ),
                    runtime_context=_mission_runtime_context_text(
                        task=task,
                        runtime_contract=_runtime_task_contract(task),
                        records=records,
                        current_screenshot=screenshot,
                        duration_started_at=duration_started_at,
                        duration_budget_seconds=duration_budget_seconds,
                        device_id=self.config.device_id,
                        target_app_package=self.config.target_app_package,
                    ),
                    mission_audit=_mission_audit_text(
                        task=task,
                        records=records,
                        current_screenshot=screenshot,
                        duration_started_at=duration_started_at,
                        duration_budget_seconds=duration_budget_seconds,
                    ),
                    chat_memory=_chat_runtime_memory_text(
                        task=task,
                        records=records,
                        commands=live_commands,
                    ),
                    live_override=_live_command_override_text(live_commands),
                    operation_style=self.config.operation_style_text,
                    live_corrections=_live_corrections_text(live_commands),
                    instruction_progress=_instruction_state_packet(
                        state=instruction_state,
                        task=task,
                        commands=live_commands,
                        records=records,
                        duration_budget_seconds=duration_budget_seconds,
                    ),
                    recent_facts=_recent_action_facts(records),
                    screenshot=screenshot,
                    previous_action=previous_action_result,
                    is_first=len(records) == 0,
                    device_id=self.config.device_id,
                    target_app_package=self.config.target_app_package,
                )
            )
            conversation = _trim_conversation(
                conversation,
                keep_recent=6,
            )
            request_stats = _message_payload_stats(conversation)
            self._log(
                f"[Loop {loop}] Model request: messages={request_stats['messages']}, "
                f"text_chars={request_stats['text_chars']}, images={request_stats['images']}"
            )

            try:
                model_response = self.model_client.ask_messages(conversation)
            except ModelError as exc:
                if conversation:
                    conversation.pop()
                consecutive_model_errors += 1
                if consecutive_model_errors <= 8 and _is_transient_model_error(exc):
                    fallback_action = _fallback_action_during_model_outage(
                        task=task,
                        records=records,
                    )
                    if fallback_action is not None and consecutive_model_errors >= 2:
                        try:
                            fallback_result = execute_action(
                                action=fallback_action,
                                screen_width=screenshot.width,
                                screen_height=screenshot.height,
                                device_id=self.config.device_id,
                            )
                        except ActionError as fallback_exc:
                            fallback_result = ActionResult(
                                success=False,
                                message=(
                                    f"Model provider returned a transient error and "
                                    f"runtime fallback failed: {fallback_exc}."
                                ),
                            )

                        records.append(
                            LoopRecord(
                                loop=loop,
                                screenshot=screenshot,
                                model_output=(
                                    "[runtime fallback after transient model error] "
                                    f"{exc}"
                                ),
                                action=fallback_action,
                                result=fallback_result,
                            )
                        )
                        previous_action_result = (
                            f"{fallback_result.message} Model provider returned a "
                            f"transient empty response ({consecutive_model_errors}/8); "
                            "runtime used a conservative fallback action to keep the "
                            "duration task moving."
                        )
                        self._log(
                            f"[Loop {loop}] Model transient error: {exc}. "
                            f"Used fallback action {fallback_action.name} "
                            f"({consecutive_model_errors}/8)."
                        )
                        continue

                    previous_action_result = (
                        "The model provider returned an empty or transient error response. "
                        f"Runtime will retry from a fresh screenshot ({consecutive_model_errors}/8). "
                        "Continue the current active user goal and recover to the "
                        "main task route if the current screen is a temporary side route. "
                        f"Original goal: {_compact_text(task.strip(), limit=700)}"
                    )
                    self._log(
                        f"[Loop {loop}] Model transient error: {exc}. "
                        f"Retrying next loop ({consecutive_model_errors}/8)."
                    )
                    time.sleep(1.0)
                    continue
                raise AgentError(f"Model request failed: {exc}") from exc

            consecutive_model_errors = 0

            self._log(f"[Loop {loop}] Model output: {model_response.content}")
            conversation[-1] = _remove_images_from_message(conversation[-1])
            progress_proposal = _extract_task_progress_proposal(model_response.content)

            try:
                action = parse_action(model_response.content)
            except ActionError as exc:
                action = _fallback_action_after_parse_failure(
                    task=task,
                    records=records,
                    model_output=model_response.content,
                )
                if action is None:
                    consecutive_parse_errors += 1
                    if consecutive_parse_errors >= 2:
                        conversation = _trim_conversation(
                            conversation,
                            keep_recent=8,
                        )
                        previous_action_result = (
                            "Repeated invalid model outputs were trimmed from recent history. "
                            "Use the current screen and active goal to plan briefly, then end "
                            "with one supported executable action for the runtime."
                        )
                    action = Action(
                        name="Invalid",
                        params={"raw_preview": _compact_text(model_response.content, limit=500)},
                    )
                    result = ActionResult(
                        success=False,
                        message=(
                            "Action parse failed: model output did not end with a supported "
                            "do(...) or finish(...) action. Reason briefly if helpful, then "
                            "end with one executable action."
                        ),
                    )
                else:
                    consecutive_parse_errors = 0
                    requested_action = action
                    action = _cap_wait_action(action, self.config.max_wait_seconds)
                    action = _normalize_launch_target_package(
                        action,
                        target_app_package=self.config.target_app_package,
                    )
                    if _action_history_text(action) != _action_history_text(requested_action):
                        self._log(f"[Loop {loop}] Runtime action: {_action_history_text(action)}")
                    try:
                        result = execute_action(
                            action=action,
                            screen_width=screenshot.width,
                            screen_height=screenshot.height,
                            device_id=self.config.device_id,
                        )
                    except ActionError as fallback_exc:
                        result = ActionResult(
                            success=False,
                            message=(
                                f"Fallback action execution failed: {fallback_exc}. "
                                "Plan from the current screen, then end with a supported action call."
                            ),
                        )
            else:
                consecutive_parse_errors = 0
                requested_action = action
                action = _cap_wait_action(action, self.config.max_wait_seconds)
                action = _normalize_launch_target_package(
                    action,
                    target_app_package=self.config.target_app_package,
                )
                if _action_history_text(action) != _action_history_text(requested_action):
                    self._log(f"[Loop {loop}] Runtime action: {_action_history_text(action)}")
                try:
                    result = execute_action(
                        action=action,
                        screen_width=screenshot.width,
                        screen_height=screenshot.height,
                        device_id=self.config.device_id,
                    )
                except ActionError as exc:
                    result = ActionResult(
                        success=False,
                        message=f"Action execution failed: {exc}. Try a different valid action.",
                    )

            records.append(
                LoopRecord(
                    loop=loop,
                    screenshot=screenshot,
                    model_output=model_response.content,
                    action=action,
                    result=result,
                )
            )
            if result.success and action.name != "Invalid":
                instruction_state.stage_completion(
                    loop=loop,
                    action=action,
                    proposal=progress_proposal,
                )

            self._log(f"[Loop {loop}] Result: {result.message}")
            previous_action_result = result.message

            if result.finished:
                open_ended_rejection = _open_ended_finish_rejection(task, live_commands)
                if open_ended_rejection:
                    pending_finish_confirmation = None
                    previous_action_result = open_ended_rejection
                    records[-1] = LoopRecord(
                        loop=records[-1].loop,
                        screenshot=records[-1].screenshot,
                        model_output=records[-1].model_output,
                        action=records[-1].action,
                        result=ActionResult(
                            success=False,
                            finished=False,
                            message=open_ended_rejection,
                        ),
                    )
                    self._log(f"[Loop {loop}] Result: {open_ended_rejection}")
                    continue

                if not _should_use_strict_completion_validation(task):
                    if action.name.lower() == "finish":
                        if pending_finish_confirmation is None:
                            pending_finish_confirmation = result
                            previous_action_result = (
                                "The model said the task is finished. Do not stop yet. "
                                "Confirm completion from the next screenshot using the same "
                                "original task context. If the whole user task is truly complete, "
                                "output finish(message=\"...\") again. Otherwise continue with "
                                "the next required do(...) action."
                            )
                            records[-1] = LoopRecord(
                                loop=records[-1].loop,
                                screenshot=records[-1].screenshot,
                                model_output=records[-1].model_output,
                                action=records[-1].action,
                                result=ActionResult(
                                    success=False,
                                    finished=False,
                                    message=previous_action_result,
                                ),
                            )
                            self._log(f"[Loop {loop}] Result: {previous_action_result}")
                            continue

                        return AgentRunResult(
                            success=result.success,
                            message=result.message or pending_finish_confirmation.message,
                            loops=records,
                        )

                    return AgentRunResult(
                        success=result.success,
                        message=result.message,
                        loops=records,
                    )

                duration_rejection = self._duration_finish_rejection(
                    task,
                    duration_started_at,
                    duration_budget_seconds,
                )
                if duration_rejection:
                    previous_action_result = duration_rejection
                    records[-1] = LoopRecord(
                        loop=records[-1].loop,
                        screenshot=records[-1].screenshot,
                        model_output=records[-1].model_output,
                        action=records[-1].action,
                        result=ActionResult(
                            success=False,
                            finished=False,
                            message=duration_rejection,
                        ),
                    )
                    self._log(f"[Loop {loop}] Result: {duration_rejection}")
                    continue

                finish_validation_message = _validate_finish_action(
                    task=task,
                    records=records,
                    current_screenshot=screenshot,
                )
                if finish_validation_message is None:
                    previous_action_result = (
                        "Finish was rejected because the current semantic milestone "
                        "is not mechanically complete yet. Continue the immutable "
                        "original user goal with the next required do(...) action. "
                        f"Original goal: {_compact_text(task.strip(), limit=700)}"
                    )
                    records[-1] = LoopRecord(
                        loop=records[-1].loop,
                        screenshot=records[-1].screenshot,
                        model_output=records[-1].model_output,
                        action=records[-1].action,
                        result=ActionResult(
                            success=False,
                            finished=False,
                            message=previous_action_result,
                        ),
                    )
                    self._log(f"[Loop {loop}] Result: {previous_action_result}")
                    continue

                return AgentRunResult(
                    success=True,
                    message=finish_validation_message or result.message,
                    loops=records,
                )
            else:
                pending_finish_confirmation = None

            if action.name != "Invalid":
                assistant_history = _assistant_history_text(
                    model_output=model_response.content,
                    action=action,
                    result=result,
                    task=task,
                )
                if not result.success:
                    assistant_history = (
                        f"{assistant_history}\n\nRuntime result: "
                        f"{_compact_text(result.message, limit=240)}"
                    )
                conversation.append(
                    {
                        "role": "assistant",
                        "content": assistant_history,
                    }
                )

        return AgentRunResult(
            success=False,
            message=f"Max loops reached: {self.config.max_loops}",
            loops=records,
        )

    def _request_instruction_plan(
        self,
        *,
        task: str,
        active_instruction: str,
        screenshot: Screenshot,
        duration_budget_seconds: int | None,
        live_commands: dict[str, object],
    ) -> dict[str, object]:
        """Ask the model once for a semantic plan before executing an epoch."""
        planning_context = {
            "original_user_goal": _compact_text(task, limit=1400),
            "active_instruction": _compact_text(active_instruction, limit=1000),
            "duration_budget_seconds": duration_budget_seconds,
            "live_command_active": bool(_live_command_items(live_commands)),
            "screen_size": f"{screenshot.width}x{screenshot.height}",
            "current_app": _safe_current_app(self.config.device_id),
        }
        messages: list[dict[str, object]] = [
            {"role": "system", "content": PLANNING_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Planning context:\n" + json.dumps(
                            planning_context,
                            ensure_ascii=False,
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{screenshot.mime_type};base64,{screenshot.base64_data}",
                        },
                    },
                ],
            },
        ]
        self._log("[Planner] Requesting semantic task plan before action execution.")
        try:
            response = self.model_client.ask_messages(messages)
        except ModelError as exc:
            raise AgentError(f"Initial task planning failed: {exc}") from exc
        self._log(f"[Planner] Model output: {response.content}")
        plan = _extract_instruction_plan(response.content)
        if plan is None:
            raise AgentError(
                "Initial task planning returned no valid task_progress plan; no device action was executed."
            )
        return plan

    def _log(self, message: str) -> None:
        """Print a message when verbose mode is enabled."""
        if self.config.verbose:
            print(message)

    def _log_loop_status(self) -> None:
        """Print sidecar status aligned with the current loop when available."""
        provider = self.config.loop_status_provider
        if provider is None or not self.config.verbose:
            return
        try:
            status = provider()
        except Exception:
            return
        if status:
            print(f"[Unicapture] {status}")

    def _log_live_command_status(self, loop: int, commands: dict[str, object]) -> None:
        """Show whether live corrections are active for the next model request."""
        persistent = _live_command_items(commands)
        once = _string_list(commands.get("once"))
        if not persistent and not once:
            return
        active = persistent + once
        preview = "; ".join(_compact_text(item, limit=80) for item in active[-3:])
        latest = _optional_command_text(commands.get("latest"))
        constraints = _string_list(commands.get("constraints"))
        if latest:
            self._log(f"[Loop {loop}] Latest live mission: {_compact_text(latest, limit=120)}")
            if constraints:
                constraint_preview = "; ".join(
                    _compact_text(item, limit=80) for item in constraints[-3:]
                )
                self._log(f"[Loop {loop}] Compatible live constraint(s): {constraint_preview}")
            return
        self._log(f"[Loop {loop}] Active live command(s): {preview}")

    def _read_live_commands(self) -> dict[str, object]:
        """Read latest live user corrections from the optional controller."""
        provider = self.config.live_command_provider
        if provider is None:
            return {}
        try:
            commands = provider()
        except Exception as exc:
            self._log(f"[LiveInput] ignored controller error: {exc}")
            return {}
        if not isinstance(commands, dict):
            return {}
        return commands

    def _build_previous_action_feedback(
        self,
        task: str,
        records: list[LoopRecord],
        current_screenshot: Screenshot,
        previous_action_result: str | None,
    ) -> str | None:
        """Augment previous action feedback when the last action did not change the screen."""
        if not records:
            return previous_action_result

        last_record = records[-1]
        base_message = previous_action_result or last_record.result.message
        if not last_record.result.success:
            return base_message

        repeated_action_feedback = _repeated_action_feedback(records)
        if repeated_action_feedback:
            base_message = f"{base_message} {repeated_action_feedback}"

        change = compare_screenshots(last_record.screenshot, current_screenshot)
        if change.mean_difference >= 0.02 or change.changed_ratio >= 0.01:
            return base_message

        action_name = last_record.action.name
        if action_name == "Tap":
            return (
                f"{base_message} The screen appears unchanged after this tap. "
                "Do not repeat the same tap or tap the same target again. "
                "Re-evaluate whether the step goal is already satisfied, or choose a different control."
            )

        if action_name in {
            "Swipe",
            "Enter",
            "Type",
            "Type_Name",
            "Launch",
            "LaunchApp",
            "Long Press",
            "Double Tap",
        }:
            return (
                f"{base_message} The screen appears unchanged after this action. "
                "Do not repeat the same strategy blindly. Re-evaluate the current screen state first."
            )

        return base_message

    def _maybe_finish_duration_budget(
        self,
        started_at: float | None,
        records: list[LoopRecord],
        duration_budget_seconds: int | None,
    ) -> str | None:
        """Return success when a local duration budget has elapsed."""
        budget = duration_budget_seconds
        if started_at is None or budget is None or budget <= 0 or not records:
            return None

        elapsed = time.monotonic() - started_at
        if elapsed < budget:
            return None
        return f"Milestone completed: duration budget reached ({int(elapsed)}/{budget}s)."

    def _duration_finish_rejection(
        self,
        task: str,
        started_at: float | None,
        duration_budget_seconds: int | None,
    ) -> str | None:
        """Reject model finish while a local duration budget is still running."""
        budget = duration_budget_seconds
        if budget is None or budget <= 0:
            return None
        if started_at is None:
            return (
                "Finish rejected: duration timer has not started yet. Continue the "
                "same original user task, not a generic visible-content task. Original "
                f"task anchor: {_compact_task_contract(task, limit=500)}"
            )

        elapsed = time.monotonic() - started_at
        if elapsed >= budget:
            return None
        return (
            f"Finish rejected: duration budget is not reached yet "
            f"({int(elapsed)}/{budget}s). Continue the same original user task. "
            f"Original task anchor: {_compact_task_contract(task, limit=500)}"
        )

    def _append_duration_feedback(
        self,
        previous_action_result: str | None,
        started_at: float | None,
        duration_budget_seconds: int | None,
    ) -> str | None:
        """Add elapsed duration budget status to model feedback."""
        budget = duration_budget_seconds
        if budget is None or budget <= 0:
            return previous_action_result

        if started_at is None:
            status = (
                f"Duration budget progress: 0/{budget} seconds elapsed; timer not "
                "started until a valid browsable content page is visible."
            )
        else:
            elapsed = int(time.monotonic() - started_at)
            status = (
                f"Duration budget progress: {elapsed}/{budget} seconds elapsed. "
                "This runtime value is authoritative; do not estimate duration from "
                "loop count, swipe count, or your own reasoning."
            )
        if previous_action_result:
            return f"{previous_action_result} {status}"
        return status

    def _build_model_history(self, records: list[LoopRecord]) -> list[ModelHistoryItem]:
        """Build recent history items for the model request."""
        if self.config.history_steps <= 0:
            return []

        recent = records[-self.config.history_steps :]
        return [
            ModelHistoryItem(
                step=record.loop,
                screenshot=record.screenshot,
                model_output=record.model_output,
                action_result=record.result.message,
            )
            for record in recent
        ]

    def _maybe_finish_completed_task(
        self,
        task: str,
        records: list[LoopRecord],
        current_screenshot: Screenshot,
        duration_budget_seconds: int | None,
    ) -> str | None:
        """Return a success message when a transition-like task is already complete."""
        if duration_budget_seconds is not None:
            return None

        if _has_plan_context(task):
            return _maybe_finish_semantic_milestone(
                task=task,
                records=records,
                current_screenshot=current_screenshot,
            )

        if not records:
            return None

        last_record = records[-1]
        if not last_record.result.success:
            return None

        if last_record.action.name not in {"Tap", "Type", "Enter", "Swipe"}:
            return None

        change = compare_screenshots(last_record.screenshot, current_screenshot)
        task_kind = _classify_task_kind(task)

        if task_kind == "strict_target":
            return None

        if task_kind == "page_transition":
            if change.mean_difference >= 0.18 or change.changed_ratio >= 0.35:
                return (
                    "Task completed early: the screen changed significantly after the "
                    "previous action, which matches this navigation task."
                )

        if task_kind == "result_wait":
            if change.mean_difference >= 0.08 or change.changed_ratio >= 0.12:
                return (
                    "Task completed early: the screen changed after input or submit, "
                    "which matches this result-loading task."
                )

        return None


def _classify_task_kind(task: str) -> str | None:
    """Classify a task into a small set of early-finish categories."""
    normalized = "".join(task.strip().lower().split())
    if not normalized:
        return None

    strict_target_keywords = (
        "进入主页",
        "进入个人主页",
        "打开主页",
        "用户主页",
        "个人主页",
        "找到用户并进入",
        "进入其主页",
        "进入他的主页",
        "进入她的主页",
        "openprofile",
        "userprofile",
        "profilepage",
        "打开推荐列表",
        "进入推荐列表",
        "推荐列表",
        "recommendlist",
        "进入关注列表",
        "打开关注列表",
        "followinglist",
    )
    if any(keyword in normalized for keyword in strict_target_keywords):
        return "strict_target"

    page_transition_keywords = (
        "进入搜索页",
        "进入搜索页面",
        "打开搜索页",
        "打开搜索页面",
        "点击搜索图标",
        "进入评论区",
        "打开评论区",
        "打开关注列表",
        "进入关注列表",
        "打开列表",
        "进入列表",
        "opensearch",
        "entersearch",
        "opencomments",
        "openfollowing",
    )
    if any(keyword in normalized for keyword in page_transition_keywords):
        return "page_transition"

    result_wait_keywords = (
        "等待结果",
        "搜索结果",
        "执行搜索",
        "提交搜索",
        "输入搜索内容并等待结果",
        "searchresults",
        "waitforresults",
        "submitsearch",
    )
    if any(keyword in normalized for keyword in result_wait_keywords):
        return "result_wait"

    return None


def _build_conversation_user_message(
    *,
    task: str,
    active_goal: str,
    runtime_context: str,
    mission_audit: str,
    chat_memory: str | None,
    live_override: str | None,
    operation_style: str | None,
    live_corrections: str | None,
    instruction_progress: str | None,
    recent_facts: str,
    screenshot: Screenshot,
    previous_action: str | None,
    is_first: bool,
    device_id: str | None,
    target_app_package: str | None,
) -> dict[str, object]:
    """Build a lightweight history-mode user message for the current screen."""
    screen_info = {
        "screen_size": f"{screenshot.width}x{screenshot.height}",
        "current_app": _safe_current_app(device_id),
    }
    if target_app_package:
        screen_info["target_app_package"] = target_app_package
        screen_info["launch_hint"] = (
            "If the target app is not foreground, prefer "
            f'do(action="LaunchApp", app="{target_app_package}").'
        )
    if previous_action:
        screen_info["previous_action_result"] = previous_action

    original_task = task.strip()
    operation_style_text = (operation_style or "").strip()
    chat_memory_text = (chat_memory or "").strip()
    live_override_text = (live_override or "").strip()
    live_corrections_text = (live_corrections or "").strip()
    instruction_progress_text = (instruction_progress or "").strip()
    recent_facts_text = (recent_facts or "- No actions have been executed yet.").strip()
    if _conversation_task_detected(task):
        operation_style_text = _chat_operation_style_text()

    state_packet = _build_turn_state_packet(
        original_task=original_task,
        active_goal=active_goal,
        runtime_context=runtime_context,
        mission_audit=mission_audit,
        live_override=live_override_text,
        live_corrections=live_corrections_text,
        operation_style=operation_style_text,
        chat_memory=chat_memory_text,
        instruction_progress=instruction_progress_text,
        recent_facts=recent_facts_text,
        previous_action=previous_action,
        screen_info=screen_info,
    )

    if is_first:
        text = (
            "Initial task context:\n"
            f"{original_task}\n\n"
            f"{state_packet}"
        )
    else:
        text = state_packet

    return {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{screenshot.mime_type};base64,{screenshot.base64_data}",
                },
            },
        ],
    }


def _build_live_command_user_message(commands: tuple[str, ...]) -> dict[str, object]:
    """Insert live user corrections as first-class conversation history."""
    command_text = "\n".join(f"- {item}" for item in commands if item)
    return {
        "role": "user",
        "content": (
            "New live command accepted during this run:\n"
            f"{command_text}\n\n"
            "The current state packet contains the active priority and compatible constraints."
        ),
    }


def _build_turn_state_packet(
    *,
    original_task: str,
    active_goal: str,
    runtime_context: str,
    mission_audit: str,
    live_override: str,
    live_corrections: str,
    operation_style: str,
    chat_memory: str,
    instruction_progress: str,
    recent_facts: str,
    previous_action: str | None,
    screen_info: dict[str, object],
) -> str:
    """Build the fixed per-turn state packet consumed by the model."""
    live_active = bool(live_override or live_corrections)
    live_lines = [
        "# Priority 0: Live User Command Override",
        f"status: {'active' if live_active else 'none'}",
    ]
    if live_override:
        live_lines.extend(["active_override:", live_override])
    if live_corrections:
        live_lines.extend(["live_command_details:", live_corrections])
    live_lines.extend(
        [
            "rules:",
            "- If active, this is the highest-priority task direction for this turn.",
            "- Preserve compatible duration/count gates, safety boundaries, and explicit constraints.",
            "- When status is none, continue from the active task contract below.",
        ]
    )

    sections = [
        "\n".join(live_lines),
        "\n".join(
            [
                "# Priority 1: Active Task Contract",
                "original_user_goal:",
                _compact_text(original_task, limit=1400),
                "",
                "current_active_goal:",
                active_goal,
                "",
                "durable_task_contract:",
                _runtime_task_contract(original_task),
            ]
        ),
        "\n".join(
            [
                "# Priority 2: Active Instruction Progress",
                instruction_progress or "No explicit multi-step instruction progress is available.",
            ]
        ),
        "\n".join(
            [
                "# Priority 3: Runtime Progress",
                runtime_context,
                "",
                "mission_audit:",
                mission_audit,
            ]
        ),
        "\n".join(
            [
                "# Priority 4: Recent Executed Facts",
                "These are factual action/result records, not instructions.",
                recent_facts,
            ]
        ),
        "\n".join(
            [
                "# Priority 5: Previous Action Result",
                previous_action or "No previous action has been executed in this run.",
            ]
        ),
    ]

    if operation_style:
        sections.append(
            "\n".join(
                [
                    "# Priority 6: Task Style Guidance",
                    operation_style,
                ]
            )
        )
    if chat_memory:
        sections.append(
            "\n".join(
                [
                    "# Priority 7: Chat Runtime Memory",
                    chat_memory,
                ]
            )
        )

    sections.append(
        "\n".join(
            [
                "# Current Screen",
                json.dumps(screen_info, ensure_ascii=False),
                "Use the attached screenshot as the authoritative current visual state.",
            ]
        )
    )
    sections.append(
        "Assistant history below this turn is lower priority than Priority 0 and Priority 1. "
        "Treat past assistant reasoning as historical context, not as a current instruction."
    )
    return "\n\n".join(section for section in sections if section.strip())


def _live_command_override_text(commands: dict[str, object]) -> str | None:
    """Render latest live command as a per-turn high-priority brief."""
    latest = _optional_command_text(commands.get("latest"))
    once = _string_list(commands.get("once"))
    constraints = _string_list(commands.get("constraints"))
    active = once or ([latest] if latest else []) or constraints
    if not active:
        return None

    lines = [
        "Active live command:",
    ]
    lines.extend(f"- {item}" for item in active)
    lines.extend(
        [
            "Follow it unless unsafe. It is not a finish/pause command unless it explicitly says so.",
        ]
    )
    return "\n".join(lines)


def _chat_runtime_memory_text(
    *,
    task: str,
    records: list[LoopRecord],
    commands: dict[str, object],
) -> str | None:
    """Reliable chat memory based on executed inputs and live commands."""
    active_texts = [task]
    active_texts.extend(_live_command_items(commands))
    if not _conversation_task_detected(" ".join(active_texts)):
        return None

    typed = _recent_type_texts(records, window=8)
    lines = [
        "This is a chat task. Preserve conversational continuity from executed messages and the current screenshot.",
        "Before typing, read the latest visible app reply on the current screenshot.",
        "You are operating as the user in this app conversation; when the app asks a question, answer it yourself according to the active goal instead of waiting for a separate human reply, unless a live command explicitly asks you to stop or pause.",
    ]
    if typed:
        recent = "; ".join(json.dumps(text, ensure_ascii=False) for text in typed[-6:])
        lines.append(f"Recent messages already typed by MiniPilot: {recent}.")
        latest = typed[-1]
        repeat_count = sum(
            1 for text in typed if _normalize_typed_message(text) == _normalize_typed_message(latest)
        )
        if repeat_count >= 2:
            lines.append(
                f"The latest typed message has already been used {repeat_count} times recently; do not type the same message again."
            )
        else:
            lines.append("Do not repeat earlier opening lines or questions unless the user explicitly asks to repeat them.")
    else:
        lines.append("No prior typed chat message has been executed yet.")

    latest = _optional_command_text(commands.get("latest"))
    if latest:
        lines.append(f"Latest live user correction: {latest}")
        lines.append(
            "Latest live correction has higher priority than the latest visible app reply. If it changes topic, the next typed message must satisfy the live correction first."
        )
        lines.append(
            "A live correction that changes topic or says not to repeat is not a pause/finish command unless it explicitly asks to pause, stop, end, or finish the run."
        )

    if latest:
        lines.append(
            "For the next Type action, write a fresh message that follows the latest live correction. Use the current app reply only if it is compatible with that correction."
        )
    else:
        lines.append(
            "For the next Type action, write a fresh message that responds to the latest visible app reply."
        )
    return "\n".join(lines)


def _assistant_history_text(
    *,
    model_output: str,
    action: Action,
    result: ActionResult,
    task: str,
) -> str:
    """Store useful assistant memory without reinforcing bad long thinking loops."""
    action_text = _action_history_text(action)
    result_text = _compact_text(result.message, limit=180)
    if not _conversation_task_detected(task):
        return f"Assistant executed: {action_text}\nRuntime result: {result_text}"

    if action.name in {"Type", "Type_Name"}:
        typed = str(action.params.get("text", ""))
        return (
            f"Assistant executed chat input: {action_text}\n"
            f"Typed message memory: {json.dumps(typed, ensure_ascii=False)}\n"
            f"Runtime result: {result_text}"
        )
    return f"Assistant executed: {action_text}\nRuntime result: {result_text}"


def _chat_operation_style_text() -> str:
    """Task style for chat apps; avoid browse/feed-specific behavior guidance."""
    return (
        "Chat operation style:\n"
        "- Treat the task as an ongoing conversation, not as generic app browsing.\n"
        "- Read the latest visible app reply before typing a new message.\n"
        "- Do not repeat the same question, opening line, or generic topic starter.\n"
        "- Continue from the app's latest reply: answer its question, choose one of its options, ask a relevant follow-up, or comment on a concrete point it made.\n"
        "- If the latest app reply is not visible, still loading, or hidden by the keyboard, wait or adjust the screen before typing.\n"
        "- For timed chat tasks, maintain conversation quality for the duration; do not keep the task alive by mechanically repeating one message."
    )


def _mission_runtime_context_text(
    *,
    task: str,
    runtime_contract: str,
    records: list[LoopRecord],
    current_screenshot: Screenshot,
    duration_started_at: float | None,
    duration_budget_seconds: int | None,
    device_id: str | None,
    target_app_package: str | None = None,
) -> str:
    """Build useful runtime facts without imposing a step plan."""
    current_app = _safe_current_app(device_id)
    target_package = target_app_package or _extract_target_package_from_task(task)
    targeted = _duration_requires_target_posted_video(task)
    observations: list[str] = []
    gates: list[str] = []
    action_summary = _recent_action_summary(records)

    if target_package:
        if current_app == target_package:
            observations.append("target app appears to be foreground")
        else:
            observations.append("target app does not appear to be foreground")

    if targeted:
        if any(
            record.result.success and record.action.name in {"Type", "Type_Name"}
            for record in records
        ):
            observations.append("target lookup text has been entered at least once")
        else:
            observations.append("target entity/content source lookup is not yet evident from action history")

        if _opened_target_video_content(records):
            observations.append("a candidate target-owned content item has been opened")
        else:
            observations.append("target-owned content consumption is not yet evident from action history")

    if duration_budget_seconds:
        if duration_started_at is None:
            gates.append(
                f"duration progress is 0/{duration_budget_seconds}s; runtime timer has not started"
            )
        else:
            elapsed = int(time.monotonic() - duration_started_at)
            gates.append(
                f"duration progress is {elapsed}/{duration_budget_seconds}s by runtime clock"
            )
        gates.append("finish is not allowed until runtime duration/count gates are satisfied")
    else:
        gates.append("no runtime duration gate is active")

    if not observations:
        observations.append("no additional runtime observations are available yet")

    context = {
        "current_app": current_app,
        "target_package": target_package or "unknown/not parsed",
        "parsed_runtime_constraints": runtime_contract,
        "root_goal_compliance": _root_goal_compliance(
            task=task,
            records=records,
            current_screenshot=current_screenshot,
            duration_started_at=duration_started_at,
            duration_budget_seconds=duration_budget_seconds,
            action_summary=action_summary,
        ),
        "root_goal_preferences": _root_goal_preferences(task),
        "recent_action_summary": action_summary,
        "recent_behavior_trajectory": _recent_behavior_trajectory(action_summary),
        "conversation_progress": _conversation_progress_summary(
            task=task,
            records=records,
        ),
        "cadence_progress": _cadence_progress_summary(
            task=task,
            records=records,
            current_screenshot=current_screenshot,
        ),
        "runtime_observations": observations,
        "runtime_gates": gates,
        "self_check": (
            "During planning, compare recent actions with the current active user "
            "goal. If the behavior pattern is drifting, recover autonomously."
        ),
        "internal_mission_note": (
            "Keep an internal mission note about what part of the root goal has "
            "been satisfied and what has been neglected. Do not output the note; "
            "use it only for task reasoning."
        ),
    }
    return json.dumps(context, ensure_ascii=False, indent=2)


def _live_corrections_text(commands: dict[str, object]) -> str | None:
    """Render live user corrections collected during the current run."""
    latest = _optional_command_text(commands.get("latest"))
    constraints = _string_list(commands.get("constraints"))
    superseded = _string_list(commands.get("superseded"))
    persistent = _live_command_items(commands)
    once = _string_list(commands.get("once"))
    if not persistent and not once:
        return None

    lines = [
        "These are live user commands entered while the task is running.",
        "Priority: one-shot correction first; otherwise latest live mission plus compatible constraints.",
        "Superseded live commands are historical only.",
    ]
    if once:
        lines.append("One-shot correction for this turn:")
        lines.extend(f"- {item}" for item in once)
    if latest:
        lines.append("Latest live mission:")
        lines.append(f"- {latest}")
    if constraints:
        lines.append("Compatible live constraints:")
        lines.extend(f"- {item}" for item in constraints)
    if superseded:
        lines.append("Superseded live commands:")
        lines.extend(f"- {item}" for item in superseded[-3:])
    return "\n".join(lines)


def _active_user_goal_text(
    *,
    task: str,
    commands: dict[str, object],
    records: list[LoopRecord],
    duration_budget_seconds: int | None,
) -> str:
    """Render the current task direction without choosing the next action."""
    original_task = task.strip()
    latest = _optional_command_text(commands.get("latest"))
    constraints = _string_list(commands.get("constraints"))
    superseded = _string_list(commands.get("superseded"))
    persistent = _live_command_items(commands)
    once = _string_list(commands.get("once"))
    active_commands = persistent + once
    summary = _recent_action_summary(records)

    if not active_commands:
        return "\n".join(
            [
                "No live user command is active.",
                f"Active goal source: immutable original user goal.",
                f"Active goal: {original_task}",
                "Use runtime facts and the screenshot to continue this goal autonomously.",
            ]
        )

    lines = [
        "Live user commands are active.",
        "Active goal source: one-shot correction if present; otherwise latest live mission plus compatible constraints.",
        "Priority: safety boundaries remain highest; superseded live commands are historical only.",
        f"Original goal for context: {original_task}",
    ]
    if once:
        lines.append("One-shot correction for this turn, highest current-turn priority:")
        lines.extend(f"- {item}" for item in once)
    if latest:
        lines.append("Latest live mission:")
        lines.append(f"- {latest}")
    else:
        lines.append("Latest live mission:")
        lines.append("- none; use compatible constraints and original goal")
    if constraints:
        lines.append("Compatible live constraints:")
        lines.extend(f"- {item}" for item in constraints)
    if superseded:
        lines.append("Superseded live commands, historical only:")
        lines.extend(f"- {item}" for item in superseded[-3:])
    lines.extend(
        [
            "Current execution requirement:",
            "- Satisfy the current live direction before resuming older browsing habits.",
            "- If it asks for a specific visible surface/control, use that surface/control rather than moving away from it.",
            "- If recent behavior conflicts with it, recover from the current screenshot using your own screen understanding.",
        ]
    )

    semantic_requirements = _live_command_semantic_requirements(active_commands)
    if semantic_requirements:
        lines.append("Detected active-command semantics:")
        lines.extend(f"- {item}" for item in semantic_requirements)

    lines.append("Recent active-command alignment:")
    lines.append(_active_command_alignment_text(active_commands, summary, records))
    lines.append(_recent_behavior_trajectory(summary))
    return "\n".join(lines)


def _live_command_semantic_requirements(commands: list[str]) -> list[str]:
    """Extract lightweight live-command facts without making a step plan."""
    joined = " ".join(commands).lower()
    compact = "".join(joined.split())
    requirements: list[str] = []

    if any(marker in compact for marker in ("\u8bc4\u8bba", "comment")):
        requirements.append(
            "Comment-view requirement detected: the active command requires opening or using comments; if a comment entry/control is visible on the current screen, opening it is direct progress, while moving to the next item is not evidence of satisfying this requirement for the current item."
        )
    if any(marker in compact for marker in ("\u6bcf\u6761", "\u6bcf\u4e2a", "\u6bcf\u4e00", "every")):
        requirements.append(
            "Per-item cadence detected: apply the active requirement to each/current item until the user changes it."
        )
    if any(marker in compact for marker in ("\u540c\u57ce", "\u699c", "\u5217\u8868", "\u8be6\u60c5", "city", "ranking", "rank", "list", "detail")):
        requirements.append(
            "Surface-navigation requirement detected: the active command names a specific surface or content area; navigate to that surface instead of only consuming the current feed."
        )
    if _conversation_task_detected(joined):
        requirements.append(
            "Conversation/reply requirement detected: use the visible conversation content and recent typed messages to continue the dialogue; repeating a starter topic is not evidence of responding to the app's latest reply."
        )
    return requirements


def _active_command_alignment_text(
    commands: list[str],
    action_summary: dict[str, object],
    records: list[LoopRecord],
) -> str:
    """Compare live commands with recent behavior as facts, not a blocklist."""
    total = int(action_summary.get("successful_actions_considered") or 0)
    if total <= 0:
        return "No successful recent behavior is available yet for active-command alignment."

    counts = action_summary.get("counts")
    if not isinstance(counts, dict) or not counts:
        return "No successful recent behavior is available yet for active-command alignment."

    dominant = action_summary.get("dominant_action")
    dominant_count = counts.get(dominant, 0) if isinstance(dominant, str) else 0
    tap_count = int(counts.get("Tap", 0) or 0)
    swipe_count = int(counts.get("Swipe", 0) or 0)
    distinct = int(action_summary.get("distinct_action_types") or 0)
    joined = " ".join(commands).lower()
    compact = "".join(joined.split())
    view_or_panel_requested = any(
        marker in compact
        for marker in (
            "\u8bc4\u8bba",
            "\u540c\u57ce",
            "\u699c",
            "\u5217\u8868",
            "\u8be6\u60c5",
            "comment",
            "city",
            "ranking",
            "rank",
            "list",
            "detail",
            "open",
            "enter",
        )
    )

    if view_or_panel_requested and swipe_count == total and total >= 3:
        return (
            f"Recent successful actions are all Swipe ({swipe_count}/{total}) while "
            "the active command asks for a specific view/surface interaction; that "
            "surface interaction is not yet evidenced by recent actions."
        )
    if view_or_panel_requested and tap_count == 0 and total >= 3:
        return (
            f"Recent behavior has no successful Tap in the last {total} actions while "
            "the active command appears to require entering or opening a surface."
        )
    if distinct == 1 and total >= 5:
        return (
            f"Recent behavior is dominated by one action type: {dominant} "
            f"({dominant_count}/{total}). Compare this fact with the active live command before acting."
        )
    conversation = _conversation_progress_summary(
        task=" ".join(commands),
        records=records,
    )
    if conversation and conversation.get("repeated_type_text"):
        return (
            "Recent typed-message facts show repeated text while the active command "
            "appears conversational. Compare the repeated text with the visible app "
            "reply before choosing the next message."
        )
    return "Recent behavior does not show an obvious action-type conflict with the active live command."


def _live_command_items(commands: dict[str, object]) -> list[str]:
    """Return active live commands while supporting old and new snapshots."""
    latest = _optional_command_text(commands.get("latest"))
    constraints = _string_list(commands.get("constraints"))
    if latest or constraints:
        items: list[str] = []
        if latest:
            items.append(latest)
        items.extend(constraints)
        return items
    return _string_list(commands.get("persistent"))


def _optional_command_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _mission_audit_text(
    *,
    task: str,
    records: list[LoopRecord],
    current_screenshot: Screenshot,
    duration_started_at: float | None,
    duration_budget_seconds: int | None,
) -> str:
    """Write a compact natural-language audit of root-goal fulfillment facts."""
    action_summary = _recent_action_summary(records)
    preferences = _root_goal_preferences(task)
    lines = [
        "The current active user goal is the mission anchor; live user commands may override the original goal for active direction.",
    ]

    if duration_budget_seconds is not None:
        if duration_started_at is None:
            lines.append(
                f"Duration audit: 0/{duration_budget_seconds}s elapsed; timer has not started."
            )
        else:
            elapsed = int(time.monotonic() - duration_started_at)
            lines.append(
                f"Duration audit: {elapsed}/{duration_budget_seconds}s elapsed by runtime clock."
            )
        lines.append(
            "Duration meaning: this is wall-clock elapsed time, not a count of swipes, videos, loops, taps, or model turns."
        )

    if preferences:
        lines.append("Stated root-goal preferences: " + "; ".join(preferences) + ".")

    alignment = _preference_alignment_text(preferences, action_summary)
    lines.append("Preference audit: " + alignment)
    cadence = _cadence_progress_summary(
        task=task,
        records=records,
        current_screenshot=current_screenshot,
    )
    if cadence:
        lines.append("Cadence audit: " + str(cadence.get("cadence_alignment")))
    conversation = _conversation_progress_summary(task=task, records=records)
    if conversation:
        lines.append("Conversation audit: " + str(conversation.get("conversation_alignment")))
    lines.append(
        "Use this audit to choose autonomously from the current screenshot; it is not a forced next-action plan."
    )
    return "\n".join(lines)


def _root_goal_compliance(
    *,
    task: str,
    records: list[LoopRecord],
    current_screenshot: Screenshot,
    duration_started_at: float | None,
    duration_budget_seconds: int | None,
    action_summary: dict[str, object],
) -> dict[str, object]:
    """Summarize root-goal fulfillment facts without choosing the next action."""
    preferences = _root_goal_preferences(task)
    compliance: dict[str, object] = {
        "immutable_goal": task,
        "stated_preferences": preferences,
        "recent_behavior": action_summary.get("counts", {}),
        "recent_behavior_trajectory": _recent_behavior_trajectory(action_summary),
        "preference_alignment": _preference_alignment_text(preferences, action_summary),
        "conversation_progress": _conversation_progress_summary(
            task=task,
            records=records,
        ),
        "cadence_progress": _cadence_progress_summary(
            task=task,
            records=records,
            current_screenshot=current_screenshot,
        ),
    }

    if duration_budget_seconds is not None:
        if duration_started_at is None:
            duration_status = (
                f"active 0/{duration_budget_seconds} seconds; runtime timer has not started"
            )
        else:
            elapsed = int(time.monotonic() - duration_started_at)
            duration_status = f"active {elapsed}/{duration_budget_seconds} seconds"
        compliance["duration_status"] = duration_status
        compliance["duration_semantics"] = (
            "The requested duration is wall-clock elapsed runtime, not a number of "
            "swipes, videos, loops, taps, or model turns."
        )
    else:
        compliance["duration_status"] = "no runtime duration budget parsed"

    if records:
        compliance["last_successful_action"] = _last_successful_action_name(records)
        compliance["successful_action_count"] = sum(
            1 for record in records if record.result.success
        )
    else:
        compliance["last_successful_action"] = None
        compliance["successful_action_count"] = 0

    return compliance


def _root_goal_preferences(task: str, limit: int = 6) -> list[str]:
    """Extract notable root-goal clauses without turning them into a plan."""
    clauses = [
        clause.strip()
        for clause in re.split(r"[\n,，。；;.!?！？]+", task)
        if clause.strip()
    ]
    markers = (
        "\u4e0d\u8981",  # 不要
        "\u522b",  # 别
        "\u4e0d\u80fd",  # 不能
        "\u7981\u6b62",  # 禁止
        "\u9700\u8981",  # 需要
        "\u5fc5\u987b",  # 必须
        "\u6bcf",  # 每
        "\u7ecf\u5e38",  # 经常
        "\u53ef\u4ee5",  # 可以
        "\u591a",  # 多
        "\u53ea",  # 只
        "do not",
        "don't",
        "must",
        "need",
        "every",
        "often",
        "frequent",
        "only",
    )
    preferences: list[str] = []
    for clause in clauses:
        compact = "".join(clause.lower().split())
        if any(marker in compact for marker in markers):
            preferences.append(clause)
        if len(preferences) >= limit:
            break
    return preferences


def _preference_alignment_text(
    preferences: list[str],
    action_summary: dict[str, object],
) -> str:
    """Compare stated preferences with recent behavior as facts, not commands."""
    total = int(action_summary.get("successful_actions_considered") or 0)
    if total <= 0:
        return "No successful recent behavior is available yet for preference alignment."

    counts = action_summary.get("counts")
    if not isinstance(counts, dict) or not counts:
        return "No successful recent behavior is available yet for preference alignment."

    dominant = action_summary.get("dominant_action")
    dominant_count = counts.get(dominant, 0) if isinstance(dominant, str) else 0
    distinct = int(action_summary.get("distinct_action_types") or 0)
    dominated = distinct == 1 or (total >= 5 and dominant_count / total >= 0.8)
    variation_preference = _preferences_indicate_variation(preferences)

    if variation_preference and dominated:
        return (
            "Root-goal preferences mention avoiding one-note behavior or allowing "
            "varied interactions; recent behavior is dominated by "
            f"{dominant} ({dominant_count}/{total}), so that preference is not yet "
            "clearly evidenced by recent actions."
        )
    if variation_preference:
        return (
            "Root-goal preferences mention varied or non-monotone behavior; recent "
            f"actions show {distinct} action types across {total} successful actions."
        )
    if dominated:
        return (
            f"Recent behavior is dominated by {dominant} ({dominant_count}/{total}); "
            "compare this fact with the current active goal before acting."
        )
    return "Recent behavior is mixed; no obvious preference drift is evidenced by action types."


def _preferences_indicate_variation(preferences: list[str]) -> bool:
    """Return whether extracted preferences ask to avoid monotony or add variety."""
    markers = (
        "\u4e0d\u8981\u4e00\u76f4",  # 不要一直
        "\u522b\u4e00\u76f4",  # 别一直
        "\u4e0d\u53ea",  # 不只
        "\u4e0d\u8981\u53ea",  # 不要只
        "\u522b\u53ea",  # 别只
        "\u5176\u4ed6",  # 其他
        "\u522b\u7684",  # 别的
        "\u591a\u70b9",  # 多点
        "\u591a\u70b9\u70b9",  # 多点点
        "not only",
        "do not only",
        "don't only",
        "not just",
        "different",
        "varied",
        "variety",
    )
    for preference in preferences:
        compact = "".join(preference.lower().split())
        if any(marker in compact for marker in markers):
            return True
    return False


def _last_successful_action_name(records: list[LoopRecord]) -> str | None:
    for record in reversed(records):
        if record.result.success:
            return record.action.name
    return None


def _recent_action_summary(records: list[LoopRecord], window: int = 10) -> dict[str, object]:
    """Return a neutral distribution of recent successful actions."""
    recent: list[str] = []
    for record in reversed(records):
        if record.result.success:
            recent.append(record.action.name)
        if len(recent) >= window:
            break
    recent.reverse()

    counts: dict[str, int] = {}
    for name in recent:
        counts[name] = counts.get(name, 0) + 1

    dominant_action = None
    if counts:
        dominant_action = max(counts, key=counts.get)

    return {
        "window": window,
        "successful_actions_considered": len(recent),
        "counts": counts,
        "dominant_action": dominant_action,
        "distinct_action_types": len(counts),
    }


def _recent_behavior_trajectory(summary: dict[str, object]) -> str:
    """Describe recent behavior in neutral natural language."""
    total = int(summary.get("successful_actions_considered") or 0)
    if total <= 0:
        return "Recent behavior trajectory: no successful actions are available yet."

    counts = summary.get("counts")
    if not isinstance(counts, dict) or not counts:
        return "Recent behavior trajectory: no successful actions are available yet."

    dominant = summary.get("dominant_action")
    dominant_count = counts.get(dominant, 0) if isinstance(dominant, str) else 0
    distinct = int(summary.get("distinct_action_types") or 0)

    if distinct == 1:
        return (
            f"Recent behavior trajectory: the last {total} successful actions are "
            f"all {dominant}."
        )
    if dominant and dominant_count / total >= 0.7:
        return (
            f"Recent behavior trajectory: recent successful actions are mostly "
            f"{dominant} ({dominant_count}/{total})."
        )
    return (
        "Recent behavior trajectory: recent successful actions are mixed across "
        f"{distinct} action types."
    )


def _conversation_progress_summary(
    *,
    task: str,
    records: list[LoopRecord],
    window: int = 12,
) -> dict[str, object] | None:
    """Summarize conversational progress facts without blocking any action."""
    if not _conversation_task_detected(task):
        return None

    typed = _recent_type_texts(records, window=window)
    if not typed:
        return {
            "chat_task_detected": True,
            "recent_typed_messages": [],
            "conversation_alignment": (
                "No successful typed messages are available yet for conversation alignment."
            ),
        }

    normalized_counts: dict[str, int] = {}
    display_by_normalized: dict[str, str] = {}
    for text in typed:
        normalized = _normalize_typed_message(text)
        if not normalized:
            continue
        normalized_counts[normalized] = normalized_counts.get(normalized, 0) + 1
        display_by_normalized.setdefault(normalized, text)

    repeated_key = None
    repeated_count = 0
    latest_key = _normalize_typed_message(typed[-1])
    if latest_key and normalized_counts.get(latest_key, 0) >= 2:
        repeated_key = latest_key
        repeated_count = normalized_counts[latest_key]
    else:
        for key, count in normalized_counts.items():
            if count > repeated_count:
                repeated_key = key if count >= 2 else None
                repeated_count = count

    recent_display = [_compact_text(text, limit=80) for text in typed[-6:]]
    opening_message = typed[0]
    observations = _recent_chat_observations(records)
    base: dict[str, object] = {
        "chat_task_detected": True,
        "opening_message_already_sent": _compact_text(opening_message, limit=100),
        "recent_typed_messages": recent_display,
        "global_chat_memory": (
            "The first topic/opening message has already been sent. Later turns "
            "should be treated as follow-up/reply phase. For the reply content, "
            "trust the current screenshot's visible app reply over prior model "
            "observations or guessed summaries."
        ),
        "chat_grounding_rule": (
            "Before typing in a chat task, identify the latest visible app reply "
            "from the current screenshot. If the latest app reply is not readable, "
            "still loading, or hidden by the keyboard, wait or adjust the screen "
            "instead of inventing a reply."
        ),
    }
    if repeated_key:
        repeated_text = display_by_normalized.get(repeated_key, repeated_key)
        base.update(
            {
                "repeated_type_text": _compact_text(repeated_text, limit=100),
                "repeated_type_count": repeated_count,
                "conversation_stage": "follow-up/reply phase; opening topic is already done",
                "conversation_alignment": (
                "Recent Type actions repeatedly used the same message. For a chat "
                "or reply task, this is weak evidence of responding to the visible "
                "app reply; use the screenshot content and active live command to "
                "continue the conversation with a fresh relevant message."
                ),
            }
        )
        return base

    base.update(
        {
            "conversation_stage": "follow-up/reply phase after opening message",
            "conversation_alignment": (
            "Recent typed messages are not obviously repeated; continue comparing "
            "new replies with the active chat goal."
            ),
        }
    )
    return base


def _conversation_task_detected(text: str) -> bool:
    """Return whether text suggests a chat/reply task."""
    compact = "".join(text.lower().split())
    markers = (
        "\u804a",  # 聊
        "\u5bf9\u8bdd",  # 对话
        "\u56de\u590d",  # 回复
        "\u8ffd\u95ee",  # 追问
        "\u9605\u8bfb",  # 阅读
        "\u9488\u5bf9",  # 针对
        "\u6839\u636e",  # 根据
        "\u518d\u8fdb\u884c\u56de\u590d",  # 再进行回复
        "chat",
        "reply",
        "respond",
        "followup",
        "follow-up",
        "conversation",
    )
    return any(marker in compact for marker in markers)


def _open_ended_finish_rejection(
    task: str,
    commands: dict[str, object],
) -> str | None:
    """Reject model finish for explicit open-ended chat missions."""
    active_text = "\n".join(
        [task]
        + _live_command_items(commands)
        + _string_list(commands.get("once"))
    )
    if not _conversation_task_detected(active_text):
        return None
    if _extract_duration_budget_seconds(active_text) is not None:
        return None

    compact = "".join(active_text.lower().split())
    open_ended_markers = (
        "\u6301\u7eed",  # continue/persistent
        "\u4e0d\u8981\u4e3b\u52a8\u7ed3\u675f",  # do not actively end
        "\u4e0d\u8981\u7ed3\u675f",  # do not end
        "\u4e0d\u8981\u505c",  # do not stop
        "\u4e0d\u505c",  # nonstop
        "\u4efb\u4f55\u6761\u4ef6\u4e0b\u4e0d\u8981",  # under any circumstances do not
        "continuechat",
        "keepchatting",
        "ongoingchat",
        "donotfinish",
        "donotstop",
        "donotend",
        "neverfinish",
        "neverstop",
    )
    if not any(marker in compact for marker in open_ended_markers):
        return None

    return (
        "Finish rejected: ongoing chat is still active. Continue with the next "
        "app action from the current screen; follow the latest live command."
    )


def _recent_type_texts(records: list[LoopRecord], window: int = 12) -> list[str]:
    """Return recent successful Type/Type_Name texts in chronological order."""
    texts: list[str] = []
    for record in reversed(records):
        if (
            record.result.success
            and record.action.name in {"Type", "Type_Name"}
            and isinstance(record.action.params.get("text"), str)
        ):
            text = str(record.action.params.get("text", "")).strip()
            if text:
                texts.append(text)
        if len(texts) >= window:
            break
    texts.reverse()
    return texts


def _recent_chat_observations(records: list[LoopRecord], limit: int = 3) -> list[str]:
    """Keep compact prior screen understanding for chat tasks."""
    observations: list[str] = []
    for record in reversed(records):
        text = _strip_action_calls(record.model_output).strip()
        if not text:
            continue
        compact = _compact_text(text, limit=420)
        if compact:
            observations.append(f"Loop {record.loop}: {compact}")
        if len(observations) >= limit:
            break
    observations.reverse()
    return observations


def _strip_action_calls(text: str) -> str:
    """Remove executable tail calls from stored model observations."""
    text = re.sub(r"<answer\b[^>]*>.*?</answer>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"do\s*\(.*?\)\s*$", "", text, flags=re.DOTALL)
    text = re.sub(r"finish\s*\(.*?\)\s*$", "", text, flags=re.DOTALL)
    return text


def _normalize_typed_message(text: str) -> str:
    """Normalize typed message text for repeat detection."""
    return re.sub(r"\s+", "", text).strip().lower()


def _cadence_progress_summary(
    *,
    task: str,
    records: list[LoopRecord],
    current_screenshot: Screenshot,
    window: int = 14,
) -> dict[str, object] | None:
    """Summarize item-cadence progress without choosing or blocking actions."""
    cadence = _cadence_requirement_hint(task)
    if cadence is None:
        return None

    successful = [record for record in records if record.result.success]
    if not successful:
        return {
            "cadence_goal": cadence,
            "recent_route_pattern": "no successful actions yet",
            "cadence_alignment": (
                "No successful behavior is available yet for cadence alignment."
            ),
        }

    transitions = _action_screen_transitions(records, current_screenshot)
    latest_content_transition = _latest_effective_content_transition(transitions)
    if latest_content_transition is None:
        since = successful[-window:]
        latest_transition_text = "no effective content transition evidenced yet"
    else:
        since = [
            record
            for record in successful
            if record.loop >= latest_content_transition["loop"]
        ][-window:]
        latest_transition_text = (
            f"loop {latest_content_transition['loop']} "
            f"{latest_content_transition['action']} changed visible content"
        )

    recent_names = [record.action.name for record in since]
    side_cycles = _side_route_cycle_count(transitions, since)
    changed_tap_or_back = [
        item
        for item in transitions
        if item["loop"] in {record.loop for record in since}
        and item["changed"]
        and item["action"] in {"Tap", "Back"}
    ]

    if side_cycles >= 2:
        alignment = (
            "Recent behavior shows repeated entry/exit of a secondary surface after "
            "the latest evidenced content transition. This is evidence of repeating "
            "a local side-route cycle more than it is evidence of progressing a "
            "per-item cadence across new content items."
        )
    elif changed_tap_or_back and latest_content_transition is None:
        alignment = (
            "Recent behavior shows side-surface transitions, but no effective content "
            "transition has been evidenced yet for distributing the cadence across "
            "items."
        )
    else:
        alignment = (
            "Recent behavior does not show a repeated side-route cycle conflict with "
            "the cadence requirement."
        )

    return {
        "cadence_goal": cadence,
        "latest_effective_content_transition": latest_transition_text,
        "recent_actions_since_transition": recent_names,
        "side_route_visit_cycles_since_transition": side_cycles,
        "recent_route_pattern": _recent_route_pattern_text(recent_names),
        "cadence_alignment": alignment,
        "note": (
            "These are factual cadence-progress observations, not a forced next-action "
            "plan. Use screen understanding to decide autonomously."
        ),
    }


def _cadence_requirement_hint(task: str) -> str | None:
    """Detect periodic/side-surface requirements in the user goal."""
    compact = "".join(task.lower().split())
    cadence_markers = (
        "\u6bcf\u6761",  # 每条
        "\u6bcf\u4e2a",  # 每个
        "\u6bcf\u4e00",  # 每一
        "\u6bcf\u9694",  # 每隔
        "\u9694\u4e00",  # 隔一
        "\u9694\u4e24",  # 隔两
        "\u9694\u4e09",  # 隔三
        "every",
        "each",
        "peritem",
        "per-item",
    )
    side_surface_markers = (
        "\u8bc4\u8bba",  # 评论
        "\u8be6\u60c5",  # 详情
        "\u4e3b\u9875",  # 主页
        "\u5c55\u5f00",  # 展开
        "\u8fdb\u5165",  # 进入
        "\u6253\u5f00",  # 打开
        "comment",
        "detail",
        "profile",
        "open",
        "enter",
    )
    if not any(marker in compact for marker in cadence_markers):
        return None
    if not any(marker in compact for marker in side_surface_markers):
        return None
    return (
        "periodic side-surface cadence detected in the active/root goal "
        "(for example every item or every N items)."
    )


def _action_screen_transitions(
    records: list[LoopRecord],
    current_screenshot: Screenshot,
) -> list[dict[str, object]]:
    """Return successful action transitions with generic screen-change facts."""
    transitions: list[dict[str, object]] = []
    for index, record in enumerate(records):
        if not record.result.success:
            continue
        after = (
            records[index + 1].screenshot
            if index + 1 < len(records)
            else current_screenshot
        )
        change = compare_screenshots(record.screenshot, after)
        changed = change.mean_difference >= 0.08 or change.changed_ratio >= 0.12
        strong_change = change.mean_difference >= 0.18 or change.changed_ratio >= 0.35
        transitions.append(
            {
                "loop": record.loop,
                "action": record.action.name,
                "changed": changed,
                "strong_change": strong_change,
                "mean_difference": round(change.mean_difference, 4),
                "changed_ratio": round(change.changed_ratio, 4),
            }
        )
    return transitions


def _latest_effective_content_transition(
    transitions: list[dict[str, object]],
) -> dict[str, object] | None:
    """Find the latest visible content transition without app-specific logic."""
    for item in reversed(transitions):
        if item["action"] != "Swipe":
            continue
        if item["changed"] or item["strong_change"]:
            return item
    return None


def _side_route_cycle_count(
    transitions: list[dict[str, object]],
    recent_records: list[LoopRecord],
) -> int:
    """Count generic enter/exit cycles after the latest content transition."""
    recent_loops = {record.loop for record in recent_records}
    route_actions = [
        item
        for item in transitions
        if item["loop"] in recent_loops
        and item["changed"]
        and item["action"] in {"Tap", "Back"}
    ]
    if len(route_actions) < 2:
        return 0

    cycles = 0
    pending_entry = False
    for item in route_actions:
        action = str(item["action"])
        if action == "Tap" and not pending_entry:
            pending_entry = True
            continue
        if pending_entry and action in {"Tap", "Back"}:
            cycles += 1
            pending_entry = False
    return cycles


def _recent_route_pattern_text(action_names: list[str]) -> str:
    if not action_names:
        return "no recent successful actions in cadence window"
    compact = " -> ".join(action_names[-10:])
    return f"recent successful action route: {compact}"


def _instruction_progress_ledger(
    *,
    task: str,
    commands: dict[str, object],
    records: list[LoopRecord],
    current_screenshot: Screenshot,
) -> str:
    """Summarize generic multi-step instruction progress as durable facts."""
    instruction = _active_instruction_text_for_progress(task, commands)
    clauses = _split_sequence_instruction(instruction)
    if len(clauses) < 2:
        return "No explicit sequential instruction detected."

    lines = [
        "active_instruction:",
        _compact_text(instruction, limit=700),
        "sequence_clauses:",
    ]
    lines.extend(f"- {clause}" for clause in clauses[:5])

    successful = [record for record in records if record.result.success]
    if not successful:
        lines.extend(
            [
                "completed_step_evidence:",
                "- No successful actions have been executed for this instruction yet.",
                "pending_step_guidance:",
                f"- Start from the first clause: {clauses[0]}",
            ]
        )
        return "\n".join(lines)

    transitions = _action_screen_transitions(records, current_screenshot)
    recent_records = successful[-12:]
    recent_names = [record.action.name for record in recent_records]
    side_cycles = _side_route_cycle_count(transitions, recent_records)
    latest_content_transition = _latest_effective_content_transition(transitions)
    changed_side_actions = [
        item
        for item in transitions
        if item["loop"] in {record.loop for record in recent_records}
        and item["changed"]
        and item["action"] in {"Tap", "Back"}
    ]
    latest_side_loop = max(
        (int(item["loop"]) for item in changed_side_actions),
        default=0,
    )
    content_after_side = (
        latest_content_transition is not None
        and int(latest_content_transition["loop"]) > latest_side_loop
    )

    completed: list[str] = []
    if side_cycles >= 1:
        completed.append(
            "Recent actions evidence at least one temporary side-surface visit cycle "
            "(a changed Tap entry followed by a changed close/back-style return)."
        )
    elif changed_side_actions:
        completed.append(
            "Recent actions evidence entry into or exit from a secondary surface."
        )
    if latest_content_transition is not None:
        completed.append(
            "A content/main-route transition is evidenced by loop "
            f"{latest_content_transition['loop']} {latest_content_transition['action']}."
        )
    completed.append(_recent_route_pattern_text(recent_names))

    lines.append("completed_step_evidence:")
    lines.extend(f"- {item}" for item in completed)

    lines.append("pending_step_guidance:")
    if side_cycles >= 1 and not content_after_side:
        lines.append(
            "- A side-surface visit has already been evidenced. If the current screen "
            "has returned to the main route, continue with the later clause(s) rather "
            "than restarting the earlier side-surface entry."
        )
        lines.extend(f"- Remaining clause candidate: {clause}" for clause in clauses[1:4])
    elif side_cycles >= 1 and content_after_side:
        lines.append(
            "- Both a side-surface visit and a main content transition are evidenced; "
            "continue the active instruction from the current screenshot."
        )
    else:
        lines.append(
            "- Use the sequence clauses and current screenshot to decide which clause "
            "is first not yet evidenced."
        )
    lines.append(
        "- This ledger is factual progress memory, not a forced action plan."
    )
    return "\n".join(lines)


def _instruction_state_packet(
    *,
    state: InstructionState,
    task: str,
    commands: dict[str, object],
    records: list[LoopRecord],
    duration_budget_seconds: int | None,
) -> str:
    """Render runtime-owned sequential progress; never infer it from prose history."""
    lines = [
        "instruction_epoch: " + str(state.epoch),
        "source_instruction:",
        _compact_text(state.source_instruction, limit=700),
    ]
    if not state.steps:
        lines.extend(
            [
                "plan_status: missing",
                "Before the action, output one compact task-progress JSON block:",
                '<task_progress>{"plan":["setup step","first repeated step","next repeated step"],"mode":"cycle","cycle_start":2}</task_progress>',
                "cycle_start is a 1-based step number. Use mode=cycle only when the goal explicitly repeats ordered steps across changing content during a duration/count task; steps before cycle_start run only once.",
                "Include Back only when returning is an explicit user step or is required to reach the next explicit user step.",
            ]
        )
        return "\n".join(lines)

    completed = state.completed or set()
    plan_status = "active" if state.current_step < len(state.steps) else "completed"
    lines.extend(
        [
            f"plan_status: {plan_status}",
            f"plan_mode: {state.repeat_mode}",
            f"completed_cycles: {state.cycle_count}",
            f"termination: {state.termination}",
            (
                f"cycle_start: S{state.cycle_start + 1}"
                if state.repeat_mode == "cycle"
                else "cycle_start: none"
            ),
            "ordered_user_steps:",
        ]
    )
    for index, step in enumerate(state.steps):
        if index in completed:
            status = "completed"
        elif index == state.current_step:
            status = "ACTIVE"
        else:
            status = "pending"
        lines.append(f"- S{index + 1} [{status}]: {step}")
    lines.extend(
        [
            "Completed user steps are durable facts. Do not restart them unless a new live command creates a new instruction epoch.",
            "Navigation or recovery may be used to reach the active step, but the semantic action must serve the active step.",
        ]
    )
    if state.current_step < len(state.steps):
        lines.insert(-2, f"current_step: S{state.current_step + 1}")
    else:
        lines.insert(-2, "current_step: none; the explicit ordered user steps are complete.")
        lines.append(
            "Do not restart the completed sequence. Continue only remaining duration or broader task requirements from the current screen."
        )
    if state.pending_completion is not None:
        lines.append("verification: the runtime is checking a proposed step completion on the next screenshot.")
    if state.last_verification:
        lines.append("runtime_state: " + state.last_verification)
    if state.current_step < len(state.steps):
        lines.append(
            'When the current user step will be complete if this action visibly succeeds, include '
            '<task_progress>{"complete_current_step":true}</task_progress> before the final action. '
            "Do not mark a step complete merely because a navigation action was attempted."
        )
    return "\n".join(lines)


def _extract_instruction_plan(text: str) -> dict[str, object] | None:
    """Validate a model-authored setup/cycle plan without inferring task intent."""
    match = re.search(r"<task_progress>\s*(\{.*?\})\s*</task_progress>", text, re.DOTALL)
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    setup = _string_list(payload.get("setup"))
    cycle = _string_list(payload.get("cycle"))
    if not setup and not cycle:
        return None
    termination = payload.get("termination")
    if termination not in {"runtime_duration_budget", "semantic_completion"}:
        return None
    return {
        "steps": setup + cycle,
        "mode": "cycle" if cycle else "once",
        "cycle_start": len(setup) if cycle else None,
        "termination": termination,
    }


def _extract_task_progress_proposal(text: str) -> dict[str, object]:
    """Read optional compact model state metadata without affecting action parsing."""
    match = re.search(r"<task_progress>\s*(\{.*?\})\s*</task_progress>", text, re.DOTALL)
    if not match:
        return {}
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    proposal: dict[str, object] = {}
    if payload.get("complete_current_step") is True:
        proposal["complete_current_step"] = True
    return proposal


def _active_instruction_text_for_progress(
    task: str,
    commands: dict[str, object],
) -> str:
    """Pick the currently active instruction text for sequence progress."""
    once = _string_list(commands.get("once"))
    if once:
        return "；".join(once)
    latest = _optional_command_text(commands.get("latest"))
    if latest:
        constraints = _string_list(commands.get("constraints"))
        if constraints:
            return latest + "；" + "；".join(constraints)
        return latest
    items = _live_command_items(commands)
    if items:
        return "；".join(items)
    return task.strip()


def _split_sequence_instruction(text: str) -> list[str]:
    """Split obvious sequential instructions without app-specific parsing."""
    normalized = re.sub(r"\s+", " ", text.strip())
    if not normalized:
        return []
    normalized = re.sub(
        r"(?:\bthen\b|\bnext\b|\bafter(?:wards?)?\b)",
        "；",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(r"(再进行|再去|再把|然后|之后|随后|接着|最后|先|再)", "；", normalized)
    parts = [
        part.strip(" ，,。.;；:：")
        for part in re.split(r"[；;]+", normalized)
        if part.strip(" ，,。.;；:：")
    ]
    return parts


def _recent_action_facts(records: list[LoopRecord], limit: int = 6) -> str:
    """Summarize recent execution facts without preserving model speculation."""
    if not records:
        return "- No actions have been executed yet."

    lines: list[str] = []
    for record in records[-limit:]:
        result = "success" if record.result.success else "failed/rejected"
        lines.append(
            "- Loop {loop}: {action} -> {result}: {message}".format(
                loop=record.loop,
                action=_action_history_text(record.action),
                result=result,
                message=_compact_text(record.result.message, limit=180),
            )
        )
    return "\n".join(lines)


def _active_operation_policy_text(task: str) -> str:
    """Return task-appropriate active-operation guidance without app-specific controls."""
    if _duration_requires_target_posted_video(task):
        return (
            "Targeted-task policy:\n"
            "- The task names a target entity or content source. Before the requested "
            "target-owned content is reached, do not treat unrelated visible content "
            "as satisfying the task.\n"
            "- Optional active exploration is secondary to the target route. Choose "
            "visible actions that move toward the requested target content first.\n"
            "- Do not add social or transactional actions merely to be active when "
            "the user only asked to locate or consume target content.\n"
            "- Likes, follows, and public comments are allowed only when compatible "
            "with the active user goal and visible context; never use private messages, "
            "share-to-contact, purchase, recharge, membership, login, or authorization "
            "flows unless explicitly requested."
        )

    return (
        "Active operation policy:\n"
        "- These active-operation defaults apply only when the user did not give "
        "specific step-by-step instructions or a specific target route.\n"
        "- For vague timed goals such as browsing or operating an app for several "
        "minutes, do not solve the task with only Swipe and Wait.\n"
        "- Use the visible screen to choose safe app-native actions: briefly inspect "
        "secondary views or content details when compatible with the active goal, then "
        "return to the main task route before continuing.\n"
        "- If you opened a temporary secondary view for optional exploration, return "
        "to the main task route after brief useful observation.\n"
        "- Likes, follows, and public comments are allowed only when compatible with "
        "the active user goal and visible context; never use private messages, "
        "share-to-contact, purchase, recharge, membership, login, or authorization "
        "flows unless explicitly requested."
    )


def _compact_task_contract(task: str, limit: int = 2200) -> str:
    """Keep the durable task contract visible without bloating every turn."""
    markers = (
        "Global goal:",
        "Milestone task type:",
        "Milestone objective:",
        "Milestone budget:",
        "Milestone recovery:",
        "Autonomous execution contract:",
        "Instruction priority:",
        "Execution policy:",
    )
    lines = task.splitlines()
    selected: list[str] = []
    capture = 0
    for line in lines:
        if any(marker in line for marker in markers):
            capture = 8
        if capture > 0:
            selected.append(line)
            capture -= 1

    text = "\n".join(selected).strip() or task.strip()
    return _compact_text(text, limit=limit)


def _runtime_task_contract(task: str) -> str:
    """Create a durable English task contract that survives mojibake and trimming."""
    budget = _extract_duration_budget_seconds(task)
    lines: list[str] = []

    if _duration_requires_target_posted_video(task):
        duration_text = f"{budget} seconds" if budget else "the requested duration"
        lines.extend(
            [
                "- Mission type: targeted timed-content task.",
                "- Required order: locate the requested target entity or content source; enter its content surface; open or start consuming a concrete target-owned content item; then continue for the requested duration.",
                f"- Duration budget: {duration_text}; the timer starts only after the requested target-owned content is actually being consumed.",
                "- Discovery, navigation, listing, setup, preview, placeholder, and loading states do not count as timed content consumption.",
                "- If the current screen is still a discovery, navigation, or setup state, choose the next visible action that moves toward actual target-owned content consumption.",
            ]
        )
    elif budget:
        lines.extend(
            [
                "- Mission type: timed app/content operation.",
                f"- Duration budget: {budget} seconds.",
                "- Keep operating in the requested app/content until runtime reports the budget is complete.",
            ]
        )
    else:
        lines.append("- Mission type: single-goal phone automation task.")

    return "\n".join(lines)


def _trim_conversation(
    messages: list[dict[str, object]], *, keep_recent: int = 8
) -> list[dict[str, object]]:
    """Keep system, compact assistant facts, and the current user state packet."""
    if not messages:
        return messages

    # Keep only the newest screenshot. Older user state packets are snapshots and
    # are superseded by the current packet, so keeping them quickly bloats prompt.
    cleaned = [
        message if index == len(messages) - 1 else _remove_images_from_message(dict(message))
        for index, message in enumerate(messages)
    ]

    system: list[dict[str, object]] = []
    body = cleaned
    if cleaned and cleaned[0].get("role") == "system":
        system = [cleaned[0]]
        body = cleaned[1:]

    if not body:
        return system

    current: list[dict[str, object]] = []
    history = body
    if body[-1].get("role") == "user":
        current = [body[-1]]
        history = body[:-1]

    assistant_history = [
        message for message in history if message.get("role") == "assistant"
    ]
    if len(assistant_history) > keep_recent:
        assistant_history = assistant_history[-keep_recent:]

    return system + assistant_history + current


def _message_payload_stats(messages: list[dict[str, object]]) -> dict[str, int]:
    """Return cheap request-size diagnostics for provider empty-response triage."""
    text_chars = 0
    images = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            text_chars += len(content)
            continue
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    text_chars += len(text)
            elif item.get("type") == "image_url":
                images += 1
    return {
        "messages": len(messages),
        "text_chars": text_chars,
        "images": images,
    }


def _action_history_text(action: Action) -> str:
    """Store only the executed action in chat history, not model time guesses."""
    name = action.name
    params = action.params
    if name == "Finish":
        return 'finish(message="runtime will validate completion")'
    if name in {"Launch", "LaunchApp"}:
        app = params.get("app", "")
        return f'do(action="{name}", app={json.dumps(str(app), ensure_ascii=False)})'
    if name == "Tap":
        return f'do(action="Tap", element={params.get("element")})'
    if name == "Swipe":
        return (
            f'do(action="Swipe", start={params.get("start")}, '
            f'end={params.get("end")})'
        )
    if name in {"Type", "Type_Name"}:
        text = json.dumps(str(params.get("text", "")), ensure_ascii=False)
        return f'do(action="{name}", text={text})'
    if name == "Wait":
        seconds = params.get("seconds", params.get("duration", ""))
        return f'do(action="Wait", seconds={seconds})'
    if name in {"Long Press", "Double Tap"}:
        return f'do(action="{name}", element={params.get("element")})'
    if name in {"Take_over", "Interact"}:
        message = json.dumps(str(params.get("message", "")), ensure_ascii=False)
        return f'do(action="{name}", message={message})'
    return f'do(action="{name}")'


def _normalize_launch_target_package(
    action: Action, *, target_app_package: str | None
) -> Action:
    """Use the capture-selected package for target app launches when available."""
    if action.name not in {"Launch", "LaunchApp"}:
        return action
    if not target_app_package:
        return action
    app = action.params.get("app")
    if app == target_app_package:
        return action
    params = dict(action.params)
    params["app"] = target_app_package
    return Action(name=action.name, params=params)


def _cap_wait_action(action: Action, max_wait_seconds: float | None) -> Action:
    """Limit model-requested Wait duration without touching other actions."""
    if action.name != "Wait":
        return action
    params = dict(action.params)
    key = "seconds" if "seconds" in params else "duration"
    raw_seconds = params.get(key)
    if isinstance(raw_seconds, (list, tuple)) and len(raw_seconds) == 1:
        raw_seconds = raw_seconds[0]
        params[key] = raw_seconds

    if max_wait_seconds is None:
        return Action(name=action.name, params=params)
    try:
        max_seconds = float(max_wait_seconds)
    except (TypeError, ValueError):
        return Action(name=action.name, params=params)
    if max_seconds <= 0:
        return Action(name=action.name, params=params)

    try:
        seconds = float(raw_seconds)
    except (TypeError, ValueError):
        return Action(name=action.name, params=params)
    if seconds <= max_seconds:
        return Action(name=action.name, params=params)

    params[key] = max_seconds
    return Action(name=action.name, params=params)


def _remove_images_from_message(message: dict[str, object]) -> dict[str, object]:
    """Keep text history but remove old image payloads from a chat message."""
    content = message.get("content")
    if isinstance(content, list):
        message = dict(message)
        message["content"] = [
            item
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
    return message


def _safe_current_app(device_id: str | None) -> str:
    """Return current foreground package when available."""
    try:
        return get_current_app(device_id=device_id)
    except DeviceError:
        return "unknown"


def _current_app_allows_duration_start(task: str, device_id: str | None) -> bool:
    """Return whether the foreground app is plausible for starting duration time."""
    current_app = _safe_current_app(device_id)
    if not current_app or current_app == "unknown":
        return True

    if _is_non_target_foreground_package(current_app):
        return False

    target_package = _extract_target_package_from_task(task)
    if target_package and current_app != target_package:
        return False

    return True


def _duration_timer_wait_reason(
    *,
    task: str,
    records: list[LoopRecord],
    screenshot: Screenshot,
    device_id: str | None,
) -> str | None:
    """Return why the duration timer should not start on the current screen."""
    if _is_transient_or_low_information_screen(screenshot):
        return (
            "The current screen still looks like a loading/splash/low-information "
            "screen. Reach the actual requested content first."
        )

    if not _current_app_allows_duration_start(task=task, device_id=device_id):
        return (
            "The current foreground app is not yet the requested target app. "
            "Reach a real content page in the requested app first."
        )

    if _duration_requires_target_posted_video(task) and not _opened_target_video_content(
        records
    ):
        return (
            "This is a targeted timed-content task. The duration timer starts only "
            "after the requested target-owned content is actually being consumed. "
            "Current discovery, navigation, setup, preview, placeholder, or loading "
            "states do not count. Continue from the visible state toward a concrete "
            "target-owned content item."
        )

    return None


def _duration_requires_target_posted_video(task: str) -> bool:
    compact = "".join(task.lower().split())
    has_duration = _extract_duration_budget_seconds(task) is not None
    if not has_duration:
        return False
    has_target_lookup = _is_search_like_task(task) or any(
        marker in compact
        for marker in (
            "账号",
            "用户",
            "主页",
            "作者",
            "account",
            "profile",
            "user",
            "鎼滅储",
        )
    )
    has_posted_video = any(
        marker in compact
        for marker in (
            "发布的视频",
            "它发布",
            "他发布",
            "她发布",
            "作品",
            "刷它",
            "刷他",
            "刷她",
            "postedvideo",
            "postedvideos",
            "works",
        )
    )
    return has_target_lookup and has_posted_video


def _opened_target_video_content(records: list[LoopRecord]) -> bool:
    """Infer whether setup progressed far enough to start a targeted video timer."""
    saw_text_input = False
    saw_search_submit = False
    for record in records:
        if not record.result.success:
            continue
        if record.action.name in {"Type", "Type_Name"}:
            saw_text_input = True
            continue
        if saw_text_input and record.action.name == "Tap" and _is_top_right_tap(
            record.action
        ):
            saw_search_submit = True
            continue
        if saw_search_submit and record.action.name == "Tap" and _is_deep_content_tap(
            record.action
        ):
            return True
    return False


def _is_deep_content_tap(action: Action) -> bool:
    element = action.params.get("element")
    if not isinstance(element, (list, tuple)) or len(element) != 2:
        return False
    try:
        x = float(element[0])
        y = float(element[1])
    except (TypeError, ValueError):
        return False
    if _is_top_right_tap(action):
        return False
    return 80 <= x <= 920 and y >= 520


def _is_non_target_foreground_package(package: str) -> bool:
    """Reject launcher/system shells as duration browsing start states."""
    blocked_exact = {
        "com.google.android.apps.nexuslauncher",
        "com.android.launcher",
        "com.android.launcher3",
        "com.miui.home",
        "com.huawei.android.launcher",
        "com.oppo.launcher",
        "com.vivo.launcher",
        "com.android.systemui",
        "android",
    }
    if package in blocked_exact:
        return True
    blocked_prefixes = (
        "com.google.android.apps.nexuslauncher",
        "com.android.launcher",
        "com.sec.android.app.launcher",
    )
    return package.startswith(blocked_prefixes)


def _extract_target_package_from_task(task: str) -> str | None:
    """Infer the requested app package from known app aliases in the task."""
    for app_name, package in APP_PACKAGES.items():
        if app_name and app_name in task:
            return package
    package_match = re.search(r"\b[a-zA-Z][\w]*(?:\.[a-zA-Z][\w]*){2,}\b", task)
    if package_match:
        return package_match.group(0)
    return None


def _tap_bucket(action: Action) -> tuple[int, int] | None:
    element = action.params.get("element")
    if not isinstance(element, (list, tuple)) or len(element) != 2:
        return None
    try:
        x = float(element[0])
        y = float(element[1])
    except (TypeError, ValueError):
        return None
    return (round(x / 50), round(y / 50))


def _is_top_right_tap(action: Action) -> bool:
    element = action.params.get("element")
    if not isinstance(element, (list, tuple)) or len(element) != 2:
        return False
    try:
        x = float(element[0])
        y = float(element[1])
    except (TypeError, ValueError):
        return False
    return x >= 820 and y <= 140


def _is_search_like_task(task: str) -> bool:
    compact = "".join(task.lower().split())
    markers = (
        "搜索",
        "搜",
        "查找",
        "检索",
        "search",
        "find",
        "鎼滅储",
        "鎼",
    )
    return any(marker in compact for marker in markers)


def _duration_task_explicitly_prefers_swipe_only(task: str) -> bool:
    """Return True when the user specifically asked for swipe-only behavior."""
    compact = "".join(task.lower().split())
    markers = (
        "onlyswipe",
        "justswipe",
        "swipeonly",
        "onlyscroll",
        "scrollonly",
        "只上滑",
        "只滑动",
        "只刷视频",
    )
    return any(marker in compact for marker in markers)


def _has_explicit_user_operation_plan(task: str) -> bool:
    """Return True when the user supplied concrete steps/cadence/content."""
    compact = "".join(task.lower().split())
    markers = (
        "每隔",
        "每",
        "评论",
        "留言",
        "发送",
        "输入",
        "点赞",
        "关注",
        "不要",
        "禁止",
        "先",
        "然后",
        "再",
        "步骤",
        "第",
        "test-message",
        "every",
        "comment",
        "send",
        "type",
        "like",
        "follow",
        "do not",
        "don't",
        "step",
        "first",
        "then",
    )
    return any(marker in compact for marker in markers)


def _is_transient_model_error(exc: ModelError) -> bool:
    """Return whether a model error is worth retrying in the next loop."""
    text = str(exc).lower()
    transient_markers = (
        "empty response",
        "choices=none",
        "choices none",
        "completion_tokens=0",
        "could not read sdk model response content",
        "could not parse model response after retry",
        "timeout",
        "temporarily",
        "rate limit",
        "429",
        "500",
        "502",
        "503",
        "504",
        "connection",
    )
    hard_fail_markers = (
        "api key",
        "unauthorized",
        "401",
        "403",
        "model not found",
        "invalid_request",
    )
    if any(marker in text for marker in hard_fail_markers):
        return False
    return any(marker in text for marker in transient_markers)


def _fallback_action_during_model_outage(
    task: str,
    records: list[LoopRecord],
) -> Action | None:
    """Keep a duration task moving briefly when the model provider is flaky."""
    if _extract_duration_budget_seconds(task) is None:
        return None

    recent_actions = [record.action.name for record in records[-4:]]
    if recent_actions and all(name == "Swipe" for name in recent_actions):
        return Action(name="Wait", params={"seconds": 3})

    return Action(
        name="Swipe",
        params={
            "start": [500, 760],
            "end": [500, 260],
            "duration_ms": 500,
        },
    )


def _repeated_action_feedback(records: list[LoopRecord]) -> str | None:
    """Describe repeated recent behavior without choosing the next action."""
    if len(records) < 3:
        return None

    recent = [record for record in records[-4:] if record.result.success]
    if len(recent) < 3:
        return None

    names = [record.action.name for record in recent]
    if len(set(names[-3:])) != 1:
        return None

    repeated = names[-1]
    if repeated == "Swipe":
        return (
            "Recent successful behavior has repeated Swipe several times. Treat "
            "this as a factual behavior pattern and compare it with the current "
            "active user goal before deciding autonomously from the current screen."
        )
    if repeated == "Wait":
        return (
            "Recent successful behavior has repeated Wait several times. Treat "
            "this as a factual behavior pattern and compare it with the current "
            "active user goal before deciding autonomously from the current screen."
        )
    return None


def _maybe_finish_semantic_milestone(
    task: str,
    records: list[LoopRecord],
    current_screenshot: Screenshot,
) -> str | None:
    """Conservatively finish common semantic milestones after successful state changes."""
    if not records:
        return None

    milestone_id = _extract_current_milestone_id(task)
    if not milestone_id:
        return None

    duration_budget = _extract_duration_budget_seconds(task)
    if duration_budget is not None:
        return None

    last_record = records[-1]
    if not last_record.result.success:
        return None

    change = compare_screenshots(last_record.screenshot, current_screenshot)
    changed = change.mean_difference >= 0.08 or change.changed_ratio >= 0.12
    action_name = last_record.action.name

    if _is_feed_entry_milestone(milestone_id, task):
        if (
            action_name in {"LaunchApp", "Tap", "Back", "Wait"}
            and changed
            and not _is_transient_or_low_information_screen(current_screenshot)
        ):
            return "Milestone completed: reached the target feed or page."

    if _is_watch_swipe_cycle_milestone(milestone_id, task):
        required_cycles = _extract_required_cycle_count(task)
        wait_count = _count_successful_actions(records, "Wait")
        swipe_count = _count_effective_swipes(records, current_screenshot)
        if wait_count >= required_cycles and swipe_count >= required_cycles:
            return (
                f"Milestone completed: finished {required_cycles} watch-and-swipe cycles."
            )
        return None

    if _is_play_only_milestone(milestone_id) and action_name == "Wait":
        return "Milestone completed: waited for the requested playback duration."

    if milestone_id.startswith("swipe_") and action_name == "Swipe" and changed:
        return "Milestone completed: swipe changed the visible content."

    return None


def _extract_current_milestone_id(task: str) -> str | None:
    """Extract the current semantic milestone id from a plan_runner task prompt."""
    match = re.search(r"Current milestone:\s*\d+/\d+\s*\(([^)]+)\)", task)
    if not match:
        return None
    return match.group(1).strip()


def _has_plan_context(task: str) -> bool:
    """Return True when plan_runner supplied semantic plan context."""
    return "Semantic plan context" in task and "Current milestone:" in task


def _is_feed_entry_milestone(milestone_id: str, task: str = "") -> bool:
    """Return True for common app/feed entry milestone ids."""
    normalized = milestone_id.lower()
    known_ids = {
        "launch_and_land_on_feed",
        "enter_recommend_feed",
        "enter_douyin_feed",
    }
    if normalized in known_ids or ("enter" in normalized and "feed" in normalized):
        return True

    compact_task = "".join(_extract_current_milestone_text(task).lower().split())
    if _describes_watch_swipe_cycle(compact_task):
        return False

    has_entry = (
        "打开" in compact_task
        or "启动" in compact_task
        or "进入" in compact_task
        or "launch" in compact_task
        or "open" in compact_task
        or "navigate" in compact_task
        or "enter" in compact_task
    )
    has_feed = (
        "推荐流" in compact_task
        or "视频流" in compact_task
        or "推荐视频流" in compact_task
        or "feed" in compact_task
        or "video_feed" in compact_task
    )
    has_app = "抖音" in compact_task or "douyin" in compact_task or "tiktok" in compact_task
    return has_entry and has_feed and has_app


def _is_watch_swipe_cycle_milestone(milestone_id: str, task: str = "") -> bool:
    """Return True for watch/play plus swipe cycle milestone ids."""
    normalized = milestone_id.lower()
    id_matches = ("cycle" in normalized or "repeat" in normalized or "loop" in normalized) and (
        ("watch" in normalized or "play" in normalized) and "swipe" in normalized
    )
    if id_matches:
        return True

    compact_task = "".join(_extract_current_milestone_text(task).lower().split())
    return _describes_watch_swipe_cycle(compact_task)


def _describes_watch_swipe_cycle(compact_text: str) -> bool:
    """Return True when compact text describes repeated watch/play plus swipe."""
    return (
        ("滑动" in compact_text or "swipe" in compact_text)
        and (
            "播放" in compact_text
            or "观看" in compact_text
            or "watch" in compact_text
            or "play" in compact_text
        )
        and (
            "重复" in compact_text
            or "循环" in compact_text
            or "repeat" in compact_text
            or "cycle" in compact_text
        )
    )


def _is_confirmation_milestone(milestone_id: str, task: str = "") -> bool:
    """Return True for semantic state-confirmation milestones."""
    normalized = milestone_id.lower()
    if normalized.startswith(("confirm_", "verify_", "ensure_")):
        return True

    compact = "".join(_extract_current_milestone_text(task).lower().split())
    return (
        ("确认" in compact or "verify" in compact or "ensure" in compact)
        and ("播放" in compact or "playing" in compact or "playback" in compact)
        and not _describes_watch_swipe_cycle(compact)
    )


def _is_play_only_milestone(milestone_id: str) -> bool:
    """Return True for play/wait milestones that do not also require swiping."""
    normalized = milestone_id.lower()
    if "swipe" in normalized or "cycle" in normalized or "repeat" in normalized:
        return False
    return normalized.startswith("play_") or normalized.startswith("watch_")


def _extract_required_cycle_count(task: str) -> int:
    """Extract a conservative repeat count from the task text."""
    milestone_text = _extract_current_milestone_text(task)
    milestone_count = _extract_count_from_text(milestone_text)
    if milestone_count is not None:
        return milestone_count

    return _extract_count_from_text(task) or 1


def _extract_count_from_text(text: str) -> int | None:
    """Extract a repeat/cycle count from one text scope."""
    compact = "".join(text.strip().lower().split())
    patterns = (
        r"共完成\s*(\d+)\s*次",
        r"重复\s*(\d+)\s*次",
        r"完成\s*(\d+)\s*次完整",
        r"(\d+)\s*次完整的",
        r"连续\s*(\d+)\s*次",
        r"连续进行\s*(\d+)\s*次",
        r"(\d+)\s*个周期",
        r"(\d+)\s*轮",
        r"repeat\s*(\d+)",
        r"(\d+)\s*cycles?",
        r"(\d+)\s*times?",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return max(1, int(match.group(1)))

    chinese_digits = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    chinese_patterns = (
        r"重复([一二两三四五六七八九十])次",
        r"连续([一二两三四五六七八九十])次",
        r"([一二两三四五六七八九十])次完整",
        r"([一二两三四五六七八九十])个周期",
        r"([一二两三四五六七八九十])轮",
    )
    for pattern in chinese_patterns:
        match = re.search(pattern, compact)
        if match:
            return chinese_digits[match.group(1)]
    return None


def _count_successful_actions(records: list[LoopRecord], action_name: str) -> int:
    """Count successful executed actions in the current agent run."""
    return sum(
        1
        for record in records
        if record.action.name == action_name and record.result.success
    )


def _count_effective_swipes(
    records: list[LoopRecord],
    current_screenshot: Screenshot,
) -> int:
    """Count successful swipes that visibly changed the screen afterwards."""
    count = 0
    for index, record in enumerate(records):
        if record.action.name != "Swipe" or not record.result.success:
            continue

        after_screenshot = (
            records[index + 1].screenshot
            if index + 1 < len(records)
            else current_screenshot
        )
        change = compare_screenshots(record.screenshot, after_screenshot)
        if change.mean_difference >= 0.08 or change.changed_ratio >= 0.12:
            count += 1
    return count


def _validate_finish_action(
    task: str,
    records: list[LoopRecord],
    current_screenshot: Screenshot,
) -> str | None:
    """Accept model finish only when local semantic completion checks agree."""
    if not _has_plan_context(task):
        return records[-1].result.message if records else "Task finished."

    milestone_id = _extract_current_milestone_id(task)
    if not milestone_id:
        return None

    if _is_watch_swipe_cycle_milestone(milestone_id, task):
        required_cycles = _extract_required_cycle_count(task)
        wait_count = _count_successful_actions(records, "Wait")
        swipe_count = _count_effective_swipes(records, current_screenshot)
        if wait_count >= required_cycles and swipe_count >= required_cycles:
            return (
                f"Milestone completed: finished {required_cycles} watch-and-swipe cycles."
            )
        return None

    if _is_confirmation_milestone(milestone_id, task):
        return records[-1].result.message or "Milestone completed: confirmed state."

    semantic_message = _maybe_finish_semantic_milestone(
        task=task,
        records=records,
        current_screenshot=current_screenshot,
    )
    if semantic_message:
        return semantic_message

    if _is_feed_entry_milestone(milestone_id, task):
        return None

    if _is_play_only_milestone(milestone_id):
        wait_count = _count_successful_actions(records, "Wait")
        if wait_count > 0:
            return records[-1].result.message or "Milestone completed."
        return None

    return None


def _should_use_strict_completion_validation(task: str) -> bool:
    """Return whether local mechanical completion checks should override finish."""
    if _extract_duration_budget_seconds(task) is not None:
        return True

    milestone_id = _extract_current_milestone_id(task) or ""
    if _is_watch_swipe_cycle_milestone(milestone_id, task):
        return True

    compact = "".join(task.lower().split())
    strict_keywords = (
        "qoe",
        "刷视频",
        "刷抖音",
        "推荐流",
        "短视频",
        "视频流",
        "watch-and-swipe",
        "duration_browse",
        "fixed_count_browse",
    )
    return any(keyword in compact for keyword in strict_keywords)


def _extract_current_milestone_text(task: str) -> str:
    """Extract only the current milestone fields from a plan_runner task prompt."""
    start_match = re.search(r"Current milestone:\s*\d+/\d+\s*\([^)]+\)", task)
    if not start_match:
        return task

    end_match = re.search(r"\nMilestone risks:", task[start_match.end() :])
    if not end_match:
        return task[start_match.start() :]

    end_index = start_match.end() + end_match.start()
    return task[start_match.start() : end_index]


def _extract_duration_budget_seconds(task: str) -> int | None:
    """Extract a duration budget from a plan prompt or raw v4 user goal."""
    milestone_text = _extract_current_milestone_text(task)
    if "type: duration" in milestone_text:
        match = re.search(r"seconds:\s*(\d+)", milestone_text)
        if match:
            return max(1, int(match.group(1)))

    text = task.lower()
    compact = "".join(text.split())

    patterns = (
        (r"(\d+(?:\.\d+)?)\s*(?:minutes?|mins?|min)\b", 60),
        (r"(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|sec|s)\b", 1),
        (r"(\d+(?:\.\d+)?)\s*分钟", 60),
        (r"(\d+(?:\.\d+)?)\s*分", 60),
        (r"(\d+(?:\.\d+)?)\s*秒", 1),
    )
    for pattern, multiplier in patterns:
        match = re.search(pattern, text)
        if match:
            return max(1, int(float(match.group(1)) * multiplier))

    compact_patterns = (
        (r"(\d+(?:\.\d+)?)(?:minutes?|mins?|min)", 60),
        (r"(\d+(?:\.\d+)?)(?:seconds?|secs?|sec|s)", 1),
        (r"(\d+(?:\.\d+)?)分钟", 60),
        (r"(\d+(?:\.\d+)?)分", 60),
        (r"(\d+(?:\.\d+)?)秒", 1),
    )
    for pattern, multiplier in compact_patterns:
        match = re.search(pattern, compact)
        if match:
            return max(1, int(float(match.group(1)) * multiplier))

    chinese_number = _extract_chinese_duration_number(compact)
    if chinese_number is not None:
        if "分钟" in compact or "分" in compact:
            return max(1, chinese_number * 60)
        if "秒" in compact:
            return max(1, chinese_number)
    return None


def _extract_chinese_duration_number(text: str) -> int | None:
    """Parse small Chinese duration numbers such as 三五分钟 or 十分钟."""
    digits = {
        "零": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if "三五分钟" in text or "三五分" in text:
        return 5

    match = re.search(r"([一二两三四五六七八九])(?:分钟|分|秒)", text)
    if match:
        return digits[match.group(1)]

    match = re.search(r"十([一二两三四五六七八九])?(?:分钟|分|秒)", text)
    if match:
        return 10 + (digits.get(match.group(1), 0) if match.group(1) else 0)

    match = re.search(r"([一二两三四五六七八九])十([一二两三四五六七八九])?(?:分钟|分|秒)", text)
    if match:
        return digits[match.group(1)] * 10 + (
            digits.get(match.group(2), 0) if match.group(2) else 0
        )
    return None


def _is_transient_or_low_information_screen(screenshot: Screenshot) -> bool:
    """Return True for splash/loading/logo-like screens.

    This deliberately avoids OCR. It catches the common failure mode where an app
    launch splash page has a nearly uniform background plus a small centered logo.
    Rich feed/detail pages have much higher grayscale entropy and variance.
    """
    try:
        with Image.open(screenshot.path) as image:
            grayscale = image.convert("L").resize((96, 192), Image.Resampling.BILINEAR)
            stat = ImageStat.Stat(grayscale)
            histogram = grayscale.histogram()
    except Exception:
        return False

    total = sum(histogram)
    if total <= 0:
        return False

    entropy = 0.0
    for count in histogram:
        if count <= 0:
            continue
        probability = count / total
        entropy -= probability * math.log2(probability)

    stddev = float(stat.stddev[0]) if stat.stddev else 0.0
    return entropy < 2.0 and stddev < 28.0


def _fallback_action_after_parse_failure(
    task: str,
    records: list[LoopRecord],
    model_output: str,
) -> Action | None:
    """Use a narrow deterministic action when the model describes the obvious action."""
    if not _has_plan_context(task):
        return None

    if _extract_duration_budget_seconds(task) is not None:
        action = _fallback_duration_action_from_intent(model_output)
        if action is not None:
            return action

    milestone_id = _extract_current_milestone_id(task)
    if not milestone_id or not _is_feed_entry_milestone(milestone_id, task):
        return None

    already_launched = any(
        record.action.name == "LaunchApp" and record.result.success for record in records
    )
    if already_launched:
        return None

    combined_text = f"{task}\n{model_output}"
    lowered = combined_text.lower()
    if "抖音" in combined_text or "douyin" in lowered or "tiktok" in lowered:
        return Action(name="LaunchApp", params={"app": "抖音"})

    return None


def _fallback_duration_action_from_intent(model_output: str) -> Action | None:
    """Recover one safe duration-browsing action from a truncated model answer."""
    text = model_output.lower()
    compact = "".join(text.split())

    wants_swipe = any(
        marker in compact
        for marker in (
            "swipeup",
            "swipetothenext",
            "nextvideo",
            "nextitem",
            "continuewithanotherswipe",
            "letmecontinuewithanotherswipe",
        )
    ) or any(marker in model_output for marker in ("上滑", "下一个视频", "继续滑", "切换到下一个"))
    if wants_swipe:
        return Action(
            name="Swipe",
            params={
                "start": [500, 760],
                "end": [500, 260],
                "duration_ms": 500,
            },
        )

    wants_wait = any(
        marker in compact
        for marker in ("waitfor", "waitamoment", "observebriefly", "watchbriefly")
    ) or any(marker in model_output for marker in ("等待", "观察", "看一会", "停留"))
    if wants_wait:
        return Action(name="Wait", params={"seconds": 3})

    return None


def _compact_text(text: str, limit: int = 500) -> str:
    """Compact noisy model text before storing it in action metadata."""
    normalized = " ".join(text.strip().split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."
