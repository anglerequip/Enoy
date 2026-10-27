# Install missing official CLIs. Account sign-in is completed by the user in Enoy.
param([switch]$Pause, [switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$failures = @()

function Find-Provider([string]$Name) {
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidates = @((Join-Path $env:USERPROFILE ".local\bin\$Name.exe"),
        (Join-Path $env:APPDATA "npm\$Name.cmd"))
    if ($Name -eq 'codex') {
        $candidates += Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin\codex.exe'
    }
    foreach ($candidate in $candidates) { if (Test-Path -LiteralPath $candidate) { return $candidate } }
    return $null
}

function Install-Official([string]$Name, [string]$Url) {
    $existing = Find-Provider $Name
    if ($existing) { Write-Host "$Name is already installed." -ForegroundColor Green; return }
    if ($CheckOnly) { Write-Host "$Name is missing."; return }
    Write-Host "Installing $Name from $Url ..." -ForegroundColor Cyan
    $script = Join-Path ([IO.Path]::GetTempPath()) ("enoy-" + $Name + '-' + [guid]::NewGuid().ToString('N') + '.ps1')
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $script
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script
    if ($LASTEXITCODE -ne 0 -or -not (Find-Provider $Name)) {
        throw "$Name setup did not finish. Retry with Install missing tools in Enoy Settings."
    }
}

foreach ($provider in @(@('codex', 'https://chatgpt.com/codex/install.ps1'), @('claude', 'https://claude.ai/install.ps1'))) {
    try { Install-Official $provider[0] $provider[1] }
    catch { $failures += $_.Exception.Message; Write-Host $_.Exception.Message -ForegroundColor Red }
}

# WebView2 is normally present on Windows 11. Install Microsoft's signed runtime if missing.
$webview = @('HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\*',
    'HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\*', 'HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\*') |
    ForEach-Object { Get-ItemProperty $_ -ErrorAction SilentlyContinue } |
    Where-Object { $_.name -like '*WebView2*' -and $_.pv -and $_.pv -ne '0.0.0.0' }
if (-not $webview -and -not $CheckOnly) {
    try {
        Write-Host 'Installing Microsoft WebView2 Runtime ...' -ForegroundColor Cyan
        $bootstrap = Join-Path ([IO.Path]::GetTempPath()) ('enoy-webview2-' + [guid]::NewGuid().ToString('N') + '.exe')
        Invoke-WebRequest -UseBasicParsing -Uri 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' -OutFile $bootstrap
        $signature = Get-AuthenticodeSignature -LiteralPath $bootstrap
        if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') {
            throw 'WebView2 signature validation failed.'
        }
        $runtime = Start-Process -FilePath $bootstrap -ArgumentList '/silent', '/install' -Wait -PassThru -WindowStyle Hidden
        if ($runtime.ExitCode -notin @(0, 3010)) { throw 'WebView2 setup failed. Enoy can fall back to Edge or your browser.' }
    } catch { $failures += $_.Exception.Message; Write-Host $_.Exception.Message -ForegroundColor Red }
}
Write-Host ''
Write-Host 'Open Enoy Settings > General, then sign into Codex and Claude with your own accounts.' -ForegroundColor Cyan
if ($Pause -or $failures.Count -gt 0) { Read-Host 'Press Enter to close' | Out-Null }
if ($failures.Count -gt 0) { exit 1 }
