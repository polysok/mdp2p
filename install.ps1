# mdp2p installer for Windows (PowerShell 5.1+ / 7+).
#
# Usage (run in PowerShell):
#   iwr -useb https://raw.githubusercontent.com/<user>/mdp2p/main/install.ps1 | iex
#
# Environment overrides:
#   $env:MDP2P_REPO    = GitHub repo in "owner/name" form (default: polysok/mdp2p)
#   $env:MDP2P_VERSION = tag to install                   (default: latest)
#   $env:INSTALL_DIR   = where to drop the binary         (default: %LOCALAPPDATA%\mdp2p\bin)

$ErrorActionPreference = 'Stop'

$repo       = if ($env:MDP2P_REPO)    { $env:MDP2P_REPO }    else { "polysok/mdp2p" }
$version    = if ($env:MDP2P_VERSION) { $env:MDP2P_VERSION } else { "latest" }
$installDir = if ($env:INSTALL_DIR)   { $env:INSTALL_DIR }   else { Join-Path $env:LOCALAPPDATA "mdp2p\bin" }

# ─── Platform detection ──────────────────────────────────────────────
if (-not [Environment]::Is64BitOperatingSystem) {
    throw "mdp2p requires 64-bit Windows. 32-bit builds are not produced."
}
$asset = "mdp2p-windows-x86_64.exe"

$url = if ($version -eq "latest") {
    "https://github.com/$repo/releases/latest/download/$asset"
} else {
    "https://github.com/$repo/releases/download/$version/$asset"
}

# ─── Download ────────────────────────────────────────────────────────
Write-Host "-> Downloading $asset ($version)"
Write-Host "   from $url"

New-Item -ItemType Directory -Force -Path $installDir | Out-Null
$target = Join-Path $installDir "mdp2p.exe"

# TLS 1.2 for older PowerShell hosts.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

try {
    Invoke-WebRequest -Uri $url -OutFile $target -UseBasicParsing
} catch {
    throw "Download failed: $($_.Exception.Message)"
}

Write-Host "OK Installed to $target"

# ─── PATH hint ───────────────────────────────────────────────────────
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (($userPath -split ';') -notcontains $installDir) {
    Write-Host ""
    Write-Host "!  $installDir is not in your PATH."
    $answer = Read-Host "   Add it now? (Y/n)"
    if ($answer -eq "" -or $answer -match '^[Yy]') {
        $newPath = if ($userPath) { "$userPath;$installDir" } else { $installDir }
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
        Write-Host "OK PATH updated for this user."
        Write-Host "   Open a NEW terminal, then run: mdp2p"
    } else {
        Write-Host "   You can run it directly with: $target"
    }
} else {
    Write-Host ""
    Write-Host "Run: mdp2p"
}
