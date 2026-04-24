#!/usr/bin/env python3
"""
MusIDE - Mobile-Optimized Web IDE for Termux/Ubuntu
Lightweight Python server (default port: 12346, configurable via MUSIDE_PORT env)

Refactored: routes split into routes/ directory, shared utilities in utils.py.
"""

import os
import sys
from flask import Flask, send_from_directory, jsonify, make_response, request
from flask_cors import CORS

# ==================== Create App ====================
from utils import SERVER_DIR, WORKSPACE, PORT, HOST, CONFIG_DIR, CHAT_HISTORY_FILE

app = Flask(__name__, static_folder=os.path.join(SERVER_DIR, 'static'), static_url_path=None)
app.url_map.strict_slashes = False
CORS(app)

# Ensure all API errors return JSON, not HTML
@app.errorhandler(Exception)
def handle_unhandled_exception(e):
    """Global error handler — always return JSON for API routes."""
    import traceback as _tb
    _tb.print_exc()
    return jsonify({'error': str(e)}), 500

# Handle 405 specifically — show the real error, not a wrapper
@app.errorhandler(405)
def handle_method_not_allowed(e):
    import traceback as _tb
    _tb.print_exc()
    return jsonify({'error': f'405 Method Not Allowed: {e.description or request.method}', 'url': request.path, 'method': request.method}), 405

# Ensure directories exist
os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(WORKSPACE, exist_ok=True)

# ==================== Register Blueprints ====================
from routes.files import bp as files_bp
from routes.run import bp as run_bp
from routes.git import bp as git_bp
from routes.chat import bp as chat_bp
from routes.venv import bp as venv_bp
try:
    from routes.update import bp as update_bp
except Exception as e:
    print(f"[WARN] Failed to load update module: {e}")
    update_bp = None
from routes.server_mgmt import bp as server_mgmt_bp
from routes.browser import bp as browser_bp
from routes.debug import bp as debug_bp
from routes.audio import bp as audio_bp

app.register_blueprint(files_bp)
app.register_blueprint(run_bp)
app.register_blueprint(git_bp)
app.register_blueprint(chat_bp)
app.register_blueprint(venv_bp)
if update_bp:
    app.register_blueprint(update_bp)
app.register_blueprint(server_mgmt_bp)
app.register_blueprint(browser_bp)
app.register_blueprint(debug_bp)
app.register_blueprint(audio_bp)

# ==================== Frontend Serving ====================
# static_url_path=None: disable Flask's built-in static route to avoid
# route conflicts with POST/PUT/DELETE API blueprints.
# Instead we serve static files manually with an explicit GET-only route.

@app.route('/')
def index():
    resp = make_response(send_from_directory(app.static_folder, 'index.html'))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp

@app.route('/<path:path>', methods=['GET'])
def static_files(path):
    # Don't intercept routes handled by blueprints (API, preview, etc.)
    # This prevents the catch-all static route from catching /preview/... URLs
    # which should be handled by the files blueprint.
    if path.startswith('api/') or path.startswith('preview/'):
        # Let Flask's 404 handler deal with it — blueprint routes will match first
        return jsonify({'error': 'Not found'}), 404
    try:
        resp = make_response(send_from_directory(app.static_folder, path))
        resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return resp
    except Exception:
        return jsonify({'error': 'File not found'}), 404

# ==================== Main ====================
if __name__ == '__main__':
    # Fix Windows encoding issues: set stdout/stderr to UTF-8
    # On Chinese Windows, the default encoding is GBK which cannot handle
    # emoji characters (e.g. 🚀 U+1F680) that LLM models often output.
    if sys.platform == 'win32':
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass  # reconfigure not available in older Python

    # Ensure workspace exists
    os.makedirs(WORKSPACE, exist_ok=True)

    # Set up log file
    from utils import log_write
    _log_file_path = os.path.join(CONFIG_DIR, 'server.log')
    _log_fh = open(_log_file_path, 'a', encoding='utf-8')
    _log_fh.write(f'\n--- MusIDE Server starting at {__import__("datetime").datetime.now().isoformat()} ---\n')
    _log_fh.flush()

    # Redirect stdout/stderr to log file while keeping console output
    import io

    class _TeeStream:
        """Tee output to both file and console.
        
        On Windows, the console (sys.__stdout__) typically uses GBK encoding
        which cannot handle emoji characters (U+1F680 etc.). This class catches
        UnicodeEncodeError and replaces unencodable chars with '?'.
        """
        def __init__(self, *targets):
            self.targets = targets
            self._lock = __import__('threading').Lock()
        def _safe_write(self, target, data):
            """Write data to target, handling UnicodeEncodeError on Windows."""
            try:
                target.write(data)
            except UnicodeEncodeError:
                # Replace unencodable chars (emoji etc.) with '?' for GBK consoles
                try:
                    target.write(data.encode(target.encoding or 'utf-8', errors='replace').decode(target.encoding or 'utf-8', errors='replace'))
                except Exception:
                    target.write(data.encode('ascii', errors='replace').decode('ascii'))
            except Exception:
                pass
        def write(self, data):
            with self._lock:
                for t in self.targets:
                    self._safe_write(t, data)
                    try:
                        t.flush()
                    except Exception:
                        pass
                log_write(data.rstrip('\n'))
        def flush(self):
            with self._lock:
                for t in self.targets:
                    try:
                        t.flush()
                    except Exception:
                        pass
        def isatty(self):
            return False

    sys.stdout = _TeeStream(sys.__stdout__, _log_fh)
    sys.stderr = _TeeStream(sys.__stderr__, _log_fh)

    from utils import SERVER_DIR as _SD, PORT as _P, HOST as _H, load_config, shlex_quote
    print(f"""
    ╔══════════════════════════════════╗
    ║       MusIDE Server           ║
    ║   Mobile Web IDE for Termux     ║
    ╠══════════════════════════════════╣
    ║  Port:    {_P:<22}║
    ║  Host:    {_H:<22}║
    ║  Workspace: {os.path.basename(WORKSPACE):<18}║
    ║  URL:     http://localhost:{_P:<8}║
    ║  Source:  ctz168/muside              ║
    ╚══════════════════════════════════╝
    """)

    # Initialize git if needed (only for non-workspace dirs, skip workspace git init)
    # Git init is now handled per-project via the Project panel
    log_write(f'[SERVER] Starting on {HOST}:{PORT}, workspace: {WORKSPACE}')

    # Suppress Werkzeug's per-request log lines (e.g. "GET /api/browser/poll HTTP/1.1" 200)
    import logging
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    app.run(host=HOST, port=PORT, debug=False, threaded=True)
