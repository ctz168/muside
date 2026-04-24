#Requires -Version 5.1
<#
.SYNOPSIS
    PhoneIDE IDE - One-line installer for Windows (fully automated)
.DESCRIPTION
    Installs Python 3, pip dependencies, clones repo, starts server,
    checks port, opens browser. One command, ready to use.
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

# ── Step 2: Install pip + dependencies ──────────────────
Write-Host ""
Write-Host "[2/5] Installing dependencies..." -ForegroundColor Blue

# Ensure pip
& $Python -m pip --version 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Info "Installing pip..."
    & $Python -m ensurepip --upgrade 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        $getPip = "$env:TEMP\get-pip.py"
        try {
            Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getPip -UseBasicParsing
            & $Python $getPip
            Remove-Item $getPip -Force -ErrorAction SilentlyContinue
        } catch {
            Write-Warn "pip install failed"
        }
    }
}

# Install flask + flask-cors
& $Python -m pip install flask flask-cors --quiet 2>$null
if ($LASTEXITCODE -ne 0) {
    & $Python -m pip install --user flask flask-cors --quiet 2>$null
}
if ($LASTEXITCODE -eq 0) {
    Write-OK "flask + flask-cors"
} else {
    Write-Warn "pip install failed - try: $Python -m pip install flask flask-cors"
}

# ── Install audio analysis dependencies ──
# These are needed for smart stem separation + lyrics transcription
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

# ── Step 3: Clone & setup ──────────────────────────────
Write-Host ""
Write-Host "[3/5] Setting up MusIDE IDE..." -ForegroundColor Blue

if (Test-Path "$InstallDir\.git") {
    Write-Info "Updating existing installation..."
    Push-Location $InstallDir
    git pull --ff-only 2>$null
    if ($LASTEXITCODE -ne 0) { Write-Warn "git pull failed - using existing files" }
    Pop-Location
} else {
    if (Test-Path $InstallDir) {
        Write-Warn "Directory $InstallDir exists but is not a git repo"
        $InstallDir = "$InstallDir-$(Get-Date -Format 'yyyyMMddHHmmss')"
        Write-Warn "Using $InstallDir instead"
    }

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

    # Clone with mirror fallback (same as Linux install.sh)
    $CloneUrls = @(
        "https://github.com/ctz168/muside.git",
        "https://ghfast.top/https://github.com/ctz168/muside.git",
        "https://gh-proxy.com/https://github.com/ctz168/muside.git",
        "https://mirror.ghproxy.com/https://github.com/ctz168/muside.git"
    )

    $CloneOK = $false
    foreach ($url in $CloneUrls) {
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            Write-Info "Cloning (attempt $attempt/3)..."
            git clone --depth 1 $url $InstallDir 2>$null
            if ($LASTEXITCODE -eq 0) {
                $CloneOK = $true
                break
            }
            if ($attempt -lt 3) { Start-Sleep -Seconds 2 }
        }
        if ($CloneOK) { break }
        # Clean up failed partial clone
        Remove-Item -Recurse -Force $InstallDir -ErrorAction SilentlyContinue
    }

    if (-not $CloneOK) {
        Write-Fail "All clone attempts failed - check your network"
        Write-Host ""
        Write-Host "Try manually:" -ForegroundColor Yellow
        Write-Host "  git clone https://github.com/ctz168/muside.git $InstallDir" -ForegroundColor Cyan
        exit 1
    }

    # Normalize remote to official GitHub (in case we cloned via mirror)
    if ($url -ne "https://github.com/ctz168/muside.git") {
        Push-Location $InstallDir
        git remote set-url origin https://github.com/ctz168/muside.git 2>$null
        Pop-Location
        Write-Info "Remote set to official GitHub URL"
    }
}

# Create dirs
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\muside_workspace" | Out-Null
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.muside" | Out-Null

Write-OK "Ready at $InstallDir"

# ── Step 4: Verify dependencies in target environment ──
Write-Host ""
Write-Host "[4/5] Verifying in target environment..." -ForegroundColor Blue

Push-Location $InstallDir
$VerifyFailed = $false

try {
    $flaskCheck = & $Python -c "import flask; print(flask.__version__)" 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-OK "flask $flaskCheck"
    } else {
        Write-Info "flask not found - installing..."
        & $Python -m pip install flask --quiet 2>$null
        if ($LASTEXITCODE -ne 0) { & $Python -m pip install --user flask --quiet 2>$null }
        if ($LASTEXITCODE -eq 0) { Write-OK "flask installed" } else { $VerifyFailed = $true }
    }

    $corsCheck = & $Python -c "import flask_cors; print('ok')" 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-OK "flask-cors"
    } else {
        & $Python -m pip install flask-cors --quiet 2>$null
        if ($LASTEXITCODE -eq 0) { Write-OK "flask-cors installed" } else { $VerifyFailed = $true }
    }

    # Final smoke test
    $smoke = & $Python -c "from flask import Flask; from flask_cors import CORS; print('OK')" 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-OK "Core dependencies ready"
    } else {
        Write-Warn "Flask import still fails - you may need to run: $Python -m pip install flask flask-cors"
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
        Write-Host "  $Python -m pip install torch torchaudio demucs openai-whisper" -ForegroundColor Cyan
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
Write-Host ""
Write-Host "  Press Ctrl+C to stop the server" -ForegroundColor Yellow
Write-Host ""

# Run server in foreground — Ctrl+C will stop it
# Set PYTHONIOENCODING=utf-8 to prevent GBK encoding errors on Chinese Windows
$env:PYTHONIOENCODING = "utf-8"
Set-Location $InstallDir
& $Python muside_server.py
