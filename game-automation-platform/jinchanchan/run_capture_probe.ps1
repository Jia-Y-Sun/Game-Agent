param(
    [string]$RunId = "",
    [int]$Rounds = 1,
    [int]$GameTimeout = 3600,
    [int]$MatchReadyTimeout = 900,
    [int]$CaptureCeiling = 3900,
    [int]$PostprocessOcrEvery = 10,
    [string]$SessionId = "",
    [string]$DeviceId = "127.0.0.1:5555",
    [string]$GamePython = "",
    [string]$PregameGoal = "",
    [switch]$DisablePostprocess,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding

$JccDir = $PSScriptRoot
$RepoRoot = (Resolve-Path (Join-Path $JccDir "..")).Path
$CaptureRoot = if ($env:MINI_PILOT_ROOT) {
    (Resolve-Path -LiteralPath $env:MINI_PILOT_ROOT).Path
} else {
    Join-Path $RepoRoot "minipilot-main"
}
$CapturePython = Join-Path $CaptureRoot ".venv\Scripts\python.exe"
if (-not $GamePython) {
    # 本地布局: 仓库根直接是 jinchanchan/wzry/scheduler; 优先仓库根 venv, 否则用 PATH 里的 python
    $repoVenv = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    $GamePython = if (Test-Path -LiteralPath $repoVenv) { $repoVenv } else { "python" }
}
$Adb = if ($env:MINI_PILOT_ADB_BIN) {
    $env:MINI_PILOT_ADB_BIN
} else {
    $adbCommand = Get-Command adb.exe -ErrorAction SilentlyContinue
    if (-not $adbCommand -and -not $DryRun) {
        throw "Set MINI_PILOT_ADB_BIN or add adb.exe to PATH."
    }
    if ($adbCommand) { $adbCommand.Source } else { "adb.exe" }
}
$Config = Join-Path $CaptureRoot "mini_pilot.config.json"

if (-not $DryRun) {
foreach ($required in @($CapturePython, $GamePython, $Adb, $Config)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Missing required file: $required"
    }
}

if ($DeviceId -match ':') { & $Adb connect $DeviceId 2>$null | Out-Null }
$deviceReady = $false
for ($attempt = 1; $attempt -le 4; $attempt++) {
    $devices = & $Adb devices 2>$null
    if ($devices -match ([regex]::Escape($DeviceId) + "\s+device")) {
        $deviceReady = $true
        break
    }
    Start-Sleep -Seconds 2
}
if (-not $deviceReady) {
    throw "ADB device $DeviceId is not ready. Start LDPlayer and retry."
}
}

$module = if ($Rounds -gt 1) {
    "integration.multi_round_orchestrator"
} else {
    "integration.game_probe_orchestrator"
}

$arguments = @(
    "-m", $module,
    "--config", $Config,
    "--runs-dir", (Join-Path $CaptureRoot "runs"),
    "--adb", $Adb,
    "--device-id", $DeviceId,
    "--game-id", "jcc",
    "--game-python", $GamePython,
    "--game-driver", (Join-Path $JccDir "auto_loop.py"),
    "--game-directory", $JccDir,
    "--game-timeout", "$GameTimeout",
    "--match-ready-timeout", "$MatchReadyTimeout",
    "--capture-ceiling", "$CaptureCeiling",
    "--capture-app", "jcc",
    "--capture-scene", "battle",
    "--capture-pcap-mode", "standard",
    "--capture-skip-pcapdroid-preflight",
    "--postprocess-ocr-every", "$PostprocessOcrEvery",
    "--game-package", "com.tencent.jkchess",
    "--game-launch-activity", "com.tencent.gcloud.msdk.core.policy.ZGamePolicyActivity"
)
if ($PregameGoal) { $arguments += @("--pregame-goal", $PregameGoal) }

if ($Rounds -gt 1) {
    $arguments += @("--rounds", "$Rounds")
    if ($SessionId) {
        $arguments += @("--session-id", $SessionId)
    }
} elseif ($RunId) {
    $arguments += @("--run-id", $RunId)
}

if ($DisablePostprocess) {
    $arguments += "--capture-disable-postprocess"
}

if ($DryRun) {
    Write-Output ($CapturePython + " " + ($arguments -join " "))
    return
}

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
Push-Location $CaptureRoot
try {
    & $CapturePython @arguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
