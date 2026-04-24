"""
MusIDE - Python Runtime Debugger using sys.settrace()
Provides breakpoints, stepping, variable inspection, call stack for Python code execution.
"""

import os
import sys
import json
import time
import threading
import traceback
import linecache
import bisect
from flask import Blueprint, jsonify, request, Response
from utils import WORKSPACE

bp = Blueprint('debug', __name__)

# ==================== Debug Session ====================

class DebugSession:
    """Manages a single debugging session with sys.settrace()."""

    STATES = ('idle', 'running', 'paused', 'stopped')

    def __init__(self):
        self.state = 'idle'
        self.breakpoints = {}       # filename -> set of line numbers
        self.call_stack = []        # list of (filename, lineno, func_name)
        self.local_vars = {}        # variable name -> value repr
        self.current_file = None
        self.current_line = 0
        self.current_func = ''
        self._pause_event = threading.Event()
        self._stop_event = threading.Event()
        self._step_mode = None      # None, 'step_in', 'step_over', 'step_out'
        self._step_stack_depth = 0
        self._output_lines = []     # captured stdout/stderr
        self._lock = threading.Lock()
        self._thread = None
        self._listeners = []        # SSE listeners

    def start(self, file_path, args='', cwd=None):
        """Start debugging a Python file."""
        with self._lock:
            if self.state == 'running':
                return False, 'Debug session already running'
            self.state = 'running'
            self.breakpoints = {}
            self.call_stack = []
            self.local_vars = {}
            self.current_file = None
            self.current_line = 0
            self._step_mode = None
            self._output_lines = []
            self._stop_event.clear()
            self._pause_event.clear()

        self._thread = threading.Thread(
            target=self._run_target,
            args=(file_path, args, cwd),
            daemon=True
        )
        self._thread.start()
        return True, 'Debug session started'

    def stop(self):
        """Stop the debug session."""
        with self._lock:
            if self.state in ('idle', 'stopped'):
                return
            self.state = 'stopped'
            self._stop_event.set()
            self._pause_event.set()  # unblock if paused

    def resume(self):
        """Continue execution after a breakpoint/step pause."""
        with self._lock:
            if self.state != 'paused':
                return False
            self._step_mode = None
            self.state = 'running'
        self._pause_event.set()
        return True

    def step_in(self):
        """Step into the next line."""
        with self._lock:
            if self.state != 'paused':
                return False
            self._step_mode = 'step_in'
            self.state = 'running'
        self._pause_event.set()
        return True

    def step_over(self):
        """Step over the next line."""
        with self._lock:
            if self.state != 'paused':
                return False
            self._step_mode = 'step_over'
            self._step_stack_depth = len(self.call_stack)
            self.state = 'running'
        self._pause_event.set()
        return True

    def step_out(self):
        """Step out of the current function."""
        with self._lock:
            if self.state != 'paused':
                return False
            self._step_mode = 'step_out'
            self._step_stack_depth = len(self.call_stack)
            self.state = 'running'
        self._pause_event.set()
        return True

    def set_breakpoints(self, file_path, lines):
        """Set breakpoints for a file. Replaces existing breakpoints for that file."""
        with self._lock:
            self.breakpoints[file_path] = set(lines)
        return True

    def add_breakpoint(self, file_path, line):
        """Add a single breakpoint."""
        with self._lock:
            if file_path not in self.breakpoints:
                self.breakpoints[file_path] = set()
            self.breakpoints[file_path].add(line)
        return True

    def remove_breakpoint(self, file_path, line):
        """Remove a single breakpoint."""
        with self._lock:
            if file_path in self.breakpoints:
                self.breakpoints[file_path].discard(line)
        return True

    def evaluate(self, expression):
        """Evaluate an expression in the current frame context."""
        with self._lock:
            if self.state != 'paused':
                return None, 'Not paused'
            if not self.call_stack:
                return None, 'No call stack'
        # We can't directly access frame locals from here since the trace
        # function runs in the debug thread. We use a special mechanism.
        result = [None, '']
        eval_event = threading.Event()

        def _eval_in_frame():
            try:
                frame = sys._getframe().f_back
                # Walk up to find the target frame
                while frame:
                    if (frame.f_code.co_filename == self.current_file and
                            frame.f_lineno == self.current_line):
                        break
                    frame = frame.f_back
                if frame:
                    result[0] = repr(eval(expression, frame.f_globals, frame.f_locals))
                else:
                    result[1] = 'Could not find target frame'
            except Exception as e:
                result[1] = str(e)
            finally:
                eval_event.set()

        # Schedule eval in the trace thread context
        self._eval_callback = _eval_in_frame
        self._eval_event = eval_event
        self._pause_event.set()  # let trace function run the eval
        eval_event.wait(timeout=5)
        self._eval_callback = None

        if result[1]:
            return None, result[1]
        return result[0], None

    def get_state(self):
        """Get current debug state as a dict."""
        with self._lock:
            return {
                'state': self.state,
                'file': self.current_file,
                'line': self.current_line,
                'func': self.current_func,
                'breakpoints': {k: sorted(v) for k, v in self.breakpoints.items()},
                'call_stack': list(self.call_stack),
                'local_vars': dict(self.local_vars),
                'output': list(self._output_lines),
            }

    # ── Internal trace function ──

    def _trace_function(self, frame, event, arg):
        """sys.settrace() callback."""
        # Check stop
        if self._stop_event.is_set():
            self.state = 'stopped'
            sys.settrace(None)
            return None

        filename = frame.f_code.co_filename
        lineno = frame.f_lineno
        func_name = frame.f_code.co_name

        # Only trace user files (not stdlib/ide)
        if self._should_ignore(filename):
            return self._trace_function

        if event == 'call':
            self._on_call(filename, lineno, func_name)
        elif event == 'line':
            self._on_line(filename, lineno, func_name, frame)
        elif event == 'return':
            self._on_return(filename, lineno, func_name, arg)

        return self._trace_function

    def _should_ignore(self, filename):
        """Skip tracing for non-user files."""
        if not filename or filename.startswith('<'):
            return True
        # Skip standard library
        if 'site-packages' in filename or '/usr/lib/' in filename:
            return True
        return False

    def _on_call(self, filename, lineno, func_name):
        """Handle function call event."""
        entry = (filename, lineno, func_name)
        self.call_stack.append(entry)
        # If stepping out, check if we've returned enough
        if self._step_mode == 'step_out':
            if len(self.call_stack) <= self._step_stack_depth:
                pass  # Will pause on next line event

    def _on_line(self, filename, lineno, func_name, frame):
        """Handle line event - check breakpoints and stepping."""
        with self._lock:
            self.current_file = filename
            self.current_line = lineno
            self.current_func = func_name
            # Update call stack top
            if self.call_stack:
                self.call_stack[-1] = (filename, lineno, func_name)

        # Check if we should evaluate a pending expression
        if hasattr(self, '_eval_callback') and self._eval_callback:
            cb = self._eval_callback
            self._eval_callback = None
            cb()
            # Re-pause after eval
            self._pause_event.clear()
            self._pause_event.wait()
            return

        # Collect local variables
        try:
            self.local_vars = {k: repr(v) for k, v in frame.f_locals.items()
                               if not k.startswith('__')}
        except Exception:
            self.local_vars = {}

        # Check breakpoints
        bp_lines = self.breakpoints.get(filename, set())
        if lineno in bp_lines:
            self._pause('breakpoint')
            return

        # Check stepping
        if self._step_mode == 'step_in':
            self._pause('step')
            return
        elif self._step_mode == 'step_over':
            if len(self.call_stack) <= self._step_stack_depth:
                self._pause('step')
                return
        elif self._step_mode == 'step_out':
            if len(self.call_stack) < self._step_stack_depth:
                self._pause('step')
                return

    def _on_return(self, filename, lineno, func_name, return_value):
        """Handle function return event."""
        if self.call_stack:
            self.call_stack.pop()

    def _pause(self, reason):
        """Pause execution and wait for resume/step/stop."""
        with self._lock:
            self.state = 'paused'
        self._pause_event.clear()
        self._notify_state_change(reason)
        # Wait for resume/step/stop
        self._pause_event.wait()

    def _notify_state_change(self, reason=''):
        """Send state change to SSE listeners."""
        state = self.get_state()
        state['reason'] = reason
        data = json.dumps(state)

    def _run_target(self, file_path, args, cwd):
        """Run the target file with tracing enabled."""
        from utils import load_config, shlex_quote, WORKSPACE

        config = load_config()
        env = os.environ.copy()
        venv_path = config.get('venv_path', '')

        # Auto-detect virtual environment if not explicitly configured
        if not venv_path:
            # Check common venv locations relative to the file being debugged
            file_dir = os.path.dirname(file_path)
            for candidate in [
                os.path.join(file_dir, 'venv'),
                os.path.join(file_dir, '.venv'),
                os.path.join(file_dir, 'env'),
                os.path.join(file_dir, '.env'),
                # Also check workspace root
                os.path.join(config.get('workspace', WORKSPACE), 'venv'),
                os.path.join(config.get('workspace', WORKSPACE), '.venv'),
            ]:
                if os.path.isdir(candidate) and os.path.isfile(os.path.join(candidate, 'bin', 'python')):
                    venv_path = candidate
                    break

        venv_site_packages = None
        if venv_path and os.path.exists(venv_path):
            venv_bin = os.path.join(venv_path, 'bin')
            if os.path.exists(venv_bin):
                env['PATH'] = venv_bin + ':' + env.get('PATH', '')
                env['VIRTUAL_ENV'] = venv_path
            # Detect venv site-packages and add to sys.path
            venv_lib = os.path.join(venv_path, 'lib')
            if os.path.isdir(venv_lib):
                # Find python3.x/site-packages under lib/
                for entry in os.listdir(venv_lib):
                    sp = os.path.join(venv_lib, entry, 'site-packages')
                    if os.path.isdir(sp):
                        venv_site_packages = sp
                        break
            # Also try the exact Python version path
            if not venv_site_packages:
                for sp in [
                    os.path.join(venv_path, 'lib', f'python{sys.version_info.major}.{sys.version_info.minor}', 'site-packages'),
                ]:
                    if os.path.isdir(sp):
                        venv_site_packages = sp
                        break

        # Set working directory
        if not cwd:
            cwd = config.get('workspace', os.path.dirname(file_path))

        # Prepare to run
        old_cwd = os.getcwd()
        old_argv = sys.argv
        old_signal = None
        old_path = None
        try:
            os.chdir(cwd)
            sys.argv = [file_path] + (args.split() if args else [])

            # Add venv site-packages to sys.path so imports work in exec()
            if venv_site_packages and venv_site_packages not in sys.path:
                old_path = sys.path.copy()
                sys.path.insert(0, venv_site_packages)

            # Add file directory and workspace to sys.path for local imports
            file_dir = os.path.dirname(os.path.abspath(file_path))
            if file_dir not in sys.path:
                if old_path is None:
                    old_path = sys.path.copy()
                sys.path.insert(0, file_dir)
            workspace = config.get('workspace', WORKSPACE)
            if workspace and workspace not in sys.path:
                if old_path is None:
                    old_path = sys.path.copy()
                sys.path.insert(0, workspace)

            # Monkey-patch signal.signal to work in non-main thread.
            # Python's signal module only works in the main thread, but many
            # programs call signal.signal() without checking. We make it a
            # no-op in the debug thread so the target code doesn't crash.
            import signal as _signal_mod
            if threading.current_thread() is not threading.main_thread():
                old_signal = _signal_mod.signal
                def _safe_signal(sig, handler):
                    try:
                        return old_signal(sig, handler)
                    except ValueError:
                        # signal only works in main thread — silently ignore
                        return handler
                _signal_mod.signal = _safe_signal

            # Set the trace function
            sys.settrace(self._trace_function)

            # Run with captured output
            import io
            old_stdout = sys.stdout
            old_stderr = sys.stderr
            captured_out = io.StringIO()

            class _Writer:
                def __init__(self, inner, session):
                    self.inner = inner
                    self.session = session

                def write(self, data):
                    self.inner.write(data)
                    if data:
                        self.session._output_lines.append(data.rstrip('\n'))
                        if len(self.session._output_lines) > 500:
                            self.session._output_lines = self.session._output_lines[-500:]

                def flush(self):
                    self.inner.flush()

            sys.stdout = _Writer(old_stdout, self)
            sys.stderr = _Writer(old_stderr, self)

            # Compile and execute
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                code = f.read()

            compiled = compile(code, file_path, 'exec')
            exec(compiled, {'__name__': '__main__', '__file__': file_path})

        except SystemExit:
            pass
        except Exception as e:
            tb = traceback.format_exc()
            self._output_lines.append(f'\n{tb}')
        finally:
            sys.settrace(None)
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            sys.argv = old_argv
            os.chdir(old_cwd)
            # Restore original sys.path if we added venv site-packages
            if old_path is not None:
                sys.path[:] = old_path
            # Restore original signal.signal if we patched it
            if old_signal is not None:
                _signal_mod.signal = old_signal

            with self._lock:
                if self.state != 'stopped':
                    self.state = 'stopped'

            self._notify_state_change('finished')

    def cleanup(self):
        """Clean up session resources."""
        self.stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)


# ==================== Global Session ====================

_session = None

def get_session():
    """Get or create the global debug session."""
    global _session
    if _session is None:
        _session = DebugSession()
    return _session


# ==================== API Routes ====================

@bp.route('/api/debug/start', methods=['POST'])
def debug_start():
    """Start a debug session for a file."""
    data = request.json or {}
    file_path = data.get('file_path', '')
    args = data.get('args', '')
    cwd = data.get('cwd', '')

    if not file_path:
        return jsonify({'ok': False, 'error': 'file_path is required'})

    # Resolve relative paths against workspace
    if not os.path.isabs(file_path):
        resolved = os.path.join(WORKSPACE, file_path)
        if os.path.isfile(resolved):
            file_path = resolved

    if not os.path.isfile(file_path):
        return jsonify({'ok': False, 'error': 'File not found: ' + file_path})

    session = get_session()
    ok, msg = session.start(file_path, args, cwd or None)
    return jsonify({'ok': ok, 'message': msg, 'state': session.get_state()})


@bp.route('/api/debug/stop', methods=['POST'])
def debug_stop():
    """Stop the current debug session."""
    session = get_session()
    session.stop()
    return jsonify({'ok': True, 'state': session.get_state()})


@bp.route('/api/debug/continue', methods=['POST'])
def debug_continue():
    """Continue execution after a pause."""
    session = get_session()
    ok = session.resume()
    return jsonify({'ok': ok, 'state': session.get_state()})


@bp.route('/api/debug/step', methods=['POST'])
def debug_step():
    """Step execution. Supports step_in, step_over, step_out."""
    data = request.json or {}
    action = data.get('action', 'step_in')
    session = get_session()

    if action == 'step_in':
        ok = session.step_in()
    elif action == 'step_over':
        ok = session.step_over()
    elif action == 'step_out':
        ok = session.step_out()
    else:
        ok = session.step_in()

    return jsonify({'ok': ok, 'state': session.get_state()})


@bp.route('/api/debug/breakpoints', methods=['POST'])
def debug_set_breakpoints():
    """Set breakpoints for a file."""
    data = request.json or {}
    file_path = data.get('file_path', '')
    lines = data.get('lines', [])
    action = data.get('action', 'set')  # set, add, remove

    session = get_session()
    if action == 'add':
        session.add_breakpoint(file_path, data.get('line', 0))
    elif action == 'remove':
        session.remove_breakpoint(file_path, data.get('line', 0))
    else:
        session.set_breakpoints(file_path, lines)

    return jsonify({'ok': True, 'breakpoints': session.get_state().get('breakpoints', {})})


@bp.route('/api/debug/state', methods=['GET'])
def debug_get_state():
    """Get current debug state (polling endpoint)."""
    session = get_session()
    return jsonify(session.get_state())


@bp.route('/api/debug/state/stream', methods=['GET'])
def debug_state_stream():
    """SSE endpoint for real-time debug state updates."""
    session = get_session()

    def generate():
        last_state = None
        while True:
            state = session.get_state()
            state_json = json.dumps(state)

            if state_json != last_state:
                last_state = state_json
                yield f"data: {state_json}\n\n"

                # If session ended, close stream
                if state['state'] in ('idle', 'stopped'):
                    yield f"event: done\ndata: \"debug session ended\"\n\n"
                    break

            time.sleep(0.2)

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@bp.route('/api/debug/evaluate', methods=['POST'])
def debug_evaluate():
    """Evaluate an expression in the current debug context."""
    data = request.json or {}
    expression = data.get('expression', '')

    if not expression:
        return jsonify({'ok': False, 'error': 'Empty expression'})

    session = get_session()
    result, error = session.evaluate(expression)

    if error:
        return jsonify({'ok': False, 'error': error})
    return jsonify({'ok': True, 'result': result})


@bp.route('/api/debug/output', methods=['GET'])
def debug_get_output():
    """Get captured output from debug session."""
    session = get_session()
    state = session.get_state()
    return jsonify({'output': state.get('output', []), 'state': state['state']})
