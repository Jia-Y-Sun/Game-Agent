"""Simple OpenAI-compatible model endpoint ping script.

By default this script reads mini_pilot.config.json and checks:

- executor.profiles.autoglm
- executor.profiles.seed2pro
- capture.postprocess_vlm

Run:

    python model_ping.py

The script is intentionally standalone so it can test a model provider before
MiniPilot starts a real phone task.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


CONFIG = {
    # Examples:
    # "https://api.deepseek.com"
    # "https://api-inference.modelscope.cn/v1"
    # "https://ark.cn-beijing.volces.com/api/v3"
    "base_url": "https://ark.cn-beijing.volces.com/api/v3",
    "model": "doubao-seed-2-0-pro-260215",
    "api_key": "YOUR_API_KEY",
    "timeout": 60,
    "max_tokens": 128,
    "temperature": 0.0,
}


@dataclass(frozen=True)
class PingTarget:
    """One model endpoint to ping."""

    name: str
    base_url: str
    model: str
    api_key: str
    timeout: float = 60
    max_tokens: int = 128
    temperature: float = 0.0
    extra_body: dict[str, Any] = field(default_factory=dict)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ping MiniPilot model endpoints from mini_pilot.config.json."
    )
    parser.add_argument(
        "--config",
        default="mini_pilot.config.json",
        help="Path to MiniPilot config JSON. Default: mini_pilot.config.json",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Ignore config JSON and ping the hard-coded CONFIG block only.",
    )
    parser.add_argument(
        "--target",
        action="append",
        help=(
            "Only ping target names containing this text. Can be repeated. "
            "Examples: --target autoglm --target seed2pro --target postprocess_vlm"
        ),
    )
    args = parser.parse_args()

    try:
        from openai import OpenAI
    except ImportError:
        print("ERROR: openai package is not installed.")
        print("Install dependencies first, for example: python -m pip install openai")
        return 2

    try:
        targets = [manual_target()] if args.manual else load_targets(args.config)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2

    if args.target:
        needles = [item.lower() for item in args.target]
        targets = [
            target for target in targets
            if any(needle in target.name.lower() for needle in needles)
        ]

    if not targets:
        print("ERROR: No model targets found to ping.")
        print("Check mini_pilot.config.json or use --manual.")
        return 2

    print("Model ping")
    print("=" * 50)
    print(f"Targets: {len(targets)}")

    failures = 0
    for index, target in enumerate(targets, start=1):
        ok = ping_target(OpenAI, target, index=index, total=len(targets))
        if not ok:
            failures += 1

    print("\nSummary")
    print("=" * 50)
    print(f"OK: {len(targets) - failures}")
    print(f"Failed: {failures}")
    return 0 if failures == 0 else 1


def ping_target(openai_class: Any, target: PingTarget, index: int, total: int) -> bool:
    """Ping one target and print a compact result."""
    if not target.base_url or not target.model or not target.api_key:
        print(f"\n[{index}/{total}] {target.name}")
        print("=" * 50)
        print("SKIPPED: missing base_url, model, or api_key")
        return False

    client = openai_class(
        base_url=target.base_url,
        api_key=target.api_key,
        timeout=target.timeout,
    )

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": "You are a concise test assistant. Reply with exactly: pong",
        },
        {
            "role": "user",
            "content": "ping",
        },
    ]

    print(f"\n[{index}/{total}] {target.name}")
    print("=" * 50)
    print(f"Base URL: {target.base_url}")
    print(f"Model: {target.model}")
    print(f"API key: {mask_secret(target.api_key)}")
    print("Sending request...")

    started_at = time.time()
    try:
        response = client.chat.completions.create(
            model=target.model,
            messages=messages,
            temperature=target.temperature,
            max_tokens=target.max_tokens,
            extra_body=target.extra_body,
            stream=False,
        )
    except Exception as exc:
        print("FAILED")
        print(format_exception(exc))
        return False

    elapsed = time.time() - started_at
    content = extract_content(response)

    print("SUCCESS")
    print(f"Elapsed: {elapsed:.2f}s")
    print(f"Output: {content!r}")
    print("\nRaw response preview:")
    print(raw_preview(response))
    return True


def load_targets(config_path: str | Path) -> list[PingTarget]:
    """Load executor profile targets from MiniPilot config."""
    path = Path(config_path)
    if not path.exists():
        raise ValueError(
            f"config file does not exist: {path}. Use --manual to test CONFIG only."
        )

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"config file is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError("config root must be a JSON object.")

    targets: list[PingTarget] = []

    executor = data.get("executor")
    if isinstance(executor, dict):
        profiles = executor.get("profiles")
        if isinstance(profiles, dict):
            for name, value in profiles.items():
                if isinstance(value, dict):
                    targets.append(role_target(f"executor.profiles.{name}", value))
        elif any(key in executor for key in ("base_url", "model", "api_key")):
            targets.append(role_target("executor", executor))

    capture = data.get("capture")
    if isinstance(capture, dict) and capture.get("postprocess_enable_vlm", True):
        targets.append(
            PingTarget(
                name="capture.postprocess_vlm",
                base_url=str(capture.get("postprocess_vlm_base_url") or "").strip(),
                model=str(capture.get("postprocess_vlm_model") or "").strip(),
                api_key=str(capture.get("postprocess_vlm_api_key") or "").strip(),
                timeout=float(capture.get("postprocess_vlm_timeout") or 60),
                max_tokens=128,
                temperature=0.0,
            )
        )

    return targets


def role_target(name: str, data: dict[str, Any]) -> PingTarget:
    """Build one ping target from a config object."""
    return PingTarget(
        name=name,
        base_url=str(data.get("base_url") or "").strip(),
        model=str(data.get("model") or "").strip(),
        api_key=str(data.get("api_key") or "").strip(),
        timeout=float(data.get("timeout") or CONFIG.get("timeout", 60)),
        max_tokens=int(data.get("max_tokens") or CONFIG.get("max_tokens", 128)),
        temperature=float(data.get("temperature") or CONFIG.get("temperature", 0.0)),
        extra_body=parse_extra_body(data.get("extra_body")),
    )


def parse_extra_body(value: Any) -> dict[str, Any]:
    """Return extra_body only when it is a JSON object."""
    if isinstance(value, dict):
        return value
    return {}


def manual_target() -> PingTarget:
    """Build a ping target from the hard-coded CONFIG block."""
    return PingTarget(
        name="manual CONFIG",
        base_url=str(CONFIG["base_url"]).strip(),
        model=str(CONFIG["model"]).strip(),
        api_key=str(CONFIG["api_key"]).strip(),
        timeout=float(CONFIG.get("timeout", 60)),
        max_tokens=int(CONFIG.get("max_tokens", 128)),
        temperature=float(CONFIG.get("temperature", 0.0)),
    )


def mask_secret(value: str) -> str:
    """Mask API keys in console output."""
    if not value:
        return "<empty>"
    if value == "PUT_YOUR_API_KEY_HERE":
        return "<placeholder>"
    if len(value) <= 8:
        return value[:1] + "***"
    return value[:4] + "***" + value[-4:]


def extract_content(response: Any) -> str:
    try:
        content = response.choices[0].message.content
    except Exception:
        return ""
    if isinstance(content, str):
        return content.strip()
    return str(content).strip()


def raw_preview(response: Any) -> str:
    try:
        if hasattr(response, "model_dump"):
            data = response.model_dump()
        else:
            data = response
        text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    except Exception:
        text = repr(response)
    if len(text) > 2000:
        return text[:2000] + "\n...<truncated>"
    return text


def format_exception(exc: Exception) -> str:
    parts = [f"{exc.__class__.__name__}: {exc}"]

    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        parts.append(f"status_code={status_code}")

    body = getattr(exc, "body", None)
    if body:
        parts.append(f"body={body}")

    response = getattr(exc, "response", None)
    if response is not None:
        text = getattr(response, "text", None)
        if text:
            parts.append(f"response={text[:1000]}")

    cause = exc.__cause__
    if cause is not None:
        parts.append(f"cause={cause.__class__.__name__}: {cause}")

    context = exc.__context__
    if context is not None and context is not cause:
        parts.append(f"context={context.__class__.__name__}: {context}")

    message = "\n".join(parts)
    hint = classify_error_hint(message)
    if hint:
        message += "\n\nHint: " + hint
    return message


def classify_error_hint(message: str) -> str:
    lower = message.lower()
    if "401" in lower or "unauthorized" in lower or "authentication" in lower:
        return "API key is missing, invalid, expired, or does not match this provider."
    if "402" in lower or "insufficient" in lower or "quota" in lower or "balance" in lower:
        return "Quota/balance may be insufficient. Check provider billing or token quota."
    if "403" in lower or "forbidden" in lower or "permission" in lower:
        return "The key may not have permission for this model or endpoint."
    if "404" in lower or "not found" in lower:
        return "Base URL or model name may be wrong."
    if "429" in lower or "rate limit" in lower:
        return "Rate limit hit. Retry later or reduce request frequency."
    if "connection" in lower or "timed out" in lower or "timeout" in lower:
        return "Network, proxy, DNS, or provider endpoint may be unreachable."
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
