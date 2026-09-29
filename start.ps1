$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonPath = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ is required.' }
    & $pythonPath -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}
$address = 'http://127.0.0.1:8765'
$running = $false
try {
    $reply = Invoke-RestMethod -Uri "$address/api/session" -TimeoutSec 2
    $running = $reply.app -eq 'land-recon'
} catch { }
if (-not $running) {
    New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot 'data') | Out-Null
    Start-Process -FilePath $pythonPath -ArgumentList @('app.py') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $PSScriptRoot 'data/server.log') -RedirectStandardError (Join-Path $PSScriptRoot 'data/server-errors.log')
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        Start-Sleep -Milliseconds 250
        try {
            $reply = Invoke-RestMethod -Uri "$address/api/session" -TimeoutSec 1
            if ($reply.app -eq 'land-recon') { $running = $true; break }
        } catch { }
    }
}
if (-not $running) { throw 'App did not start. Check data/server-errors.log and port 8765.' }
Start-Process $address
