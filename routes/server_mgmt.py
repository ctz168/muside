"""
MusIDE - Server Management API routes.
"""

import os
import sys
import json
import subprocess
import time
import threading
from datetime import datetime
from flask import Blueprint, jsonify, request, Response
from utils import (
    handle_error, load_config, WORKSPACE, SERVER_DIR, PORT, HOST, CONFIG_DIR,
    running_processes, _log_buffer, _log_lock, log_write,
)

bp = Blueprint('server_mgmt', __name__)

# ==================== Server State ====================
SERVER_START_TIME = time.time()
SERVER_VERSION = '2.0.0'


# ---- Health Check API ----
@bp.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({'status': 'ok', 'version': '1.0.0', 'port': PORT})


# ---- Config APIs ----
@bp.route('/api/config', methods=['GET'])
@handle_error
def get_config():
    from utils import load_config as _load_config
    return jsonify(_load_config())


@bp.route('/api/config', methods=['POST'])
@handle_error
def update_config():
    from utils import save_config as _save_config, load_config as _load_config

    data = request.json
    config = _load_config()
    config.update(data)
    _save_config(config)
    if config.get('workspace'):
        os.makedirs(config['workspace'], exist_ok=True)
    return jsonify({'ok': True})


# ---- Server Status ----
@bp.route('/api/server/status', methods=['GET'])
@handle_error
def server_status():
    """Returns server uptime, version, process info, memory usage, connected processes count."""
    import resource
    uptime = time.time() - SERVER_START_TIME
    hours, remainder = divmod(int(uptime), 3600)
    minutes, seconds = divmod(remainder, 60)

    mem_info = {}
    try:
        rusage = resource.getrusage(resource.RUSAGE_SELF)
        mem_info['rss_mb'] = round(rusage.ru_maxrss / 1024, 1)  # Linux: ru_maxrss is in KB
    except Exception:
        try:
            mem_info['rss_mb'] = round(os.popen('ps -o rss= -p ' + str(os.getpid())).read().strip().split()[0] / 1024, 1)
        except Exception:
            mem_info['rss_mb'] = 'unknown'

    active_procs = sum(1 for p in running_processes.values() if p.get('running', False))

    from utils import load_config as _load_config
    return jsonify({
        'status': 'running',
        'version': SERVER_VERSION,
        'uptime_seconds': int(uptime),
        'uptime': f'{hours}h {minutes}m {seconds}s',
        'pid': os.getpid(),
        'port': PORT,
        'host': HOST,
        'python': sys.version.split()[0],
        'platform': sys.platform,
        'workspace': _load_config().get('workspace', WORKSPACE),
        'memory': mem_info,
        'connected_processes': active_procs,
        'config_dir': CONFIG_DIR,
        'server_dir': SERVER_DIR,
    })


# ---- Server Restart ----
@bp.route('/api/server/restart', methods=['POST'])
@handle_error
def server_restart():
    """Restart the Flask server by spawning a new process and exiting current."""
    from utils import load_config as _load_config

    # Write a marker file so the new process knows it's a restart
    marker = os.path.join(CONFIG_DIR, 'restart_marker.json')
    with open(marker, 'w', encoding='utf-8') as f:
        json.dumps({'pid': os.getpid(), 'time': datetime.now().isoformat()}, f)

    log_write('[SERVER] Restart requested, spawning new process...')

    # Spawn new server process
    server_script = os.path.join(SERVER_DIR, 'muside_server.py')
    env = os.environ.copy()
    env['MUSIDE_WORKSPACE'] = _load_config().get('workspace', WORKSPACE)
    env['MUSIDE_PORT'] = str(PORT)
    env['MUSIDE_HOST'] = HOST

    try:
        subprocess.Popen(
            [sys.executable, server_script],
            env=env,
            stdout=open(os.path.join(CONFIG_DIR, 'server.log'), 'a', encoding='utf-8'),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return jsonify({'ok': True, 'message': 'Server restarting...'})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    finally:
        # Exit current process after a short delay
        def _exit():
            time.sleep(0.5)
            os._exit(0)
        threading.Thread(target=_exit, daemon=True).start()


# ---- Server Logs ----
@bp.route('/api/server/logs', methods=['POST'])
@handle_error
def server_logs():
    """Returns last N lines of server output log."""
    data = request.json or {}
    count = data.get('count', 100)
    log_file = os.path.join(CONFIG_DIR, 'server.log')

    if not os.path.exists(log_file):
        return jsonify({'lines': [], 'source': 'memory', 'total': len(_log_buffer)})

    try:
        with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
            all_lines = f.readlines()
        last_n = all_lines[-count:]
        return jsonify({
            'lines': [line.rstrip('\n') for line in last_n],
            'source': 'file',
            'total': len(all_lines),
        })
    except Exception as e:
        # Fall back to memory buffer
        mem_lines = [f'{e["time"]} {e["text"]}' for e in list(_log_buffer)[-count:]]
        return jsonify({'lines': mem_lines, 'source': 'memory', 'total': len(_log_buffer)})


@bp.route('/api/server/logs/stream', methods=['GET'])
def server_logs_stream():
    """SSE stream of server log output in real-time."""
    log_file = os.path.join(CONFIG_DIR, 'server.log')

    def generate():
        if os.path.exists(log_file):
            file_size = os.path.getsize(log_file)
        else:
            file_size = 0

        idx = len(_log_buffer)

        while True:
            # Check memory buffer for new entries
            if idx < len(_log_buffer):
                with _log_lock:
                    entries = list(_log_buffer)
                for entry in entries[idx:]:
                    yield f"data: {json.dumps({'type': 'log', 'text': entry['text'], 'time': entry['time']})}\n\n"
                idx = len(entries)

            # Check log file for new content
            if os.path.exists(log_file):
                new_size = os.path.getsize(log_file)
                if new_size > file_size:
                    try:
                        with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                            f.seek(file_size)
                            new_content = f.read()
                        for line in new_content.split('\n'):
                            line = line.strip()
                            if line:
                                yield f"data: {json.dumps({'type': 'log', 'text': line, 'time': datetime.now().isoformat()})}\n\n"
                        file_size = new_size
                    except Exception:
                        pass

            time.sleep(0.5)

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ---- System Info ----
@bp.route('/api/system/info', methods=['GET'])
def system_info():
    from utils import load_config as _load_config, IS_WINDOWS, PLATFORM_NAME, PLATFORM_DETAIL, IS_TERMUX

    info = {
        'platform': PLATFORM_NAME,
        'platform_detail': PLATFORM_DETAIL,
        'is_windows': IS_WINDOWS,
        'is_termux': IS_TERMUX,
        'workspace': _load_config().get('workspace', WORKSPACE),
        'pid': os.getpid(),
        'server_version': SERVER_VERSION,
    }
    # On Linux/Termux, also get kernel info
    if not IS_WINDOWS:
        try:
            result = subprocess.run('uname -a', shell=True, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                info['uname'] = result.stdout.strip()
        except Exception:
            pass
    return jsonify(info)


# ---- Platform Info for Frontend ----
@bp.route('/api/platform/info', methods=['GET'])
@handle_error
def platform_info():
    """Return platform info for frontend to adapt shell/terminal behavior."""
    from utils import IS_WINDOWS, IS_TERMUX, IS_MACOS, PLATFORM_NAME, get_default_shell, get_default_compiler

    return jsonify({
        'is_windows': IS_WINDOWS,
        'is_macos': IS_MACOS,
        'is_linux': not IS_WINDOWS and not IS_MACOS,
        'is_termux': IS_TERMUX,
        'platform': PLATFORM_NAME,
        'default_shell': get_default_shell(),
        'default_compiler': get_default_compiler(),
        'shell_prompt': '>' if IS_WINDOWS else '$',
    })
