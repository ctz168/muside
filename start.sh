#!/bin/bash
# MusIDE - Start Script
# Usage: bash start.sh [port]

PORT=${1:-12346}
HOST="0.0.0.0"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Activate virtual environment if exists
if [ -f "$HOME/muside_workspace/.venv/bin/activate" ]; then
    source "$HOME/muside_workspace/.venv/bin/activate"
    echo "[INFO] Activated virtual environment"
fi

# ── Check and install audio analysis dependencies if missing ──
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
        pip3 install torch torchaudio --index-url https://download.pytorch.org/whl/cpu 2>/dev/null || \
        pip3 install torch torchaudio 2>/dev/null || \
        echo "[WARN] torch/torchaudio install failed — audio analysis will be limited"
        # Remove torch from missing list
        MISSING_PKGS=$(echo "$MISSING_PKGS" | sed 's/torch//g; s/torchaudio//g')
    fi
    if [ -n "$MISSING_PKGS" ]; then
        pip3 install $MISSING_PKGS 2>/dev/null || \
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
