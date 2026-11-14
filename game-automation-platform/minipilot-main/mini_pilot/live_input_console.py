"""Separate console for MiniPilot live corrections."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from mini_pilot.text_repair import recover_mojibake


def main() -> int:
    parser = argparse.ArgumentParser(description="MiniPilot live input console.")
    parser.add_argument("queue_path", help="JSONL command queue path.")
    args = parser.parse_args()

    queue_path = Path(args.queue_path)
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.touch(exist_ok=True)

    print("MiniPilot live input console")
    print("=" * 50)
    print("Commands:")
    print("  plain text     replace latest live mission, same as /send <text>")
    print("  /send <text>   replace latest live mission")
    print("  /also <text>   add compatible persistent constraint")
    print("  /keep <text>   add compatible persistent constraint")
    print("  /once <text>   correction for next loop only")
    print("  /clear         clear live mission and constraints")
    print("  /stop [text]   stop at the next loop checkpoint")
    print("  /exit          close this input console")
    print()
    print("Plain text is accepted in this separate input console.")
    print()

    while True:
        try:
            line = input("MiniPilot live> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break

        text = recover_mojibake(line.strip())
        if not text:
            continue
        if text.lower() == "/exit":
            break

        command_text = _normalize_console_command(text)
        _append_command(queue_path, command_text)
        if command_text.lower().startswith("/stop"):
            print("Stop command queued.")
        elif _is_supported_command(command_text):
            print("Command queued.")
        else:
            print("Ignored by runner: unsupported command.")

    return 0


def _append_command(queue_path: Path, text: str) -> None:
    payload = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "text": text,
    }
    with queue_path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _is_supported_command(text: str) -> bool:
    lowered = text.lower()
    return (
        lowered.startswith("/send ")
        or lowered.startswith("/also ")
        or lowered.startswith("/keep ")
        or lowered.startswith("/once ")
        or lowered == "/clear"
        or lowered.startswith("/stop")
    )


def _normalize_console_command(text: str) -> str:
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
    return f"/send {text}"


if __name__ == "__main__":
    raise SystemExit(main())
