"""Global configuration loading for MiniPilot."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RoleSettings:
    """Model settings for one runtime role."""

    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    image_max_side: int | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GlobalSettings:
    """Top-level configuration loaded from a JSON file."""

    executor: RoleSettings = field(default_factory=RoleSettings)
    executor_active_profile: str | None = None
    executor_profiles: dict[str, RoleSettings] = field(default_factory=dict)
    capture: dict[str, Any] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict)
    operation_style: dict[str, Any] = field(default_factory=dict)
    runs_dir: str | None = None


class SettingsError(RuntimeError):
    """Raised when a global configuration file is invalid."""


def load_global_settings(path: str | Path) -> GlobalSettings:
    """Load v4 executor and capture settings from a JSON file if it exists."""
    config_path = Path(path)
    if not config_path.exists():
        return GlobalSettings()

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SettingsError(
            f"Global config is not valid JSON: {config_path}"
        ) from exc

    if not isinstance(data, dict):
        raise SettingsError("Global config root must be a JSON object.")

    executor_data = data.get("executor")

    return GlobalSettings(
        executor=_parse_role_settings(executor_data, role_name="executor"),
        executor_active_profile=_parse_executor_active_profile(executor_data),
        executor_profiles=_parse_executor_profiles(executor_data),
        capture=_parse_optional_object(data.get("capture"), field_name="capture"),
        runtime=_parse_runtime(data.get("runtime")),
        operation_style=_parse_operation_style(data.get("operation_style")),
        runs_dir=_parse_optional_string(data.get("runs_dir"), field_name="runs_dir"),
    )


def _parse_optional_object(value: Any, field_name: str) -> dict[str, Any]:
    """Return an object value or an empty dict."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SettingsError(f"{field_name} must be a JSON object.")
    return value


def _parse_operation_style(value: Any) -> dict[str, Any]:
    """Parse human-editable, machine-readable operation style settings."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SettingsError("operation_style must be a JSON object.")

    parsed: dict[str, Any] = {}
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise SettingsError("operation_style.enabled must be a boolean.")
    parsed["enabled"] = enabled

    for key in ("persona", "freeform"):
        item = value.get(key)
        if item is not None and not isinstance(item, str):
            raise SettingsError(f"operation_style.{key} must be a string.")
        if item is not None:
            parsed[key] = item

    for key in ("principles", "priority_rules", "safety_boundaries"):
        item = value.get(key)
        if item is None:
            continue
        if not isinstance(item, list) or not all(isinstance(entry, str) for entry in item):
            raise SettingsError(f"operation_style.{key} must be a list of strings.")
        parsed[key] = item

    return parsed


def _parse_runtime(value: Any) -> dict[str, Any]:
    """Parse hidden runtime settings controlled from config."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SettingsError("runtime must be a JSON object.")

    parsed: dict[str, Any] = {}
    max_loops = value.get("max_loops")
    if max_loops is not None:
        if not isinstance(max_loops, int) or max_loops <= 0:
            raise SettingsError("runtime.max_loops must be a positive integer.")
        parsed["max_loops"] = max_loops
    return parsed


def _parse_role_settings(value: Any, role_name: str) -> RoleSettings:
    """Parse one role block from raw JSON."""
    if value is None:
        return RoleSettings()
    if not isinstance(value, dict):
        raise SettingsError(f"{role_name} config must be a JSON object.")

    extra_body = value.get("extra_body", {})
    if extra_body is None:
        extra_body = {}
    if not isinstance(extra_body, dict):
        raise SettingsError(f"{role_name}.extra_body must be a JSON object.")

    return RoleSettings(
        base_url=_parse_optional_string(value.get("base_url"), f"{role_name}.base_url"),
        model=_parse_optional_string(value.get("model"), f"{role_name}.model"),
        api_key=_parse_optional_string(value.get("api_key"), f"{role_name}.api_key"),
        temperature=_parse_optional_float(
            value.get("temperature"), f"{role_name}.temperature"
        ),
        max_tokens=_parse_optional_int(
            value.get("max_tokens"), f"{role_name}.max_tokens"
        ),
        image_max_side=_parse_optional_int(
            value.get("image_max_side"), f"{role_name}.image_max_side"
        ),
        extra_body=extra_body,
    )


def _parse_executor_active_profile(value: Any) -> str | None:
    """Parse executor.active_profile from the executor config block."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SettingsError("executor config must be a JSON object.")
    profile = value.get("active_profile", value.get("profile"))
    return _parse_optional_string(profile, "executor.active_profile")


def _parse_executor_profiles(value: Any) -> dict[str, RoleSettings]:
    """Parse executor.profiles into role settings by profile name."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SettingsError("executor config must be a JSON object.")

    raw_profiles = value.get("profiles", {})
    if raw_profiles is None:
        return {}
    if not isinstance(raw_profiles, dict):
        raise SettingsError("executor.profiles must be a JSON object.")

    profiles: dict[str, RoleSettings] = {}
    for name, profile_value in raw_profiles.items():
        if not isinstance(name, str) or not name.strip():
            raise SettingsError("executor.profiles keys must be non-empty strings.")
        profiles[name] = _parse_role_settings(
            profile_value,
            role_name=f"executor.profiles.{name}",
        )
    return profiles


def _parse_optional_string(value: Any, field_name: str) -> str | None:
    """Return a string value or None."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise SettingsError(f"{field_name} must be a string.")
    return value


def _parse_optional_float(value: Any, field_name: str) -> float | None:
    """Return a float value or None."""
    if value is None:
        return None
    if not isinstance(value, (int, float)):
        raise SettingsError(f"{field_name} must be a number.")
    return float(value)


def _parse_optional_int(value: Any, field_name: str) -> int | None:
    """Return an int value or None."""
    if value is None:
        return None
    if not isinstance(value, int):
        raise SettingsError(f"{field_name} must be an integer.")
    return value
