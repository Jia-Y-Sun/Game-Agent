# External Game Driver Integration

## Scope

This directory connects the existing MiniPilot/Unicapture capture pipeline to
an external Jin Chan Chan game driver. The game driver owns match operations;
MiniPilot owns capture lifecycle, business timing, and post-processing.

The first supported environment is:

```text
LDPlayer 14
Android landscape 1920x1080
ADB 127.0.0.1:5555
Game package com.tencent.jkchess
```

## Run

From `minipilot-main`:

```powershell
& .\.venv\Scripts\python.exe -m integration.game_probe_orchestrator `
  --config .\mini_pilot.config.json `
  --adb "D:\leidian\LDPlayer14\adb.exe" `
  --device-id 127.0.0.1:5555 `
  --game-driver "E:\path\to\jinchanchan\auto_loop.py" `
  --game-directory "E:\path\to\jinchanchan" `
  --game-timeout 3600 `
  --capture-ceiling 3900 `
  --capture-app jcc `
  --capture-scene battle `
  --capture-pcap-mode standard
```

The orchestrator:

1. Starts Unicapture before the game driver.
2. Launches `com.tencent.jkchess/...ZGamePolicyActivity` unless
   `--skip-launch-game` is present.
3. Starts the external game driver with `--mode external`.
4. Reads `MATCH_READY` and writes `capture/business_timing.json`.
5. Reads `MATCH_FINISHED` and updates the marker with the real business end.
6. Stops Unicapture, which collects files and runs QoE post-processing.
7. Writes `manifest.json`.

Use `--skip-launch-game` when the game is already in the required lobby or
match state.

By default the orchestrator also starts a MiniPilot pre-game agent. It handles
login, announcements, popups, lobby navigation, matchmaking and match
acceptance. The external game driver remains in `external` mode and waits for
the match. When `MATCH_READY` arrives, the orchestrator writes a `/stop` live
command to the pre-game agent so it cannot interfere with match operations.

Use `--disable-pregame` when the game is already in a match or when only the
external match executor should be tested.

## Game Driver Contract

The driver command is constructed as:

```text
python <auto_loop.py> --mode external --device-id 127.0.0.1:5555 \
  --event-file <run_dir>\events\game_driver.jsonl \
  --stop-file <run_dir>\control\stop.game \
  --timeout 1200
```

The event file is the authoritative state channel. The capture side does not
parse stdout.

Required events:

```json
{"schema_version":1,"run_id":"run-1","seq":1,"ts":"2026-09-29T21:00:00+08:00","event":"SCRIPT_STARTED"}
{"schema_version":1,"run_id":"run-1","seq":2,"ts":"2026-09-29T21:00:01+08:00","event":"DEVICE_READY"}
{"schema_version":1,"run_id":"run-1","seq":3,"ts":"2026-09-29T21:00:02+08:00","event":"WAITING_MATCH_READY"}
{"schema_version":1,"run_id":"run-1","seq":4,"ts":"2026-09-29T21:00:03+08:00","event":"MATCH_READY"}
{"schema_version":1,"run_id":"run-1","seq":5,"ts":"2026-09-29T21:01:03+08:00","event":"MATCH_FINISHED","result":"unknown"}
{"schema_version":1,"run_id":"run-1","seq":6,"ts":"2026-09-29T21:01:04+08:00","event":"SCRIPT_STOPPED","reason":"match_finished"}
```

Rules:

- `seq` must be strictly increasing.
- `MATCH_READY` and `MATCH_FINISHED` must each appear at most once per run.
- `SCRIPT_STOPPED` must be the last event.
- `ERROR` must be followed by `SCRIPT_STOPPED`.
- The driver must stop game input after `MATCH_FINISHED`.
- `MATCH_FINISHED` must not click "play again" or the next-match button.
- A stop-file request should exit with code `3`.
- A timeout should emit `ERROR(stage=timeout)` and exit with code `4`.
- A natural settlement exit should use code `0`.

## Current Capture-side Status

Implemented:

- JSONL parser and strict sequence validation.
- Event file tailing across partial writes and appended records.
- External driver process lifecycle and stop-file support.
- `MATCH_READY` to business-start marker.
- `MATCH_FINISHED` to business-end marker and actual business duration.
- Capture stop and automatic QoE post-processing.
- `manifest.json` output.

Validated end to end:

- A real LDPlayer run with both Unicapture and the external game driver.
- Natural `MATCH_READY -> capture -> MATCH_FINISHED -> postprocess -> summary.json`.
- Real postprocess status after a live external match: `status: ok`.

Still pending:

- Optional result/rank enrichment from `MATCH_FINISHED`.

## Sequential Multi-Round Runs

Use the outer multi-round orchestrator to run several matches without restarting
the game between rounds:

```powershell
& .\.venv\Scripts\python.exe -m integration.multi_round_orchestrator `
  --rounds 2 `
  --round-delay 10 `
  --session-id my-session `
  --config .\mini_pilot.config.json `
  --adb "D:\leidian\LDPlayer14\adb.exe" `
  --device-id 127.0.0.1:5555 `
  --game-python "E:\path\to\Game-Agent\.venv\Scripts\python.exe" `
  --game-driver "E:\path\to\jinchanchan\auto_loop.py" `
  --game-directory "E:\path\to\jinchanchan"
```

Round 1 uses the normal cold-start pre-game goal. Later rounds run with
`--skip-launch-game` and use the post-game goal to return from the settlement
page to the lobby, select standard ranked, and start the next match. A failed
round can be retried with the recovery goal.
