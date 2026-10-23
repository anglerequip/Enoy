# Enoy - first-run setup. Safe to run again at any time.
#
# What it does:
#   1. Checks that Python 3.11+ is installed (does not install it).
#   2. Creates a private Python environment in .venv inside this folder.
#   3. Installs the pinned packages from requirements.txt (downloads from PyPI).
#   4. Creates "Enoy" shortcuts on the Desktop and in the Start menu.
#   5. Checks whether Ollama and the qwen3.5:9b model are present (does not download models).
#
# It needs no administrator rights and does not change system settings.
# -NoShortcuts skips step 4 (used when testing a copy of the folder).

param([switch]$NoShortcuts)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
Write-Host "Enoy setup in $Root" -ForegroundColor Cyan

# 1. Python
$py = Get-Command py -ErrorAction SilentlyContinue
$pythonCmd = $null
if ($py) {
    & py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) { $pythonCmd = @("py", "-3") }
}
if (-not $pythonCmd) {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        & python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $pythonCmd = @("python") }
    }
}
if (-not $pythonCmd) {
    Write-Host "Python 3.11 or newer was not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this setup again." -ForegroundColor Red
    exit 1
}
Write-Host "[1/5] Python found."

# 2. Virtual environment
$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "[2/5] Creating .venv ..."
    $venvArgs = @($pythonCmd | Select-Object -Skip 1) + @("-m", "venv", ".venv")
    & $pythonCmd[0] @venvArgs
    if ($LASTEXITCODE -ne 0) { Write-Host "Creating the Python environment failed." -ForegroundColor Red; exit 1 }
} else {
    Write-Host "[2/5] .venv already exists."
}

# 3. Packages
Write-Host "[3/5] Installing pinned packages (this downloads from PyPI the first time) ..."
& $venvPython -m pip install --disable-pip-version-check --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) { Write-Host "Package installation failed. See the messages above." -ForegroundColor Red; exit 1 }

# 4. Shortcuts
$icon = Join-Path $Root "launch\enoy.ico"
if (-not (Test-Path $icon)) { & $venvPython (Join-Path $Root "launch\make_icon.py") $icon }
if ($NoShortcuts) {
    Write-Host "[4/5] Skipping shortcuts (-NoShortcuts)."
} else {
Write-Host "[4/5] Creating shortcuts ..."
$pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"
$shell = New-Object -ComObject WScript.Shell
$targets = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "Enoy.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "Enoy.lnk")
)
# The app was called Fusion AI Workspace before; remove only those old shortcuts, and only if they start this app.
foreach ($old in @((Join-Path ([Environment]::GetFolderPath("Desktop")) "Fusion AI Workspace.lnk"),
                   (Join-Path ([Environment]::GetFolderPath("Programs")) "Fusion AI Workspace.lnk"))) {
    if (Test-Path $old) {
        $o = $shell.CreateShortcut($old)
        if ($o.Arguments -eq "-m fusion_ai" -and $o.WorkingDirectory -eq $Root) { Remove-Item $old; Write-Host "      Removed old shortcut $old" }
    }
}
foreach ($lnkPath in $targets) {
    $lnk = $shell.CreateShortcut($lnkPath)
    $lnk.TargetPath = $pythonw
    $lnk.Arguments = "-m fusion_ai"
    $lnk.WorkingDirectory = $Root
    $lnk.IconLocation = $icon
    $lnk.Description = "Enoy"
    $lnk.Save()
    Write-Host "      $lnkPath"
}
# Give the shortcuts Enoy's taskbar ID, so Windows shows Enoy instead of Python.
Push-Location $Root
& $venvPython -m fusion_ai.winshell @targets | Out-Null
Pop-Location
}

# 5. Ollama check (information only)
Write-Host "[5/5] Checking Ollama ..."
$ollama = Get-Command ollama -ErrorAction SilentlyContinue
if (-not $ollama) {
    Write-Host "      Ollama is not installed. Get it from https://ollama.com/download. Enoy's local model needs it." -ForegroundColor Yellow
} else {
    $models = & ollama list 2>$null
    if ($models -match "qwen3.5:9b") { Write-Host "      Ollama and qwen3.5:9b are installed." -ForegroundColor Green }
    else { Write-Host "      qwen3.5:9b is not installed. To download it (about 6.6 GB), run:  ollama pull qwen3.5:9b" -ForegroundColor Yellow }
}

Write-Host ""
Write-Host "Done. Start Enoy from the Desktop or Start menu shortcut." -ForegroundColor Green
