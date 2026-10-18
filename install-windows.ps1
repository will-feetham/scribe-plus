# Scribe Plus installer and updater for Windows.
#
#   irm https://scribeplus.feetham.us/install.ps1 | iex
#
# Downloads the latest release from GitHub and runs its "Install Scribe Plus.bat",
# which installs (or updates) Scribe Plus in %LOCALAPPDATA%\Programs\Scribe Plus.
# Your API keys, settings and history are kept.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$zipUrl = 'https://github.com/will-feetham/scribe-plus/releases/latest/download/ScribePlus-Windows.zip'
$tmp = Join-Path $env:TEMP ('scribe-plus-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    Write-Host 'Downloading the latest Scribe Plus...'
    Invoke-WebRequest -Uri $zipUrl -OutFile (Join-Path $tmp 'ScribePlus.zip') -UseBasicParsing
    Expand-Archive -Path (Join-Path $tmp 'ScribePlus.zip') -DestinationPath $tmp -Force
    $bat = Join-Path $tmp 'ScribePlus\Install Scribe Plus.bat'
    if (-not (Test-Path $bat)) { throw 'The download does not look like Scribe Plus.' }
    & cmd.exe /c "`"$bat`""
} finally {
    Remove-Item -Path $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
