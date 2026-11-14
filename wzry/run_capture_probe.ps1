param(
    [string]$RunId = "",
    [int]$GameTimeout = 3600,
    [int]$MatchReadyTimeout = 900,
    [int]$CaptureCeiling = 3900,
    [int]$PostprocessOcrEvery = 10,
    [int]$Rounds = 1,
    [string]$SessionId = "",
    [string]$DeviceId = "emulator-5554",
    [string]$GamePython = "",
    [string]$ComputeDevice = "cuda:0",
    [string]$ModelPath = "src/wzry_ai_bc.pt",
    [switch]$SkipPcapdroidPreflight,
    [string]$PregameGoal = "",
    [switch]$DisablePostprocess,
    [switch]$DryRun
)

if ($PSVersionTable.PSVersion.Major -lt 7) {
    $pwsh = Get-Command pwsh.exe -ErrorAction SilentlyContinue
    if ($pwsh) {
        $forward = @{}
        foreach ($key in $PSBoundParameters.Keys) {
            $forward[$key] = $PSBoundParameters[$key]
        }
        & $pwsh.Source -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath @forward
        exit $LASTEXITCODE
    }
}

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$WzryDir = $PSScriptRoot
$RepoRoot = (Resolve-Path (Join-Path $WzryDir "..")).Path
$CaptureRoot = if ($env:MINI_PILOT_ROOT) {
    (Resolve-Path -LiteralPath $env:MINI_PILOT_ROOT).Path
} else {
    Join-Path $RepoRoot "minipilot-main"
}
$CapturePython = Join-Path $CaptureRoot ".venv\Scripts\python.exe"
if (-not $GamePython) {
    $GamePython = Join-Path $WzryDir ".venv\Scripts\python.exe"
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

& $GamePython (Join-Path $WzryDir "check_runtime.py") --model-path $ModelPath --compute-device $ComputeDevice
if ($LASTEXITCODE -ne 0) { throw "WZRY runtime preflight failed." }

$adbReady = $false
if ($DeviceId -match ':') { & $Adb connect $DeviceId 2>$null | Out-Null }
for ($attempt = 1; $attempt -le 4; $attempt++) {
    $devices = & $Adb devices 2>$null
    if ($devices -match ([regex]::Escape($DeviceId) + "\s+device")) {
        $adbReady = $true
        break
    }
    Start-Sleep -Seconds 2
}
if (-not $adbReady) {
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
    "--game-id", "wzry",
    "--game-python", $GamePython,
    "--game-driver", (Join-Path $WzryDir "train.py"),
    "--game-directory", $WzryDir,
    "--game-timeout", "$GameTimeout",
    "--match-ready-timeout", "$MatchReadyTimeout",
    "--capture-ceiling", "$CaptureCeiling",
    "--capture-app", "honorofkings",
    "--capture-scene", "battle",
    "--capture-pcap-mode", "standard",
    "--postprocess-ocr-every", "$PostprocessOcrEvery",
    "--game-package", "com.tencent.tmgp.sgame",
    "--game-launch-activity", ".SGameActivity",
    "--pregame-driver", (Join-Path $WzryDir "pregame_driver.py"),
    "--pregame-python", $GamePython,
    "--pregame-directory", $WzryDir,
    "--game-arg=--model_path",
    "--game-arg=$ModelPath",
    "--game-arg=--window_title",
    "--game-arg=wzry_ai",
    "--game-arg=--device_id",
    "--game-arg=$ComputeDevice"
)
if ($PregameGoal) { $arguments += @("--pregame-goal", $PregameGoal) }
if ($SkipPcapdroidPreflight) { $arguments += "--capture-skip-pcapdroid-preflight" }

if ($Rounds -gt 1) {
    $arguments += @("--rounds", "$Rounds")
    if ($SessionId) {
        $arguments += @("--session-id", $SessionId)
    }
}
elseif ($RunId) {
    $arguments += @("--run-id", $RunId)
}

if ($DisablePostprocess) {
    $arguments += "--capture-disable-postprocess"
}

if ($DryRun) {
    Write-Output ($CapturePython + " " + ($arguments -join " "))
    return
}

Push-Location $CaptureRoot
try {
    & $CapturePython @arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
