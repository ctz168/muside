"""
MusIDE - Shared utilities, constants, and helper functions.
"""

import os
import sys
import json
import subprocess
import threading
import time
import re
import shutil
import traceback
import uuid
import fnmatch
import signal
from pathlib import Path
from datetime import datetime
from functools import wraps
from flask import jsonify

# ==================== Constants ====================
SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.environ.get('MUSIDE_WORKSPACE', os.path.expanduser('~/muside_workspace'))
PORT = int(os.environ.get('MUSIDE_PORT', 12346))
HOST = os.environ.get('MUSIDE_HOST', '0.0.0.0')

CONFIG_DIR = os.path.expanduser('~/.muside')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')
LLM_CONFIG_FILE = os.path.join(CONFIG_DIR, 'llm_config.json')
CHAT_HISTORY_FILE = os.path.join(CONFIG_DIR, 'chat_history.json')
CONVERSATIONS_FILE = os.path.join(CONFIG_DIR, 'conversations.json')

# ==================== Log Buffer ====================
import collections

_log_buffer = collections.deque(maxlen=10000)
_log_lock = threading.Lock()


def log_write(line):
    """Write a line to the in-memory ring buffer."""
    with _log_lock:
        _log_buffer.append({'time': datetime.now().isoformat(), 'text': line})


# ==================== Config Management ====================
def _read_json_file(filepath):
    """Read a JSON file with encoding fallback: utf-8 → gbk → latin-1.
    
    On Windows, JSON config files may have been written in GBK encoding
    by an older version of the server. This function tries UTF-8 first,
    then falls back to GBK and latin-1.
    """
    for enc in ['utf-8', 'utf-8-sig', 'gbk', 'gb2312', 'latin-1']:
        try:
            with open(filepath, 'r', encoding=enc) as f:
                return json.load(f)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        except Exception:
            break
    # Last resort: read with utf-8 and replace errors
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            return json.load(f)
    except Exception:
        pass
    return None


def load_config():
    try:
        if os.path.exists(CONFIG_FILE):
            result = _read_json_file(CONFIG_FILE)
            if result is not None:
                return result
    except Exception:
        pass
    return {
        'workspace': WORKSPACE,
        'venv_path': '',
        'compiler': 'python3',
        'theme': 'claude',
        'font_size': 14,
        'tab_size': 4,
        'show_line_numbers': True,
        'github_token': '',
        'github_auth_method': '',
    }


def save_config(config):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


DEFAULT_LLM_MODELS = [
    {
        'name': 'OpenAI',
        'provider': 'openai',
        'api_type': 'openai',
        'api_key': '',
        'api_base': '',
        'model': 'gpt-4o-mini',
        'enabled': True,
        'temperature': 0.7,
        'max_tokens': 100000,
        'max_context': 128000,
        'reasoning': True,
    },
    {
        'name': 'Anthropic',
        'provider': 'anthropic',
        'api_type': 'anthropic',
        'api_key': '',
        'api_base': 'https://api.anthropic.com/v1',
        'model': 'claude-sonnet-4-20250514',
        'enabled': False,
        'temperature': 0.7,
        'max_tokens': 100000,
        'max_context': 200000,
        'reasoning': True,
    },
    {
        'name': 'Ollama',
        'provider': 'ollama',
        'api_type': 'ollama',
        'api_key': '',
        'api_base': 'http://localhost:11434',
        'model': 'llama3',
        'enabled': False,
        'temperature': 0.7,
        'max_tokens': 100000,
        'max_context': 128000,
        'reasoning': True,
    },
    {
        'name': 'ModelScope',
        'provider': 'modelscope',
        'api_type': 'openai',
        'api_key': 'ms-3eca52df-ea14-481b-9e72-73b988b612f7',
        'api_base': 'https://api-inference.modelscope.cn/v1',
        'model': 'stepfun-ai/Step-3.5-Flash',
        'enabled': False,
        'temperature': 0.7,
        'max_tokens': 100000,
        'max_context': 128000,
        'reasoning': True,
    },
]


def load_llm_config():
    """Load LLM config, migrating legacy single-model format to multi-model format."""
    if os.path.exists(LLM_CONFIG_FILE):
        config = _read_json_file(LLM_CONFIG_FILE)
        if config is None:
            config = {}
        # Migrate legacy single-model config to multi-model format
        if 'models' not in config:
            legacy = {
                'name': config.get('provider', 'openai').capitalize(),
                'provider': config.get('provider', 'openai'),
                'api_type': config.get('api_type', 'openai'),
                'api_key': config.get('api_key', ''),
                'api_base': config.get('api_base', ''),
                'model': config.get('model', 'gpt-4o-mini'),
                'enabled': True,
                'temperature': config.get('temperature', 0.7),
                'max_tokens': config.get('max_tokens', 100000),
                'max_context': 128000,
                'reasoning': True,
            }
            config['models'] = [legacy]
            del config['provider']
            del config['api_key']
            del config['api_base']
            del config['model']
            del config['temperature']
            del config['max_tokens']
            if 'api_type' in config:
                del config['api_type']
            save_llm_config(config)
        # Ensure all models have the reasoning and max_context field (migration for existing configs)
        for m in config.get('models', []):
            if 'reasoning' not in m:
                m['reasoning'] = True
            if 'max_context' not in m:
                m['max_context'] = 128000
        return config
    return {
        'models': DEFAULT_LLM_MODELS,
        'system_prompt': '你是一个专业的程序员，擅长编程和测试。每次开发任务的时候，先读项目的readme.md和worklog.md（没有你就创建）。每次开始和结束任务的时候要更新worklog.md，主要是整理压缩之前的记录（不能超过100条）、总结经验、更新最新工作记录。readme重点维护怎样测试使用这个项目的内容。',
    }


def save_llm_config(config):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(LLM_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def get_active_llm_config(config=None):
    """Get the first enabled model config as a flat dict (compatible with existing code)."""
    if config is None:
        config = load_llm_config()
    models = config.get('models', [])
    system_prompt = config.get('system_prompt', '')
    for m in models:
        if m.get('enabled'):
            flat = dict(m)
            flat['system_prompt'] = flat.get('system_prompt') or system_prompt
            return flat
    # Fallback: return first model or default
    if models:
        flat = dict(models[0])
        flat['system_prompt'] = flat.get('system_prompt') or system_prompt
        return flat
    return {
        'provider': 'openai', 'api_type': 'openai', 'api_key': '',
        'api_base': '', 'model': 'gpt-4o-mini', 'temperature': 0.7,
        'max_tokens': 100000, 'system_prompt': system_prompt,
    }


def load_chat_history():
    if os.path.exists(CHAT_HISTORY_FILE):
        result = _read_json_file(CHAT_HISTORY_FILE)
        if result is not None:
            return result
    return []


def save_chat_history(history):
    # Keep last 200 messages
    history = history[-200:]
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CHAT_HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


# ==================== Conversations (multi-session) ====================

def load_conversations():
    """Load all conversations. Returns list of {id, title, created_at, updated_at, messages}."""
    if os.path.exists(CONVERSATIONS_FILE):
        result = _read_json_file(CONVERSATIONS_FILE)
        if result is not None:
            return result
    return []


def save_conversations(conversations):
    """Save all conversations."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONVERSATIONS_FILE, 'w', encoding='utf-8') as f:
        json.dump(conversations, f, indent=2, ensure_ascii=False)


def get_conversation(conv_id):
    """Get a single conversation by id."""
    convs = load_conversations()
    for c in convs:
        if c.get('id') == conv_id:
            return c
    return None


def save_conversation(conv_id, messages, title=None):
    """Save messages to a conversation, auto-creating if needed. Returns updated conv."""
    convs = load_conversations()
    now = datetime.now().isoformat()
    conv = None
    for c in convs:
        if c.get('id') == conv_id:
            conv = c
            break
    if not conv:
        # Auto-generate title from first user message
        auto_title = title or 'New Chat'
        for m in messages:
            if m.get('role') == 'user':
                auto_title = (m.get('content', '') or '')[:50]
                break
        conv = {
            'id': conv_id,
            'title': auto_title,
            'created_at': now,
            'updated_at': now,
            'messages': [],
        }
        convs.insert(0, conv)  # newest first
    conv['messages'] = messages[-200:]  # keep last 200
    conv['updated_at'] = now
    if title:
        conv['title'] = title
    save_conversations(convs)
    return conv


def delete_conversation(conv_id):
    """Delete a conversation by id."""
    convs = load_conversations()
    convs = [c for c in convs if c.get('id') != conv_id]
    save_conversations(convs)


# ==================== Process Management ====================
running_processes = {}
process_outputs = {}

# Maximum number of finished processes to keep in memory
MAX_FINISHED_PROCESSES = 20
# Maximum age (seconds) for finished process entries
MAX_FINISHED_AGE = 3600  # 1 hour


def _verify_process_state(proc_id):
    """
    Verify the actual OS process state using proc.poll().
    Updates running_processes in-place and returns the verified running flag.
    This handles cases where the running flag is stale.
    """
    if proc_id not in running_processes:
        return False
    info = running_processes[proc_id]
    proc = info.get('process')
    if proc is None:
        return info.get('running', False)
    # If the process object exists, check its actual state
    poll_result = proc.poll()
    if poll_result is not None:
        # Process has exited at the OS level — update our state
        if info.get('running'):
            info['running'] = False
            info['exit_code'] = poll_result
            # Append exit status to output if not already there
            if proc_id in process_outputs:
                process_outputs[proc_id].append({
                    'type': 'status',
                    'text': f'Process exited with code {poll_result}',
                    'exit_code': poll_result,
                    'time': datetime.now().isoformat(),
                })
        return False
    # poll() returns None → process is still alive
    info['running'] = True
    return True


def _cleanup_old_processes():
    """
    Remove old finished process entries to prevent memory leaks.
    Keeps at most MAX_FINISHED_PROCESSES finished entries under MAX_FINISHED_AGE seconds.
    """
    now = time.time()
    finished = []
    running = []
    for pid, info in running_processes.items():
        # First verify actual state
        _verify_process_state(pid)
        if info.get('running'):
            running.append(pid)
        else:
            finished.append((pid, info.get('start_time', now)))

    # Sort finished by start_time descending (newest first)
    finished.sort(key=lambda x: x[1], reverse=True)

    # Remove old finished processes beyond the limit
    to_remove = []
    for i, (pid, start) in enumerate(finished):
        age = now - start if start else 0
        if i >= MAX_FINISHED_PROCESSES or age > MAX_FINISHED_AGE:
            to_remove.append(pid)

    for pid in to_remove:
        running_processes.pop(pid, None)
        process_outputs.pop(pid, None)


def run_process(cmd, cwd=None, timeout=300, proc_id=None):
    """Run a subprocess and capture output"""
    if not proc_id:
        proc_id = str(uuid.uuid4())[:8]

    # Cleanup old finished processes before starting a new one
    _cleanup_old_processes()

    process_outputs[proc_id] = []
    running_processes[proc_id] = {
        'process': None,
        'cwd': cwd,
        'cmd': cmd,          # Store the command for display
        'running': False,
        'start_time': None,
        'exit_code': None,
    }

    def execute():
        try:
            env = os.environ.copy()
            config = load_config()
            if config.get('venv_path') and os.path.exists(config['venv_path']):
                if IS_WINDOWS:
                    venv_bin = os.path.join(config['venv_path'], 'Scripts')
                else:
                    venv_bin = os.path.join(config['venv_path'], 'bin')
                if os.path.exists(venv_bin):
                    env['PATH'] = venv_bin + (os.pathsep + env.get('PATH', '') if env.get('PATH') else '')
                    env['VIRTUAL_ENV'] = config['venv_path']

            running_processes[proc_id]['running'] = True
            running_processes[proc_id]['start_time'] = time.time()

            popen_kwargs = dict(
                shell=True,
                cwd=cwd or config.get('workspace', WORKSPACE),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                text=True,
                bufsize=1,
            )
            # On Windows, force UTF-8 to avoid cp936/cp1252 encoding issues
            # with non-ASCII output (Chinese, Japanese, Korean, etc.)
            if IS_WINDOWS:
                popen_kwargs['encoding'] = 'utf-8'
                popen_kwargs['errors'] = 'replace'
                # Create new process group on Windows so we can kill the entire tree
                popen_kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                # On Linux/macOS, start a new session (process group) so we can killpg
                popen_kwargs['start_new_session'] = True
            proc = subprocess.Popen(cmd, **popen_kwargs)
            running_processes[proc_id]['process'] = proc

            for line in iter(proc.stdout.readline, ''):
                if not running_processes[proc_id]['running']:
                    break
                output = line.rstrip('\n')
                process_outputs[proc_id].append({
                    'type': 'stdout',
                    'text': output,
                    'time': datetime.now().isoformat(),
                })

            proc.wait(timeout=5)
            code = proc.returncode
            running_processes[proc_id]['exit_code'] = code

            process_outputs[proc_id].append({
                'type': 'status',
                'text': f'Process exited with code {code}',
                'exit_code': code,
                'time': datetime.now().isoformat(),
            })
        except subprocess.TimeoutExpired:
            running_processes[proc_id]['exit_code'] = -1
            process_outputs[proc_id].append({
                'type': 'error',
                'text': 'Process timed out',
                'time': datetime.now().isoformat(),
            })
        except Exception as e:
            running_processes[proc_id]['exit_code'] = -1
            process_outputs[proc_id].append({
                'type': 'error',
                'text': str(e),
                'time': datetime.now().isoformat(),
            })
        finally:
            running_processes[proc_id]['running'] = False

    t = threading.Thread(target=execute, daemon=True)
    t.start()
    return proc_id


def stop_process(proc_id):
    if proc_id in running_processes:
        proc = running_processes[proc_id]
        if proc['process'] and proc['running']:
            proc['running'] = False
            try:
                os_proc = proc['process']
                pid = os_proc.pid
                if IS_WINDOWS:
                    # On Windows, send CTRL_BREAK_EVENT to the process group
                    # This gracefully stops processes like python, node, etc.
                    try:
                        os.kill(pid, signal.CTRL_BREAK_EVENT)
                        os_proc.wait(timeout=3)
                    except (OSError, subprocess.TimeoutExpired):
                        # Fallback: kill the process tree
                        try:
                            os_proc.kill()
                            os_proc.wait(timeout=2)
                        except:
                            pass
                else:
                    # On Linux/macOS, kill the entire process group
                    # os.killpg sends signal to all processes in the group
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGTERM)
                        os_proc.wait(timeout=3)
                    except (OSError, subprocess.TimeoutExpired):
                        # SIGTERM didn't work, force SIGKILL the whole group
                        try:
                            os.killpg(os.getpgid(pid), signal.SIGKILL)
                            os_proc.wait(timeout=2)
                        except:
                            pass
                proc['exit_code'] = os_proc.returncode
            except:
                pass
            return True
    return False


# ==================== File Type Detection ====================
def get_file_type(filename):
    ext = os.path.splitext(filename)[1].lower()
    type_map = {
        '.py': 'python', '.js': 'javascript', '.ts': 'typescript',
        '.jsx': 'javascript', '.tsx': 'typescript',
        '.html': 'html', '.htm': 'html', '.css': 'css', '.scss': 'scss',
        '.json': 'json', '.xml': 'xml', '.yaml': 'yaml', '.yml': 'yaml',
        '.md': 'markdown', '.txt': 'text', '.sh': 'shell', '.bash': 'shell',
        '.c': 'c', '.cpp': 'cpp', '.h': 'c', '.hpp': 'cpp',
        '.java': 'java', '.kt': 'kotlin', '.swift': 'swift',
        '.go': 'go', '.rs': 'rust', '.rb': 'ruby', '.php': 'php',
        '.sql': 'sql', '.r': 'r', '.lua': 'lua', '.vim': 'vim',
        '.dockerfile': 'dockerfile', '.toml': 'toml', '.ini': 'ini',
        '.cfg': 'ini', '.conf': 'ini', '.log': 'text',
        '.env': 'shell', '.gitignore': 'text', '.editorconfig': 'ini',
    }
    # Check special filenames
    if filename == 'Dockerfile':
        return 'dockerfile'
    if filename == 'Makefile':
        return 'makefile'
    return type_map.get(ext, 'text')


def get_icon_for_file(filename):
    if os.path.isdir(filename) if isinstance(filename, str) else False:
        return 'folder'
    ext = os.path.splitext(filename)[1].lower()
    icon_map = {
        '.py': '🐍', '.js': '📜', '.ts': '📘', '.html': '🌐',
        '.css': '🎨', '.json': '📋', '.md': '📝', '.txt': '📄',
        '.sh': '⚡', '.yml': '⚙️', '.yaml': '⚙️', '.toml': '⚙️',
        '.gitignore': '🚫', '.env': '🔒',
        '.c': '🔧', '.cpp': '🔧', '.h': '🔧',
        '.java': '☕', '.go': '🐹', '.rs': '🦀', '.rb': '💎',
        '.sql': '🗃️', '.xml': '📰', '.svg': '🖼️',
        '.png': '🖼️', '.jpg': '🖼️', '.jpeg': '🖼️', '.gif': '🖼️',
    }
    if filename == 'Dockerfile':
        return '🐳'
    if filename == 'Makefile':
        return '🔨'
    if filename == 'README.md':
        return '📖'
    if filename.startswith('.'):
        return '⚙️'
    return icon_map.get(ext, '📄')


# ==================== Error Handler ====================
def handle_error(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            traceback.print_exc()
            return jsonify({'error': str(e)}), 500
    return wrapper


# ==================== Platform Detection ====================
import platform

IS_WINDOWS = platform.system() == 'Windows'
IS_MACOS = platform.system() == 'Darwin'
IS_LINUX = platform.system() == 'Linux'
IS_TERMUX = 'termux' in os.environ.get('PREFIX', '').lower() or 'com.termux' in os.environ.get('HOME', '').lower()

PLATFORM_NAME = platform.system()  # 'Windows', 'Linux', 'Darwin'
PLATFORM_DETAIL = platform.platform(terse=True)  # e.g. 'Linux-5.15.0-x86_64', 'Windows-10-10.0.19045-SP0'


def get_system_info():
    """Return a human-readable summary of the current system environment."""
    info_parts = [f"OS: {PLATFORM_NAME}"]
    if IS_WINDOWS:
        info_parts.append(f"Platform: {PLATFORM_DETAIL}")
    elif IS_LINUX:
        info_parts.append(f"Platform: {PLATFORM_DETAIL}")
        if IS_TERMUX:
            info_parts.append("Environment: Termux (Android)")
    elif IS_MACOS:
        info_parts.append(f"Platform: {PLATFORM_DETAIL}")

    info_parts.append(f"Python: {platform.python_version()}")
    info_parts.append(f"Architecture: {platform.machine()}")

    # Shell environment
    if IS_WINDOWS:
        info_parts.append("Shell: cmd.exe / PowerShell (Windows)")
    elif IS_TERMUX:
        info_parts.append("Shell: bash (Termux)")
    else:
        info_parts.append("Shell: bash")

    return '\n'.join(info_parts)


def get_default_shell():
    """Return the default shell command for the current platform.
    On Windows, returns 'cmd' for basic commands or 'powershell' for advanced usage.
    On Unix-like, returns 'bash'."""
    if IS_WINDOWS:
        return 'cmd'
    return 'bash'


def get_default_compiler():
    """Return the default Python compiler command for the current platform."""
    if IS_WINDOWS:
        return 'python'  # Windows uses 'python' not 'python3'
    return 'python3'


# ==================== Helper ====================
def shlex_quote(s):
    """Platform-aware shell quoting.
    On Windows, uses double-quote style quoting (cmd.exe).
    On Unix-like, uses POSIX single-quote quoting (bash/sh)."""
    if not s:
        return '""' if IS_WINDOWS else "''"
    if IS_WINDOWS:
        # Windows cmd.exe quoting: wrap in double quotes, escape inner double quotes
        escaped = s.replace('"', '\\"')
        return f'"{escaped}"'
    else:
        # POSIX sh/bash quoting: wrap in single quotes, handle embedded single quotes
        return "'" + s.replace("'", "'\\''") + "'"


def get_git_executable():
    """Return the git executable path or 'git'.
    On Windows, git.exe is commonly available via PATH or Git for Windows."""
    return 'git'
