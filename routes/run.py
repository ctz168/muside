"""
MusIDE - Execution / Run API routes.
"""

import os
import re
import json
import time
import subprocess as sp
from flask import Blueprint, jsonify, request, Response
from utils import (
    handle_error, load_config, WORKSPACE, shlex_quote,
    run_process, stop_process, running_processes, process_outputs,
    _verify_process_state, IS_WINDOWS, get_default_compiler,
)

bp = Blueprint('run', __name__)

# IDE's own port — never kill this
_IDE_PORT = int(os.environ.get('MUSIDE_PORT', 12345))


def _extract_ports_from_code(code_text):
    """Extract port numbers from Python code.
    Detects patterns like:
      port=5000, port = 8080, app.run(port=3000)
      .listen(3000), HOST:5000, 0.0.0.0:8000
      socket.bind(('0.0.0.0', 9000))
    """
    ports = set()
    # Pattern 1: port=NNNN or port = NNNN (most common: Flask, Django, etc.)
    for m in re.finditer(r'port\s*=\s*(\d{2,5})', code_text):
        port = int(m.group(1))
        if 10 <= port <= 65535:
            ports.add(port)
    # Pattern 2: host:port pattern like '0.0.0.0:8000' or 'localhost:5000'
    for m in re.finditer(r'(?:\d+\.\d+\.\d+\.\d+|localhost):(\d{2,5})', code_text):
        port = int(m.group(1))
        if 10 <= port <= 65535:
            ports.add(port)
    # Pattern 3: .listen(NNNN) (Node.js/Express style)
    for m in re.finditer(r'\.listen\s*\(\s*(\d{2,5})', code_text):
        port = int(m.group(1))
        if 10 <= port <= 65535:
            ports.add(port)
    return ports


def _kill_port_occupants(ports):
    """Kill processes occupying the given ports. Returns list of killed info."""
    killed = []
    ide_port = _IDE_PORT
    for port in ports:
        if port == ide_port:
            continue  # Never kill IDE's own port
        if IS_WINDOWS:
            try:
                result = sp.run(
                    f'netstat -ano | findstr :{port} | findstr LISTENING',
                    shell=True, capture_output=True, text=True, timeout=5
                )
                for line in result.stdout.strip().splitlines():
                    parts = line.strip().split()
                    if parts:
                        pid = parts[-1]
                        try:
                            sp.run(f'taskkill /F /PID {pid}', shell=True, capture_output=True, timeout=5)
                            killed.append({'port': port, 'pid': pid})
                        except Exception:
                            pass
            except Exception:
                pass
        else:
            try:
                result = sp.run(
                    f'lsof -ti :{port}', shell=True, capture_output=True, text=True, timeout=5
                )
                for pid_str in result.stdout.strip().splitlines():
                    pid_str = pid_str.strip()
                    if pid_str:
                        try:
                            os.kill(int(pid_str), 9)
                            killed.append({'port': port, 'pid': pid_str})
                        except (OSError, ValueError):
                            pass
            except Exception:
                pass
        # Also stop any of our managed processes that might be using this port
        for proc_id, info in list(running_processes.items()):
            if info.get('running') and str(port) in info.get('cmd', ''):
                stop_process(proc_id)
                killed.append({'port': port, 'managed_proc': proc_id})
    return killed


@bp.route('/api/run/execute', methods=['POST'])
@handle_error
def execute_code():
    data = request.json
    code = data.get('code', '')
    file_path = data.get('file_path', '')
    compiler = data.get('compiler', '') or get_default_compiler()
    args = data.get('args', '')
    config = load_config()
    base = config.get('workspace', WORKSPACE)

    # When a project is open, run code in the project directory
    project = config.get('project', None)
    if project:
        project_dir = os.path.join(base, project)
        if os.path.isdir(project_dir):
            base = project_dir

    # Warn if no venv is configured (helpful for Python projects)
    no_venv = False
    if compiler in ('python3', 'python') and not config.get('venv_path'):
        no_venv = True

    # ── Auto-detect ports and kill occupants ──
    # Read the source code to find port numbers, then kill any processes
    # occupying those ports so the new process can start cleanly.
    killed_ports = []
    source_text = code  # start with inline code if provided
    if file_path:
        # file_path can be relative to workspace or project
        target = os.path.realpath(os.path.join(config.get('workspace', WORKSPACE), file_path))
        ws = os.path.realpath(config.get('workspace', WORKSPACE))
        if not target.startswith(ws):
            return jsonify({'error': 'Access denied'}), 403
        # Also read the file content for port detection
        try:
            with open(target, 'r', encoding='utf-8', errors='ignore') as f:
                source_text = f.read()
        except Exception:
            source_text = code
        cmd = f'{compiler} {shlex_quote(target)} {args}'
    else:
        # Write temp file in the effective base (project dir or workspace)
        tmp_file = os.path.join(base, '.muside_tmp.py')
        with open(tmp_file, 'w', encoding='utf-8') as f:
            f.write(code)
        cmd = f'{compiler} {shlex_quote(tmp_file)} {args}'

    # Detect ports from source code + args
    detected_ports = _extract_ports_from_code(source_text)
    # Also check args for --port NNNN pattern
    for m in re.finditer(r'(?:--port|-p)\s+(\d{2,5})', args):
        port = int(m.group(1))
        if 10 <= port <= 65535:
            detected_ports.add(port)

    if detected_ports:
        killed_ports = _kill_port_occupants(detected_ports)
        # Small delay to let OS release the port
        if killed_ports:
            time.sleep(0.3)

    proc_id = run_process(cmd, cwd=base)
    result = {'ok': True, 'proc_id': proc_id, 'no_venv': no_venv, 'cwd': base}
    if detected_ports:
        result['detected_ports'] = sorted(detected_ports)
    if killed_ports:
        result['killed_ports'] = killed_ports
    return jsonify(result)


@bp.route('/api/run/shell', methods=['POST'])
@handle_error
def execute_shell():
    """Execute a raw shell command directly (not as code file).
    Used by the terminal/shell input bar for commands like 'dir', 'ls', 'pip install', etc."""
    data = request.json or {}
    command = data.get('command', '').strip()
    if not command:
        return jsonify({'error': 'No command provided'}), 400

    config = load_config()
    base = config.get('workspace', WORKSPACE)

    # When a project is open, run commands in the project directory
    project = config.get('project', None)
    if project:
        project_dir = os.path.join(base, project)
        if os.path.isdir(project_dir):
            base = project_dir

    # On Windows, wrap with cmd /c to ensure built-in commands (dir, cd, etc.) work
    # On Linux/macOS, use bash -c for consistency
    if IS_WINDOWS:
        cmd = f'cmd /c {command}'
    else:
        cmd = command  # shell=True already uses bash

    proc_id = run_process(cmd, cwd=base)
    return jsonify({'ok': True, 'proc_id': proc_id, 'cwd': base})


@bp.route('/api/run/stop', methods=['POST'])
@handle_error
def stop_execution():
    data = request.json
    proc_id = data.get('proc_id', '')
    if proc_id and proc_id in running_processes:
        stopped = stop_process(proc_id)
        return jsonify({'ok': stopped})
    return jsonify({'ok': False})


@bp.route('/api/run/kill-port', methods=['POST'])
@handle_error
def kill_port():
    """Kill any process listening on the given port. Useful before starting a server
    to avoid 'port already in use' errors."""
    import subprocess as sp
    data = request.json or {}
    port = data.get('port')
    if not port:
        return jsonify({'error': 'Port number required'}), 400
    try:
        port = int(port)
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid port number'}), 400

    # SAFETY: Never kill the IDE's own port
    ide_port = int(os.environ.get('MUSIDE_PORT', 12345))
    if port == ide_port:
        return jsonify({'error': f'BLOCKED: Port {port} is the MusIDE server port — killing it would shut down the IDE. Operation refused.'}), 403

    killed_pids = []
    if IS_WINDOWS:
        # Windows: use netstat to find PID, then taskkill
        try:
            result = sp.run(
                f'netstat -ano | findstr :{port} | findstr LISTENING',
                shell=True, capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.strip().splitlines():
                parts = line.strip().split()
                if parts:
                    pid = parts[-1]
                    try:
                        sp.run(f'taskkill /F /PID {pid}', shell=True, capture_output=True, timeout=5)
                        killed_pids.append(pid)
                    except Exception:
                        pass
        except Exception:
            pass
    else:
        # Linux/macOS: use lsof to find PID, then kill
        try:
            result = sp.run(
                f'lsof -ti :{port}',
                shell=True, capture_output=True, text=True, timeout=5
            )
            pids = result.stdout.strip().splitlines()
            for pid in pids:
                pid = pid.strip()
                if pid:
                    try:
                        os.kill(int(pid), 9)
                        killed_pids.append(pid)
                    except (OSError, ValueError):
                        pass
        except Exception:
            pass

    # Also stop any of our managed processes that might be using this port
    for proc_id, info in list(running_processes.items()):
        if info.get('running') and str(port) in info.get('cmd', ''):
            stop_process(proc_id)
            killed_pids.append(f'managed:{proc_id}')

    if killed_pids:
        return jsonify({'ok': True, 'killed': killed_pids, 'message': f'Killed processes on port {port}: {killed_pids}'})
    else:
        return jsonify({'ok': True, 'killed': [], 'message': f'No process found on port {port}'})


@bp.route('/api/run/processes', methods=['GET'])
@handle_error
def list_processes():
    """List all running and recent processes.
    Uses proc.poll() to verify actual OS process state,
    so this is accurate even after page refreshes."""
    processes = []
    for pid, info in running_processes.items():
        start = info.get('start_time')
        # Verify actual process state at the OS level
        running = _verify_process_state(pid)
        uptime = ''
        if start:
            elapsed = time.time() - start
            mins, secs = divmod(int(elapsed), 60)
            hours, mins = divmod(mins, 60)
            if hours > 0:
                uptime = f'{hours}h {mins}m {secs}s'
            elif mins > 0:
                uptime = f'{mins}m {secs}s'
            else:
                uptime = f'{secs}s'
        # Truncate command for display
        cmd = info.get('cmd', '')
        if len(cmd) > 120:
            cmd = cmd[:120] + '...'
        processes.append({
            'id': pid,
            'running': running,
            'cwd': info.get('cwd', ''),
            'cmd': cmd,
            'exit_code': info.get('exit_code'),
            'uptime': uptime,
            'start_time': start,
        })
    return jsonify({'processes': processes})


@bp.route('/api/run/output', methods=['GET'])
@handle_error
def get_output():
    proc_id = request.args.get('proc_id', '')
    since = int(request.args.get('since', 0))

    if proc_id and proc_id in process_outputs:
        outputs = process_outputs[proc_id][since:]
        # Verify actual process state (not just the flag)
        is_running = _verify_process_state(proc_id)
        return jsonify({
            'outputs': outputs,
            'since': len(process_outputs[proc_id]),
            'running': is_running,
        })
    return jsonify({'outputs': [], 'since': 0, 'running': False})


@bp.route('/api/run/output/stream', methods=['GET'])
def stream_output():
    """SSE endpoint for real-time output"""
    proc_id = request.args.get('proc_id', '')

    def generate():
        idx = 0
        # Wait briefly for processOutputs to be populated
        time.sleep(0.15)
        while True:
            if proc_id and proc_id in process_outputs:
                outputs = process_outputs[proc_id]
                if idx < len(outputs):
                    for item in outputs[idx:]:
                        evt_type = item.get('type', 'stdout')
                        # Send as named SSE event so frontend addEventListener works
                        yield f"event: {evt_type}\ndata: {json.dumps(item)}\n\n"
                    idx = len(outputs)

                # Verify actual process state (not just the flag)
                is_running = _verify_process_state(proc_id)
                if not is_running:
                    exit_code = running_processes.get(proc_id, {}).get('exit_code', 0)
                    yield f"event: exit\ndata: {json.dumps({'exit_code': exit_code or 0})}\n\n"
                    break
            else:
                # proc_id not found — process may have finished before we started
                # Check one more time after a brief delay
                time.sleep(0.2)
                if proc_id and proc_id not in process_outputs:
                    yield f"event: done\ndata: \"Process not found\"\n\n"
                    break

            time.sleep(0.1)

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
