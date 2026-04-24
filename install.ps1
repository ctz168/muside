#Requires -Version 5.1
<#
.SYNOPSIS
    MusIDE IDE - One-line installer for Windows (fully automated)
.DESCRIPTION
    Installs Python 3, creates venv, installs all pip dependencies (flask, torch, demucs, whisper),
    clones repo, starts server, checks port, opens browser. One command, ready to use.
.EXAMPLE
    irm https://raw.githubusercontent.com/ctz168/muside/main/install.ps1 | iex
.EXAMPLE
    $env:MUSIDE_INSTALL_DIR="C:\my-muside"; irm https://raw.githubusercontent.com/ctz168/muside/main/install.ps1 | iex
#>

$ErrorActionPreference = "SilentlyContinue"

# ── Config ───────────────────────────────────────────────
$RepoUrl = "https://github.com/ctz168/muside.git"
$DefaultDir = "$env:USERPROFILE\muside-ide"
$InstallDir = if ($env:MUSIDE_INSTALL_DIR) { $env:MUSIDE_INSTALL_DIR.Replace('~', $env:USERPROFILE) } else { $DefaultDir }

function Write-Info($msg)  { Write-Host "  [*] $msg" -ForegroundColor Blue }
function Write-OK($msg)    { Write-Host "  [+] $msg" -ForegroundColor Green }
function Write-Warn($msg)  { Write-Host "  [!] $msg" -ForegroundColor Yellow }
function Write-Fail($msg)  { Write-Host "  [-] $msg" -ForegroundColor Red }

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "       MusIDE IDE Installer" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Info "Platform: Windows"
Write-Info "Install dir: $InstallDir"
Write-Host ""

# ── Step 1: Install Python ──────────────────────────────
Write-Host "[1/5] Checking Python..." -ForegroundColor Blue

$Python = $null
foreach ($cmd in @("python3", "python", "py")) {
    $exe = Get-Command $cmd -ErrorAction SilentlyContinue
    if ($exe) {
        $ver = & $cmd --version 2>&1
        if ($ver -match "Python (\d+)\.(\d+)") {
            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 8)) {
                $Python = $cmd
                break
            }
        }
    }
}

if (-not $Python) {
    Write-Info "Python 3.8+ not found, installing via winget..."

    # Try winget (Windows 10 1709+)
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Info "Installing Python via winget..."
        winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements 2>$null
        if ($LASTEXITCODE -eq 0) {
            Write-Info "Winget install done, refreshing PATH..."
            $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
        }
    }

    # Re-check
    foreach ($cmd in @("python3", "python", "py")) {
        $exe = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($exe) {
            $ver = & $cmd --version 2>&1
            if ($ver -match "Python (\d+)\.(\d+)") {
                $major = [int]$Matches[1]; $minor = [int]$Matches[2]
                if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 8)) {
                    $Python = $cmd; break
                }
            }
        }
    }

    # Last resort: try the default install path
    if (-not $Python) {
        $pyExe = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
        if (Test-Path $pyExe) { $Python = $pyExe }
        else {
            $pyExe = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
            if (Test-Path $pyExe) { $Python = $pyExe }
        }
    }

    if (-not $Python) {
        Write-Fail "Python installation failed"
        Write-Host ""
        Write-Host "Please install Python 3.8+ manually:" -ForegroundColor Yellow
        Write-Host "  1. Download: https://www.python.org/downloads/" -ForegroundColor Yellow
        Write-Host "  2. Run installer and CHECK 'Add Python to PATH'" -ForegroundColor Yellow
        Write-Host "  3. Reopen terminal and re-run this script" -ForegroundColor Yellow
        exit 1
    }
}

$pyVer = & $Python --version 2>&1
Write-OK $pyVer

# ── Step 2: Clone repo first ──────────────────────────────
Write-Host ""
Write-Host "[2/5] Downloading MusIDE IDE..." -ForegroundColor Blue

if (Test-Path "$InstallDir\.git") {
    Write-Info "Updating existing installation..."
    Push-Location $InstallDir
    git pull --ff-only 2>$null
    if ($LASTEXITCODE -ne 0) { Write-Warn "git pull failed - using existing files" }
    Pop-Location
} else {
    Write-Info "Cloning ctz168/muside..."

    $git = Get-Command git -ErrorAction SilentlyContinue
    if (-not $git) {
        Write-Info "Installing git via winget..."
        winget install Git.Git --accept-package-agreements --accept-source-agreements 2>$null
        if ($LASTEXITCODE -eq 0) {
            $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
        }
    }

    $git = Get-Command git -ErrorAction SilentlyContinue
    if (-not $git) {
        Write-Fail "git not found - please install Git: https://git-scm.com/download/win"
        exit 1
    }

    # Clone to temp dir first, then copy (preserves any existing .venv)
    $CloneTmp = Join-Path $env:TEMP "muside-clone-$(Get-Random)"
    $CloneUrls = @(
        "https://github.com/ctz168/muside.git",
        "https://ghfast.top/https://github.com/ctz168/muside.git",
        "https://gh-proxy.com/https://github.com/ctz168/muside.git",
        "https://mirror.ghproxy.com/https://github.com/ctz168/muside.git"
    )

    $CloneOK = $false
    $UsedUrl = $null
    foreach ($url in $CloneUrls) {
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            Write-Info "Cloning (attempt $attempt/3)..."
            git clone --depth 1 $url $CloneTmp 2>$null
            if ($LASTEXITCODE -eq 0) {
                $CloneOK = $true
                $UsedUrl = $url
                break
            }
            if ($attempt -lt 3) { Start-Sleep -Seconds 2 }
        }
        if ($CloneOK) { break }
        Remove-Item -Recurse -Force $CloneTmp -ErrorAction SilentlyContinue
    }

    if (-not $CloneOK) {
        Write-Fail "All clone attempts failed - check your network"
        Write-Host ""
        Write-Host "Try manually:" -ForegroundColor Yellow
        Write-Host "  git clone https://github.com/ctz168/muside.git $InstallDir" -ForegroundColor Cyan
        exit 1
    }

    # Copy cloned files to INSTALL_DIR (preserving any existing .venv)
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    # Robocopy: /E = recursive, /XD = exclude dirs, /NFL/NDL/NJ/NJS/NP = quiet
    robocopy $CloneTmp $InstallDir /E /XD .venv /NFL /NDL /NJ /NJS /NP 2>$null
    Remove-Item -Recurse -Force $CloneTmp -ErrorAction SilentlyContinue

    # Normalize remote to official GitHub
    if ($UsedUrl -ne "https://github.com/ctz168/muside.git") {
        Push-Location $InstallDir
        git remote set-url origin https://github.com/ctz168/muside.git 2>$null
        Pop-Location
        Write-Info "Remote set to official GitHub URL"
    }
}

# ── Step 3: Create venv + install ALL dependencies (at final location) ──
Write-Host ""
Write-Host "[3/5] Setting up Python environment & installing dependencies..." -ForegroundColor Blue

# Create venv directly at final location (no move needed)
$VenvDir = "$InstallDir\.venv"

if (-not (Test-Path "$VenvDir\Scripts\python.exe")) {
    Write-Info "Creating virtual environment..."
    & $Python -m venv $VenvDir 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Info "venv creation failed, trying with --system-site-packages..."
        & $Python -m venv --system-site-packages $VenvDir 2>$null
    }
}

# Use venv python for all subsequent operations
$VenvPython = "$VenvDir\Scripts\python.exe"
$VenvPip = "$VenvDir\Scripts\pip.exe"

if (Test-Path $VenvPython) {
    Write-OK "Virtual environment at $VenvDir"
    $Python = $VenvPython
} else {
    Write-Warn "venv creation failed, using system Python"
}

# Upgrade pip
& $Python -m pip install --upgrade pip --quiet 2>$null

# Install core dependencies (flask, flask-cors)
Write-Info "Installing core dependencies (flask, flask-cors)..."
& $Python -m pip install flask flask-cors --quiet 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-OK "flask + flask-cors"
} else {
    Write-Fail "Failed to install flask - check your network connection"
    exit 1
}

# ── Install audio analysis dependencies ──
Write-Info "Installing audio analysis dependencies (may take a few minutes)..."

# Install PyTorch (CPU version to save disk space)
Write-Info "Installing PyTorch (CPU version)..."
& $Python -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu --quiet 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Info "CPU-only install failed, trying standard package..."
    & $Python -m pip install torch torchaudio --quiet 2>$null
}
$torchCheck = & $Python -c "import torch; print(torch.__version__)" 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-OK "torch $torchCheck + torchaudio"
} else {
    Write-Warn "torch/torchaudio install failed - audio analysis will be limited"
}

# Install demucs for stem separation
Write-Info "Installing Demucs (stem separation)..."
& $Python -m pip install demucs --quiet 2>$null
$demucsCheck = & $Python -c "from demucs.pretrained import get_model; print('ok')" 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-OK "demucs (stem separation)"
} else {
    Write-Warn "demucs install failed - stem separation unavailable"
}

# Install openai-whisper for lyrics transcription
Write-Info "Installing Whisper (lyrics transcription)..."
& $Python -m pip install openai-whisper --quiet 2>$null
$whisperCheck = & $Python -c "import whisper; print('ok')" 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-OK "openai-whisper (lyrics transcription)"
} else {
    Write-Warn "whisper install failed - lyrics transcription unavailable"
}

# Create dirs
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\muside_workspace" | Out-Null
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.muside" | Out-Null

Write-OK "Ready at $InstallDir"

# ── Step 4: Verify dependencies ──
Write-Host ""
Write-Host "[4/5] Verifying in target environment..." -ForegroundColor Blue

Push-Location $InstallDir
try {
    $flaskCheck = & $Python -c "from flask import Flask; from flask_cors import CORS; print('OK')" 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-OK "Core dependencies ready"
    } else {
        Write-Warn "Flask import fails - installing in venv..."
        & $Python -m pip install flask flask-cors --quiet 2>$null
    }

    # Verify audio analysis dependencies
    $audioOk = $true
    $torchVer = & $Python -c "import torch; print(torch.__version__)" 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-OK "torch $torchVer"
    } else {
        Write-Warn "torch not installed - audio analysis will be limited"
        $audioOk = $false
    }

    $demucsVer = & $Python -c "from demucs.pretrained import get_model; print('ok')" 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-OK "demucs (stem separation)"
    } else {
        Write-Warn "demucs not installed - stem separation unavailable"
        $audioOk = $false
    }

    $whisperVer = & $Python -c "import whisper; print('ok')" 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-OK "whisper (lyrics transcription)"
    } else {
        Write-Warn "whisper not installed - lyrics transcription unavailable"
        $audioOk = $false
    }

    if ($audioOk) {
        Write-OK "Audio analysis: fully enabled (stem separation + lyrics transcription)"
    } else {
        Write-Warn "Audio analysis: partially available. For full features, run:"
        Write-Host "  $VenvDir\Scripts\pip.exe install torch torchaudio demucs openai-whisper" -ForegroundColor Cyan
    }
} finally {
    Pop-Location
}

# ── Step 5: Start server, check port, open browser ────
Write-Host ""
Write-Host "[5/5] Launching MusIDE IDE..." -ForegroundColor Blue

# Detect port from utils.py (respects MUSIDE_PORT env var)
Push-Location $InstallDir
try {
    $DetectedPort = & $Python -c "import sys; sys.path.insert(0,'.'); from utils import PORT; print(PORT)" 2>$null
    if ($LASTEXITCODE -ne 0) { $DetectedPort = 12346 }
} catch {
    $DetectedPort = 12346
} finally {
    Pop-Location
}

if ($env:MUSIDE_PORT) {
    $DetectedPort = [int]$env:MUSIDE_PORT
}

$IDE_PORT = $DetectedPort
$IDE_LOCAL = "http://localhost:$IDE_PORT"
$IDE_URL = $IDE_LOCAL

Write-Info "Detected server port: $IDE_PORT"

# Kill existing server on the same port
$ExistingPid = $null
try {
    $conn = Get-NetTCPConnection -LocalPort $IDE_PORT -ErrorAction SilentlyContinue |
        Where-Object { $_.State -eq 'Listen' } |
        Select-Object -First 1 -ExpandProperty OwningProcess
    if ($conn) {
        $ExistingPid = $conn
        Write-Info "Port $IDE_PORT is in use (PID: $ExistingPid), killing..."
        Stop-Process -Id $ExistingPid -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    }
} catch {
    # Get-NetTCPConnection not available on older Windows, try netstat
    $netstatOutput = netstat -ano 2>$null | Select-String ":$IDE_PORT\s.*LISTENING"
    if ($netstatOutput) {
        $match = $netstatOutput -match '\s+(\d+)\s*$'
        if ($match) {
            $ExistingPid = [int]$Matches[1]
            Write-Info "Port $IDE_PORT is in use (PID: $ExistingPid), killing..."
            taskkill /F /PID $ExistingPid 2>$null
            Start-Sleep -Seconds 1
        }
    }
}

# Open browser first (in background, with delay to let server start)
$BrowserUrl = $IDE_LOCAL
Start-Job -ScriptBlock {
    Start-Sleep -Seconds 3
    Start-Process $using:BrowserUrl
} | Out-Null

# ── Done message ──────────────────────────────────────
Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  MusIDE IDE is ready!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Local:    $IDE_LOCAL" -ForegroundColor Cyan
Write-Host "  Network:  $IDE_URL" -ForegroundColor Cyan
Write-Host "  Dir:      $InstallDir" -ForegroundColor Cyan
Write-Host "  Venv:     $VenvDir" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Press Ctrl+C to stop the server" -ForegroundColor Yellow
Write-Host ""

# Run server in foreground — Ctrl+C will stop it
# Set PYTHONIOENCODING=utf-8 to prevent GBK encoding errors on Chinese Windows
$env:PYTHONIOENCODING = "utf-8"
Set-Location $InstallDir
& $Python muside_server.py
