param([string]$Python = 'python', [string]$Iscc = 'ISCC.exe')
$ErrorActionPreference = 'Stop'
$rootDir = Split-Path -Parent $PSScriptRoot
Push-Location $rootDir
try {
    $version = & $Python -c 'from enoy import __version__; print(__version__)'
    if ($LASTEXITCODE -ne 0) { throw 'Could not read Enoy version.' }
    & $Python -m PyInstaller --noconfirm --log-level WARN launch/Enoy.spec
    if ($LASTEXITCODE -ne 0) { throw 'Desktop build failed.' }
    # Include installed third-party license texts in the distributable.
    & $Python launch/collect-licenses.py
    if ($LASTEXITCODE -ne 0) { throw 'Collecting dependency licenses failed.' }
    & $Iscc /Q ("/DAppVersion=" + $version) launch/installer.iss
    if ($LASTEXITCODE -ne 0) { throw 'Windows installer build failed.' }
    $installer = Join-Path $rootDir 'dist\Enoy-Setup-x64.exe'
    $hash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
    [IO.File]::WriteAllText($installer + '.sha256', "$hash  Enoy-Setup-x64.exe`n", [Text.Encoding]::ASCII)
    Write-Host "Ready to share: $installer" -ForegroundColor Green
} finally { Pop-Location }
