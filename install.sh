#!/bin/bash
# MusIDE IDE - Cross-platform one-line installer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/ctz168/muside/main/install.sh | bash
#
# Works on: Termux, proot Ubuntu, Ubuntu/Debian, Fedora, CentOS, macOS, Alpine, Arch Linux
# Installs: Python 3, pip, flask, flask-cors, clones repo, launches server

# NOTE: We intentionally do NOT use 'set -e' here.
# 'set -e' causes the script to exit silently on any command failure,
# which is terrible for an installer — many commands may fail non-fatally
# (e.g., pip warnings, missing sudo, platform quirks in proot).
# Instead, we check errors explicitly at critical points.

# ── Colors ──────────────────────────────────────────────
if [ -t 1 ]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
    BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; BLUE=''; CYAN=''; NC=''
fi

info()  { echo -e "${BLUE}  [✦]${NC} $1"; }
ok()    { echo -e "${GREEN}  [✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}  [!]${NC} $1"; }
fail()  { echo -e "${RED}  [✗]${NC} $1"; }

echo -e "${CYAN}"
echo "╔══════════════════════════════════════════╗"
echo "║       MusIDE IDE Installer             ║"
echo "║       Mobile Web IDE                     ║"
echo "╚══════════════════════════════════════════╝"
echo -e "${NC}"

# ── Detect platform ───────────────────────────────────
detect_platform() {
    # Check for Termux native first
    if [ -n "$TERMUX_VERSION" ] || [ -d "/data/data/com.termux" ]; then
        echo "termux"
        return
    fi
    # Check for proot environment (Termux's Ubuntu, etc.)
    # proot leaves traces in /proc/version or has PROOT_TMP
    if [ -r "/proc/version" ] && grep -qi "termux\|proot" /proc/version 2>/dev/null; then
        echo "proot"
        return
    fi
    if [ -n "$PROOT_TMP" ] || [ -n "$PROOT_LAUNCHER" ]; then
        echo "proot"
        return
    fi
    # Standard platform detection
    if [ "$(uname)" = "Darwin" ]; then
        echo "macos"
    elif command -v apt-get &>/dev/null; then
        echo "debian"
    elif command -v dnf &>/dev/null; then
        echo "fedora"
    elif command -v yum &>/dev/null; then
        echo "centos"
    elif command -v apk &>/dev/null; then
        echo "alpine"
    elif command -v pacman &>/dev/null; then
        echo "arch"
    elif command -v zypper &>/dev/null; then
        echo "opensuse"
    else
        echo "unknown"
    fi
}

# Determine if we should use sudo (skip if already root or in proot)
need_sudo() {
    # In proot environments, we're already root — sudo may not exist
    if [ "$(id -u)" = "0" ]; then
        return 1  # Don't need sudo
    fi
    if ! command -v sudo &>/dev/null; then
        return 1  # sudo not available
    fi
    return 0  # Need sudo
}

PLATFORM=$(detect_platform)
INSTALL_DIR="${MUSIDE_INSTALL_DIR:-$HOME/muside-ide}"
INSTALL_DIR="$(echo "$INSTALL_DIR" | sed "s|~|$HOME|")"

# Show platform info
if [ "$PLATFORM" = "proot" ]; then
    info "Platform: proot (Termux Ubuntu/Debian)"
else
    info "Platform: $PLATFORM"
fi
info "Install dir: $INSTALL_DIR"
echo ""

# ── Package installer ─────────────────────────────────
install_packages() {
    # $1 = platform, $2.. = packages
    local platform="$1"; shift
    local sudo_prefix=""
    if need_sudo; then
        sudo_prefix="sudo"
    fi
    case "$platform" in
        termux)  pkg install -y "$@" ;;
        proot)   apt-get update -qq 2>/dev/null; apt-get install -y "$@" ;;
        debian)  $sudo_prefix apt-get update -qq && $sudo_prefix apt-get install -y "$@" ;;
        fedora)  $sudo_prefix dnf install -y "$@" ;;
        centos)  $sudo_prefix yum install -y "$@" ;;
        alpine)  $sudo_prefix apk add --no-progress "$@" ;;
        arch)    $sudo_prefix pacman -S --noconfirm "$@" ;;
        opensuse) $sudo_prefix zypper install -y "$@" ;;
        macos)   brew install "$@" ;;
    esac
}

# ── Step 1/5: Install Python ─────────────────────────────
echo -e "${BLUE}[1/5]${NC} Checking Python..."

if command -v python3 &>/dev/null && python3 -c "import sys; exit(0 if sys.version_info >= (3,8) else 1)" 2>/dev/null; then
    PYTHON="python3"
    ok "$($PYTHON --version 2>&1)"
elif command -v python &>/dev/null && python -c "import sys; exit(0 if sys.version_info >= (3,8) else 1)" 2>/dev/null; then
    PYTHON="python"
    ok "$($PYTHON --version 2>&1)"
else
    info "Python 3.8+ not found, installing..."
    case "$PLATFORM" in
        termux)  install_packages termux python python-pip ;;
        proot)   install_packages proot python3 python3-pip python3-venv ;;
        debian)  install_packages debian python3 python3-pip python3-venv ;;
        fedora)  install_packages fedora python3 python3-pip ;;
        centos)  install_packages centos python3 python3-pip ;;
        alpine)  install_packages alpine python3 py3-pip ;;
        arch)    install_packages arch python python-pip ;;
        opensuse) install_packages opensuse python3 python3-pip ;;
        macos)   install_packages macos python ;;
        *)       warn "Unknown platform — please install Python 3.8+ manually" ;;
    esac

    # Re-detect after install
    if command -v python3 &>/dev/null; then
        PYTHON="python3"
    elif command -v python &>/dev/null; then
        PYTHON="python"
    fi

    if [ -n "$PYTHON" ]; then
        ok "$($PYTHON --version 2>&1)"
    else
        fail "Python installation failed — please install Python 3.8+ manually"
        echo ""
        info "Try:"
        echo -e "  ${CYAN}apt-get install python3 python3-pip${NC}  (proot/debian)"
        echo -e "  ${CYAN}pkg install python python-pip${NC}      (Termux)"
        exit 1
    fi
fi

# ── Step 2/5: Install pip + dependencies ────────────────
echo ""
echo -e "${BLUE}[2/5]${NC} Installing dependencies..."

# Ensure pip
if ! $PYTHON -m pip --version &>/dev/null 2>&1; then
    info "Installing pip..."
    case "$PLATFORM" in
        termux)
            pkg install -y python-pip || true
            ;;
        proot)
            apt-get install -y python3-pip || \
            curl -sS https://bootstrap.pypa.io/get-pip.py | $PYTHON || \
            $PYTHON -m ensurepip --upgrade || true
            ;;
        macos)
            # pip should come with python on macOS
            ;;
        *)
            curl -sS https://bootstrap.pypa.io/get-pip.py | $PYTHON || \
            $PYTHON -m ensurepip --upgrade || true
            ;;
    esac
fi

if ! $PYTHON -m pip --version &>/dev/null 2>&1; then
    warn "pip not available — trying alternative install..."
fi

# Install flask + flask-cors
# Use --break-system-packages on Debian/Ubuntu 12+ and proot where externally managed env blocks pip
FLASK_OK=false
if [ "$PLATFORM" = "debian" ] || [ "$PLATFORM" = "proot" ] || [ "$PLATFORM" = "alpine" ]; then
    $PYTHON -m pip install --break-system-packages flask flask-cors 2>&1 && FLASK_OK=true
    if ! $FLASK_OK; then
        $PYTHON -m pip install flask flask-cors 2>&1 && FLASK_OK=true
    fi
    if ! $FLASK_OK; then
        $PYTHON -m pip install --user flask flask-cors 2>&1 && FLASK_OK=true
    fi
else
    $PYTHON -m pip install flask flask-cors 2>&1 && FLASK_OK=true
    if ! $FLASK_OK; then
        $PYTHON -m pip install --user flask flask-cors 2>&1 && FLASK_OK=true
    fi
fi

if $FLASK_OK; then
    ok "flask + flask-cors"
else
    warn "pip install may have failed — will verify in Step 4"
fi

# ── Git clone with retry + mirror fallback ───────────
CLONE_URLS=(
    "https://github.com/ctz168/muside.git"
    "https://ghfast.top/https://github.com/ctz168/muside.git"
    "https://gh-proxy.com/https://github.com/ctz168/muside.git"
    "https://mirror.ghproxy.com/https://github.com/ctz168/muside.git"
)

# ── Step 3/5: Clone repo ────────────────────────────
echo ""
echo -e "${BLUE}[3/5]${NC} Setting up MusIDE IDE..."

# Ensure git is available
if ! command -v git &>/dev/null; then
    info "git not found, installing..."
    case "$PLATFORM" in
        termux)  pkg install -y git ;;
        proot)   apt-get update -qq; apt-get install -y git ;;
        debian)  install_packages debian git ;;
        fedora)  install_packages fedora git ;;
        centos)  install_packages centos git ;;
        alpine)  install_packages alpine git ;;
        arch)    install_packages arch git ;;
        opensuse) install_packages opensuse git ;;
        macos)   brew install git ;;
    esac
fi

# Clone or update
if [ -d "$INSTALL_DIR/.git" ]; then
    info "Updating existing installation..."
    cd "$INSTALL_DIR"
    git pull --ff-only 2>&1 || warn "git pull failed — using existing files"
else
    if [ -d "$INSTALL_DIR" ]; then
        warn "Directory $INSTALL_DIR exists but is not a git repo"
        INSTALL_DIR="${INSTALL_DIR}-$(date +%s)"
        warn "Using $INSTALL_DIR instead"
    fi

    # Try cloning with retries and mirror fallback
    CLONE_OK=false
    for url in "${CLONE_URLS[@]}"; do
        for attempt in 1 2 3; do
            info "Cloning from ${url%%//*/}//... (attempt $attempt/3)..."
            CLONE_ERR=$(git clone --depth 1 "$url" "$INSTALL_DIR" 2>&1) && {
                CLONE_OK=true
                break 2
            }
            # Show the actual error on last attempt for this URL
            if [ $attempt -eq 3 ]; then
                warn "Failed with ${url%%\/*} — $(echo "$CLONE_ERR" | tail -1)"
            else
                sleep 2
            fi
        done
        $CLONE_OK && break
        # Clean up failed partial clone
        rm -rf "$INSTALL_DIR"
    done

    if ! $CLONE_OK; then
        fail "All clone attempts failed."
        fail "Last error: $(echo "$CLONE_ERR" | tail -3)"
        echo ""
        info "Try manually:"
        echo -e "  ${CYAN}git clone https://github.com/ctz168/muside.git ~/muside-ide${NC}"
        echo -e "  ${CYAN}cd ~/muside-ide && python3 muside_server.py${NC}"
        exit 1
    fi

    # Normalize remote to official GitHub (in case we cloned via mirror)
    if [ "$url" != "https://github.com/ctz168/muside.git" ]; then
        cd "$INSTALL_DIR"
        git remote set-url origin https://github.com/ctz168/muside.git 2>/dev/null || true
        info "Remote set to official GitHub URL"
    fi
fi

# ── Step 4/5: Final verification in target environment ──
echo ""
echo -e "${BLUE}[4/5]${NC} Verifying in target environment..."

# Re-check flask import with the actual python3 that will run the server
VERIFY_FAILED=false
if ! python3 -c "import flask" 2>/dev/null; then
    info "flask not found in current python3 — installing..."
    if command -v pip3 &>/dev/null; then
        pip3 install --break-system-packages flask flask-cors 2>&1 || \
        pip3 install flask flask-cors 2>&1 || \
        pip3 install --user flask flask-cors 2>&1 || VERIFY_FAILED=true
    elif command -v pip &>/dev/null; then
        pip install --break-system-packages flask flask-cors 2>&1 || \
        pip install flask flask-cors 2>&1 || \
        pip install --user flask flask-cors 2>&1 || VERIFY_FAILED=true
    else
        # Try via python3 -m pip
        python3 -m pip install --break-system-packages flask flask-cors 2>&1 || \
        python3 -m pip install flask flask-cors 2>&1 || \
        { python3 -m ensurepip 2>/dev/null && python3 -m pip install flask flask-cors 2>&1; } || \
        VERIFY_FAILED=true
    fi
    if $VERIFY_FAILED; then
        # Last resort: apt-get (works in proot Ubuntu and regular Debian)
        info "Trying apt-get as fallback..."
        if command -v apt-get &>/dev/null; then
            if need_sudo; then
                sudo apt-get update -qq 2>/dev/null
                sudo apt-get install -y python3-flask python3-flask-cors 2>/dev/null || \
                { sudo apt-get install -y python3-pip 2>/dev/null && pip3 install flask flask-cors 2>/dev/null; } || \
                    warn "Could not install flask automatically"
            else
                apt-get update -qq 2>/dev/null
                apt-get install -y python3-flask python3-flask-cors 2>/dev/null || \
                { apt-get install -y python3-pip 2>/dev/null && pip3 install flask flask-cors 2>/dev/null; } || \
                    warn "Could not install flask automatically"
            fi
        else
            warn "Could not install flask automatically"
        fi
    else
        ok "flask installed"
    fi
else
    ok "flask $(python3 -c 'import flask; print(flask.__version__)' 2>/dev/null)"
fi

if ! python3 -c "import flask_cors" 2>/dev/null; then
    info "flask-cors missing — installing..."
    pip3 install --break-system-packages flask-cors 2>/dev/null || \
    pip3 install flask-cors 2>/dev/null || \
    python3 -m pip install flask-cors 2>/dev/null || true
fi

# Final smoke test
SMOKE_OK=false
if python3 -c "from flask import Flask; from flask_cors import CORS; print('OK')" 2>/dev/null; then
    ok "All dependencies ready"
    SMOKE_OK=true
else
    warn "Flask import still fails — you may need to run:"
    echo -e "  ${CYAN}pip3 install flask flask-cors${NC}"
    echo ""
fi

# Create workspace & config dirs
mkdir -p "$HOME/muside_workspace"
mkdir -p "$HOME/.muside"

ok "Ready at $INSTALL_DIR"

# ── Step 5/5: Auto-start server & open browser ──────────
echo ""
echo -e "${BLUE}[5/5]${NC} Launching MusIDE IDE..."

cd "$INSTALL_DIR" || {
    fail "Cannot cd to $INSTALL_DIR"
    info "Try manually: cd $INSTALL_DIR && python3 muside_server.py"
    exit 1
}

# Detect local IP for display
LOCAL_IP=""
if command -v hostname &>/dev/null; then
    LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
fi
if [ -z "$LOCAL_IP" ]; then
    LOCAL_IP=$(ip route get 1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' 2>/dev/null)
fi
if [ -z "$LOCAL_IP" ]; then
    LOCAL_IP="localhost"
fi

# Dynamically detect the server port from utils.py
# Respects MUSIDE_PORT env var if set by the user
IDE_PORT=$(python3 -c "
import os, sys
sys.path.insert(0, '.')
from utils import PORT
print(PORT)
" 2>/dev/null) || IDE_PORT=${MUSIDE_PORT:-12346}

info "Detected server port: $IDE_PORT"

IDE_URL="http://${LOCAL_IP}:${IDE_PORT}"
IDE_LOCAL="http://localhost:${IDE_PORT}"

# Kill any existing server on this port (leftover from previous background run)
if command -v lsof &>/dev/null; then
    OLD_PID=$(lsof -ti :$IDE_PORT 2>/dev/null)
    if [ -n "$OLD_PID" ]; then
        info "Killing old server on port $IDE_PORT (PID: $OLD_PID)..."
        kill $OLD_PID 2>/dev/null
        sleep 1
        kill -9 $OLD_PID 2>/dev/null
    fi
elif command -v fuser &>/dev/null; then
    OLD_PID=$(fuser $IDE_PORT/tcp 2>/dev/null)
    if [ -n "$OLD_PID" ]; then
        info "Killing old server on port $IDE_PORT (PID: $OLD_PID)..."
        kill $OLD_PID 2>/dev/null
        sleep 1
        kill -9 $OLD_PID 2>/dev/null
    fi
elif command -v ss &>/dev/null; then
    OLD_PID=$(ss -tlnp "sport = :$IDE_PORT" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1)
    if [ -n "$OLD_PID" ]; then
        info "Killing old server on port $IDE_PORT (PID: $OLD_PID)..."
        kill $OLD_PID 2>/dev/null
        sleep 1
        kill -9 $OLD_PID 2>/dev/null
    fi
fi

# Start server in foreground (so Ctrl+C can stop it)
# First, open browser in background after a short delay
(sleep 2 && (
    case "$PLATFORM" in
        termux)
            if command -v termux-open-url &>/dev/null; then
                termux-open-url "$IDE_URL" 2>/dev/null
            elif command -v xdg-open &>/dev/null; then
                xdg-open "$IDE_URL" 2>/dev/null
            fi
            ;;
        proot)
            # In proot Ubuntu, try termux-open-url first (works with Termux:API),
            # then fall back to xdg-open or sensible-browser
            if command -v termux-open-url &>/dev/null; then
                termux-open-url "$IDE_URL" 2>/dev/null
            elif command -v xdg-open &>/dev/null; then
                xdg-open "$IDE_URL" 2>/dev/null
            elif command -v sensible-browser &>/dev/null; then
                sensible-browser "$IDE_URL" 2>/dev/null
            fi
            ;;
        macos)
            open "$IDE_URL" 2>/dev/null
            ;;
        *)
            if command -v xdg-open &>/dev/null; then
                xdg-open "$IDE_URL" 2>/dev/null
            elif command -v sensible-browser &>/dev/null; then
                sensible-browser "$IDE_URL" 2>/dev/null
            elif command -v gnome-open &>/dev/null; then
                gnome-open "$IDE_URL" 2>/dev/null
            elif command -v python3 &>/dev/null; then
                python3 -m webbrowser "$IDE_URL" 2>/dev/null
            fi
            ;;
    esac
)) &
BROWSER_OPENER_PID=$!

echo ""
echo -e "${GREEN}══════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}  MusIDE IDE is ready!${NC}"
echo -e "${GREEN}══════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Local:    ${CYAN}${IDE_LOCAL}${NC}"
echo -e "  Network:  ${CYAN}${IDE_URL}${NC}"
echo -e "  Dir:      ${CYAN}${INSTALL_DIR}${NC}"
echo ""
echo -e "  ${YELLOW}Press Ctrl+C to stop the server${NC}"
echo ""

# Run server in foreground — Ctrl+C will stop it
# Set PYTHONIOENCODING=utf-8 to prevent encoding errors on some systems
export PYTHONIOENCODING=utf-8

# Try to start the server, with helpful error message if it fails
if ! python3 muside_server.py; then
    EXIT_CODE=$?
    echo ""
    fail "Server exited with code $EXIT_CODE"
    echo ""
    info "Possible reasons:"
    echo "  1. Flask is not installed correctly"
    echo "  2. Port $IDE_PORT is already in use"
    echo "  3. Python version is too old (need 3.8+)"
    echo ""
    info "Try these commands:"
    echo -e "  ${CYAN}pip3 install --break-system-packages flask flask-cors${NC}"
    echo -e "  ${CYAN}cd $INSTALL_DIR && python3 muside_server.py${NC}"
    echo ""
    exit $EXIT_CODE
fi
