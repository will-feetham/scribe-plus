# Scribe Plus for Windows. Fetches the installer that matches the latest
# release and runs it. Usage:  irm https://scribeplus.feetham.us/install.ps1 | iex
$url = 'https://github.com/will-feetham/scribe-plus/releases/latest/download/install-windows.ps1'
Invoke-Expression ((New-Object System.Net.WebClient).DownloadString($url))
