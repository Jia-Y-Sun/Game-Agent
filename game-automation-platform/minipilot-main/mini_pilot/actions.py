"""Action parsing and execution for MiniPilot.

The action layer translates model text into concrete device operations. It sits
between the model layer and the low-level ADB device layer.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Any

from mini_pilot import device
from mini_pilot.text_repair import recover_mojibake


SUPPORTED_ACTIONS = {
    "Tap",
    "Swipe",
    "Type",
    "Type_Name",
    "Back",
    "Home",
    "Enter",
    "Wait",
    "Launch",
    "LaunchApp",
    "Long Press",
    "Double Tap",
    "Take_over",
    "Interact",
    "Finish",
}


@dataclass(frozen=True)
class Action:
    """A parsed model action."""

    name: str
    params: dict[str, Any]


@dataclass(frozen=True)
class ActionResult:
    """Result after executing an action."""

    success: bool
    finished: bool = False
    message: str = ""


class ActionError(RuntimeError):
    """Raised when an action cannot be parsed or executed."""


def parse_action(text: str) -> Action:
    """Parse model output into an Action.

    Supported examples:
        do(action="Tap", element=[0.5, 0.6])
        do(action="Swipe", start=[0.5, 0.8], end=[0.5, 0.2])
        do(action="Type", text="hello")
        do(action="Launch", app="微信")
        do(action="LaunchApp", app="微信")
        do(action="Type_Name", text="张三")
        do(action="Long Press", element=[500, 500])
        do(action="Double Tap", element=[500, 500])
        do(action="Take_over", message="需要用户登录")
        do(action="Interact", message="存在多个候选联系人")
        do(action="Back")
        do(action="Wait", seconds=1)
        finish(message="done")
    """
    expression = _normalize_action_expression(_extract_action_expression(text))

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        fallback_finish = _parse_finish_fallback(expression)
        if fallback_finish is not None:
            return fallback_finish
        raise ActionError(f"Invalid action syntax: {expression}") from exc

    call = tree.body
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
        raise ActionError(f"Expected an action function call: {expression}")

    function_name = call.func.id
    kwargs = _literal_keywords(call)

    if function_name == "finish":
        return Action(name="Finish", params=kwargs)

    if function_name != "do":
        raise ActionError(f"Unknown action function: {function_name}")

    action_name = kwargs.pop("action", None)
    if not isinstance(action_name, str):
        raise ActionError("Action call must include action=\"...\".")
    action_name = recover_mojibake(action_name)

    if action_name not in SUPPORTED_ACTIONS:
        raise ActionError(f"Unsupported action: {action_name}")

    kwargs = _repair_text_params(kwargs)
    return Action(name=action_name, params=kwargs)


def _repair_text_params(params: dict[str, Any]) -> dict[str, Any]:
    repaired: dict[str, Any] = {}
    for key, value in params.items():
        if isinstance(value, str):
            repaired[key] = recover_mojibake(value)
        else:
            repaired[key] = value
    return repaired


def _parse_finish_fallback(expression: str) -> Action | None:
    """Parse finish(...) leniently when AST parsing fails on a long message literal."""
    stripped = expression.strip()
    if not stripped.startswith("finish(") or not stripped.endswith(")"):
        return None

    inner = stripped[len("finish(") : -1].strip()
    if not inner.startswith("message="):
        return None

    value = inner[len("message=") :].strip()
    if len(value) < 2:
        return None

    quote = value[0]
    if quote not in {"'", '"'}:
        return None

    # Keep the parser permissive for finish(message="...") so a correct semantic
    # completion is not lost just because the model included extra quotes or line
    # breaks inside the explanatory text.
    message = value[1:-1] if value.endswith(quote) else value[1:]
    message = (
        message.replace("\\n", "\n")
        .replace("\\r", "\r")
        .replace('\\"', '"')
        .replace("\\'", "'")
    ).strip()
    if not message:
        message = "Task finished."

    return Action(name="Finish", params={"message": message})


def execute_action(
    action: Action,
    screen_width: int,
    screen_height: int,
    device_id: str | None = None,
) -> ActionResult:
    """Execute a parsed Action using the device layer."""
    try:
        if action.name == "Tap":
            x, y = _relative_point_to_absolute(
                action.params.get("element"),
                screen_width,
                screen_height,
                field_name="element",
            )
            device.tap(x, y, device_id=device_id)
            return ActionResult(success=True, message=f"Tapped at ({x}, {y}).")

        if action.name == "Swipe":
            start_x, start_y = _relative_point_to_absolute(
                action.params.get("start"),
                screen_width,
                screen_height,
                field_name="start",
            )
            end_x, end_y = _relative_point_to_absolute(
                action.params.get("end"),
                screen_width,
                screen_height,
                field_name="end",
            )
            duration_ms = int(action.params.get("duration_ms", 500))
            device.swipe(
                start_x,
                start_y,
                end_x,
                end_y,
                duration_ms=duration_ms,
                device_id=device_id,
            )
            return ActionResult(
                success=True,
                message=f"Swiped from ({start_x}, {start_y}) to ({end_x}, {end_y}).",
            )

        if action.name in {"Type", "Type_Name"}:
            text = action.params.get("text")
            if not isinstance(text, str):
                raise ActionError(f"{action.name} action requires text=\"...\".")
            input_method = device.type_text(text, device_id=device_id)
            return ActionResult(
                success=True,
                message=f"Typed {len(text)} characters via {input_method}.",
            )

        if action.name == "Back":
            device.back(device_id=device_id)
            return ActionResult(success=True, message="Pressed Back.")

        if action.name == "Home":
            device.home(device_id=device_id)
            return ActionResult(success=True, message="Pressed Home.")

        if action.name == "Enter":
            device.enter(device_id=device_id)
            return ActionResult(success=True, message="Pressed Enter.")

        if action.name == "Wait":
            seconds = _parse_wait_seconds(
                action.params.get("seconds", action.params.get("duration"))
            )
            device.wait(seconds)
            return ActionResult(success=True, message=f"Waited {seconds:g} seconds.")

        if action.name in {"Launch", "LaunchApp"}:
            app_name = action.params.get("app")
            if not isinstance(app_name, str):
                raise ActionError(f"{action.name} action requires app=\"...\".")
            device.launch_app(app_name, device_id=device_id)
            return ActionResult(success=True, message=f"Launched app: {app_name}.")

        if action.name == "Long Press":
            x, y = _relative_point_to_absolute(
                action.params.get("element"),
                screen_width,
                screen_height,
                field_name="element",
            )
            duration_ms = int(action.params.get("duration_ms", 3000))
            device.long_press(x, y, duration_ms=duration_ms, device_id=device_id)
            return ActionResult(success=True, message=f"Long pressed at ({x}, {y}).")

        if action.name == "Double Tap":
            x, y = _relative_point_to_absolute(
                action.params.get("element"),
                screen_width,
                screen_height,
                field_name="element",
            )
            device.double_tap(x, y, device_id=device_id)
            return ActionResult(success=True, message=f"Double tapped at ({x}, {y}).")

        if action.name == "Take_over":
            message = action.params.get("message", "Manual takeover required.")
            if not isinstance(message, str):
                message = str(message)
            return ActionResult(success=False, finished=True, message=message)

        if action.name == "Interact":
            message = action.params.get("message", "User interaction required.")
            if not isinstance(message, str):
                message = str(message)
            return ActionResult(success=False, finished=True, message=message)

        if action.name == "Finish":
            message = action.params.get("message", "Task finished.")
            if not isinstance(message, str):
                message = str(message)
            return ActionResult(success=True, finished=True, message=message)

    except device.DeviceError as exc:
        raise ActionError(f"Device operation failed: {exc}") from exc

    raise ActionError(f"Unsupported action: {action.name}")


def _parse_wait_seconds(value: Any) -> float:
    """Parse and bound Wait action duration."""
    if isinstance(value, str):
        stripped = value.strip().lower()
        for suffix in ("seconds", "second", "secs", "sec", "s", "秒"):
            if stripped.endswith(suffix):
                stripped = stripped[: -len(suffix)].strip()
                break
        try:
            value = float(stripped)
        except ValueError as exc:
            raise ActionError("Wait action requires seconds=number or duration=\"N seconds\".") from exc

    if not isinstance(value, (int, float)):
        raise ActionError("Wait action requires seconds=number or duration=\"N seconds\".")

    seconds = float(value)
    if seconds < 0:
        raise ActionError("Wait seconds must be non-negative.")
    if seconds > 300:
        raise ActionError("Wait seconds must be <= 300.")
    return seconds


def _extract_action_expression(text: str) -> str:
    """Extract one supported action call from model text.

    The parser is deliberately redundant but conservative:
    1. Prefer an explicit <answer>...</answer> block.
    2. Prefer a fenced-code action or a line-aligned final action.
    3. As a last resort, accept the last action only when earlier calls look
       like quoted/history text rather than a multi-action instruction.

    Ambiguous multiple current actions are rejected rather than executed.
    """
    stripped = text.strip()

    answer_text = _extract_answer_block(stripped)
    if answer_text is not None:
        answer_matches = _find_action_matches(answer_text)
        if len(answer_matches) == 1:
            return answer_matches[0].expression
        if len(answer_matches) > 1:
            final = _select_final_action(answer_text, answer_matches, allow_history=True)
            if final is not None:
                return final.expression
            raise ActionError(
                "Answer block contains multiple ambiguous action calls. Output exactly one."
            )
        raise ActionError("Answer block did not contain a do(...) or finish(...) action.")

    matches = _find_action_matches(stripped)
    if not matches:
        raise ActionError(f"No action call found in model output: {text}")

    final = _select_final_action(stripped, matches, allow_history=True)
    if final is not None:
        return final.expression

    raise ActionError(
        "Model output contains multiple ambiguous action calls. Output exactly one final action."
    )


@dataclass(frozen=True)
class _ActionMatch:
    """A candidate action expression found in model text."""

    start: int
    end: int
    expression: str
    line_aligned: bool
    fenced: bool


def _extract_answer_block(text: str) -> str | None:
    """Return the last explicit answer block when present."""
    matches = list(
        re.finditer(r"<answer\b[^>]*>(.*?)</answer>", text, flags=re.IGNORECASE | re.DOTALL)
    )
    if matches:
        return matches[-1].group(1).strip()

    lower = text.lower()
    marker = "<answer>"
    index = lower.rfind(marker)
    if index == -1:
        return None
    return text[index + len(marker) :].strip()


def _find_action_matches(text: str) -> list[_ActionMatch]:
    """Find all syntactically balanced action calls in text."""
    matches: list[_ActionMatch] = []
    for prefix in ("do(", "finish("):
        start = 0
        while start < len(text):
            start = text.find(prefix, start)
            if start == -1:
                break

            end = _find_matching_parenthesis(text, start + len(prefix) - 1)
            if end != -1:
                line_start = text.rfind("\n", 0, start) + 1
                starts_line = text[line_start:start].strip().strip("`") == ""
                matches.append(
                    _ActionMatch(
                        start=start,
                        end=end + 1,
                        expression=text[start : end + 1],
                        line_aligned=starts_line,
                        fenced=_is_inside_fenced_code(text, start),
                    )
                )
                start = end + 1
            else:
                start += len(prefix)
    return sorted(matches, key=lambda item: item.start)


def _select_final_action(
    text: str,
    matches: list[_ActionMatch],
    *,
    allow_history: bool,
) -> _ActionMatch | None:
    """Select the safest final action candidate, or None when ambiguous."""
    if len(matches) == 1:
        return matches[0]

    fenced = [match for match in matches if match.fenced]
    if len(fenced) == 1:
        return fenced[0]
    if len(fenced) > 1:
        return fenced[-1] if _previous_matches_look_like_history(text, fenced[:-1]) else None

    line_aligned = [match for match in matches if match.line_aligned]
    if len(line_aligned) == 1:
        return line_aligned[0]
    if len(line_aligned) > 1:
        last = line_aligned[-1]
        if allow_history and _previous_matches_look_like_history(text, line_aligned[:-1]):
            return last
        return None

    last = matches[-1]
    if allow_history and _previous_matches_look_like_history(text, matches[:-1]):
        return last

    return None


def _previous_matches_look_like_history(text: str, matches: list[_ActionMatch]) -> bool:
    """Return True when earlier action calls are likely quoted history/examples."""
    if not matches:
        return True

    history_markers = (
        "previous",
        "history",
        "last",
        "before",
        "example",
        "示例",
        "历史",
        "上一",
        "上一步",
        "之前",
        "刚才",
        "模型输出",
        "action result",
        "model output",
    )
    for match in matches:
        prefix = text[max(0, match.start - 120) : match.start].lower()
        line_start = text.rfind("\n", 0, match.start) + 1
        same_line_prefix = text[line_start : match.start].strip().lower()
        if (
            any(marker in prefix for marker in history_markers)
            or same_line_prefix.startswith((">", "-", "*", "#", "//"))
            or "```" in prefix[-20:]
        ):
            continue
        return False
    return True


def _is_inside_fenced_code(text: str, index: int) -> bool:
    """Return whether index is inside a Markdown fenced code block."""
    before = text[:index]
    return before.count("```") % 2 == 1


def _find_matching_parenthesis(text: str, open_index: int) -> int:
    """Find the matching closing parenthesis for text[open_index]."""
    depth = 0
    in_string: str | None = None
    escape_next = False

    for index in range(open_index, len(text)):
        char = text[index]

        if escape_next:
            escape_next = False
            continue

        if in_string:
            if char == "\\":
                escape_next = True
            elif char == in_string:
                in_string = None
            continue

        if char in {"'", '"'}:
            in_string = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index

    return -1


def _literal_keywords(call: ast.Call) -> dict[str, Any]:
    """Read keyword arguments from an AST call using literal values only."""
    kwargs: dict[str, Any] = {}
    for keyword in call.keywords:
        if keyword.arg is None:
            raise ActionError("Action calls do not support **kwargs.")

        try:
            kwargs[keyword.arg] = ast.literal_eval(keyword.value)
        except ValueError as exc:
            raise ActionError(f"Action argument must be a literal: {keyword.arg}") from exc

    return kwargs


def _normalize_action_expression(expression: str) -> str:
    """Normalize a model action expression before AST parsing."""
    chars: list[str] = []
    in_string: str | None = None
    escape_next = False

    for char in expression:
        if escape_next:
            chars.append(char)
            escape_next = False
            continue

        if in_string:
            if char == "\\":
                chars.append(char)
                escape_next = True
                continue
            if char == in_string:
                in_string = None
                chars.append(char)
                continue
            if char in {"\r", "\n"}:
                chars.append("\\n")
                continue
            chars.append(char)
            continue

        if char in {"'", '"'}:
            in_string = char

        chars.append(char)

    return "".join(chars)


def _relative_point_to_absolute(
    point: Any,
    screen_width: int,
    screen_height: int,
    field_name: str,
) -> tuple[int, int]:
    """Convert a relative [x, y] point to absolute screen pixels."""
    if not isinstance(point, (list, tuple)) or len(point) != 2:
        raise ActionError(f"{field_name} must be a two-item list like [500, 500].")

    rel_x, rel_y = point
    if not isinstance(rel_x, (int, float)) or not isinstance(rel_y, (int, float)):
        raise ActionError(f"{field_name} coordinates must be numbers.")

    if 0 <= rel_x <= 1 and 0 <= rel_y <= 1:
        return int(rel_x * screen_width), int(rel_y * screen_height)

    if 0 <= rel_x <= 1000 and 0 <= rel_y <= 1000:
        return int(rel_x / 1000 * screen_width), int(rel_y / 1000 * screen_height)

    raise ActionError(f"{field_name} coordinates must be between 0 and 1000.")
