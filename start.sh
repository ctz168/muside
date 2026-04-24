#!/bin/bash
# MusIDE - Start Script
# Usage: bash start.sh [port]

PORT=${1:-12346}
HOST="0.0.0.0"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# ── Setup virtual environment (avoids PEP 668 externally-managed-environment errors) ──
if [ ! -d "$VENV_DIR" ] || [ ! -f "$VENV_DIR/bin/python3" ]; then
    echo "[INFO] Creating virtual environment..."
    python3 -m venv "$VENV_DIR" 2>/dev/null || {
        # venv module might need python3-venv package
        echo "[INFO] venv module not found, installing python3-venv..."
        if command -v apt-get &>/dev/null; then
            apt-get update -qq 2>/dev/null
            apt-get install -y python3-venv 2>/dev/null
        fi
        python3 -m venv "$VENV_DIR" || {
            echo "[ERROR] Cannot create virtual environment. Please run:"
            echo "  apt-get install python3-venv"
            echo "  python3 -m venv $VENV_DIR"
            exit 1
        }
    }
fi

# Activate virtual environment
source "$VENV_DIR/bin/activate"
echo "[INFO] Virtual environment activated: $VENV_DIR"

# Upgrade pip in venv
pip install --upgrade pip --quiet 2>/dev/null

# ── Install core dependencies in venv ──
if ! python3 -c "import flask" 2>/dev/null; then
    echo "[INFO] Installing core dependencies (flask, flask-cors)..."
    pip install flask flask-cors 2>/dev/null || {
        echo "[ERROR] Failed to install core dependencies"
        exit 1
    }
fi

# ── Check and install audio analysis dependencies ──
echo "[INFO] Checking audio analysis dependencies..."
MISSING_PKGS=""

if ! python3 -c "import torch" 2>/dev/null; then
    MISSING_PKGS="$MISSING_PKGS torch torchaudio"
fi
if ! python3 -c "from demucs.pretrained import get_model" 2>/dev/null; then
    MISSING_PKGS="$MISSING_PKGS demucs"
fi
if ! python3 -c "import whisper" 2>/dev/null; then
    MISSING_PKGS="$MISSING_PKGS openai-whisper"
fi

if [ -n "$MISSING_PKGS" ]; then
    echo "[INFO] Installing missing audio analysis dependencies:$MISSING_PKGS"
    echo "[INFO] This may take a few minutes on first run..."

    # Install torch with CPU-only index first for smaller download
    if echo "$MISSING_PKGS" | grep -q "torch"; then
        echo "[INFO] Installing PyTorch (CPU version)..."
        pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu 2>/dev/null || \
        pip install torch torchaudio 2>/dev/null || \
        echo "[WARN] torch/torchaudio install failed — audio analysis will be limited"
        # Remove torch from missing list
        MISSING_PKGS=$(echo "$MISSING_PKGS" | sed 's/torch//g; s/torchaudio//g')
    fi

    # Install remaining packages (demucs, whisper)
    if [ -n "$(echo $MISSING_PKGS | xargs)" ]; then
        pip install $MISSING_PKGS 2>/dev/null || \
        echo "[WARN] Some dependencies failed to install — audio analysis may be limited"
    fi
else
    echo "[INFO] All audio analysis dependencies are installed"
fi

# Check if port is in use
if command -v lsof &> /dev/null; then
    PID=$(lsof -ti:$PORT 2>/dev/null)
    if [ -n "$PID" ]; then
        echo "[WARN] Port $PORT is in use (PID: $PID)"
        read -p "Kill process and continue? (y/n) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            kill -9 $PID 2>/dev/null || true
            sleep 1
        else
            echo "Aborted."
            exit 1
        fi
    fi
fi

echo "Starting MusIDE on http://localhost:$PORT ..."

# Start server
cd "$SCRIPT_DIR"
export MUSIDE_PORT=$PORT
python3 muside_server.py &
SERVER_PID=$!

# Open browser if possible
if command -v termux-open-url &> /dev/null; then
    sleep 2
    termux-open-url "http://localhost:$PORT"
elif command -v xdg-open &> /dev/null; then
    sleep 2
    xdg-open "http://localhost:$PORT" 2>/dev/null &
fi

# Wait for server
wait $SERVER_PID
