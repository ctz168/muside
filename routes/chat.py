"""
MusIDE - LLM Chat + AI Agent routes.
"""

import os
import json
import re
import time
import platform
import shutil
import tempfile
import subprocess
import hashlib
from routes.ast_index import (extract_definitions, find_references_ast, get_file_structure,
                               project_index)
import fnmatch
import threading
import queue
import glob as _glob
import concurrent.futures
import urllib.request
import urllib.error
import urllib.parse
from collections import deque

# Custom redirect handler that follows 307/308 for POST requests
class _PostRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if code in (307, 308):
            # Preserve POST method and body
            return urllib.request.Request(newurl, data=req.data, headers=req.headers,
                                          method=req.method, origin_req_host=req.origin_req_host)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

_urllib_opener = urllib.request.build_opener(_PostRedirectHandler)
from datetime import datetime
from flask import Blueprint, jsonify, request, Response
from utils import (
    handle_error, load_config, load_llm_config, save_llm_config,
    get_active_llm_config,
    load_chat_history, save_chat_history,
    load_conversations, save_conversations, get_conversation, save_conversation, delete_conversation,
    WORKSPACE, SERVER_DIR,
    get_file_type, shlex_quote,
    get_system_info, IS_WINDOWS, get_default_shell,
    log_write,
)
from routes.git import git_cmd
from routes.browser import create_browser_command, wait_browser_result

bp = Blueprint('chat', __name__)

# ==================== Global Active Task State ====================
_active_task = {
    'running': False,
    'cancelled': False,       # True when user requests cancellation
    'conv_id': None,
    'message': None,
    'model_index': None,
    'started_at': None,
    'event_queue': None,       # queue.Queue for broadcasting events to subscribers
    'event_buffer': None,      # deque-based ring buffer of last 100 raw SSE event strings
    'subscribers': 0,          # count of active SSE subscribers
    'lock': threading.Lock(),
    'thread': None,            # background thread running the agent loop
}

# ==================== System Prompt ====================
# Build system environment info for the system prompt
_SYSTEM_ENV_INFO = get_system_info()
_PLATFORM_NAME = 'Windows' if IS_WINDOWS else ('macOS' if platform.system() == 'Darwin' else 'Linux')
_DEFAULT_COMPILER = 'python' if IS_WINDOWS else 'python3'
_SERVER_DIR = SERVER_DIR
_IDE_PORT = os.environ.get('MUSIDE_PORT', '12345')

RING_BUFFER_SIZE = 100

# Debug store for last LLM payload (used when errors need raw payload inspection)
_last_llm_payload_debug = {}

# Load system prompt template from external file (routes/system_prompt.txt)
# The template uses {_IDE_PORT} as placeholder, injected at load time.
_SYSTEM_PROMPT_TEMPLATE = None  # raw template string (loaded once, cached)
_SYSTEM_PROMPT_MTIME = None     # file modification time for hot-reload

def _load_system_prompt_template():
    """Load system prompt template from system_prompt.txt, cache it, and inject runtime variables.
    
    - First call: reads file, caches template and mtime, returns rendered prompt.
    - Subsequent calls: checks mtime — if file changed, re-reads and re-caches.
    - Uses str.format_map() to inject {_IDE_PORT} etc.
    - Falls back to a minimal hardcoded prompt if file is missing.
    """
    global _SYSTEM_PROMPT_TEMPLATE, _SYSTEM_PROMPT_MTIME
    
    _prompt_file = os.path.join(os.path.dirname(__file__), 'system_prompt.txt')
    
    # Check if file has been modified (hot-reload support)
    try:
        current_mtime = os.path.getmtime(_prompt_file)
    except OSError:
        current_mtime = None
    
    if current_mtime is not None and _SYSTEM_PROMPT_TEMPLATE is not None and _SYSTEM_PROMPT_MTIME == current_mtime:
        # Cache hit — file unchanged, just re-render with current variables
        return _SYSTEM_PROMPT_TEMPLATE.format_map({'_IDE_PORT': _IDE_PORT})
    
    # Cache miss or file changed — load from file
    _file_changed = (_SYSTEM_PROMPT_MTIME is not None and current_mtime != _SYSTEM_PROMPT_MTIME)
    if current_mtime is not None:
        try:
            with open(_prompt_file, 'r', encoding='utf-8') as f:
                _SYSTEM_PROMPT_TEMPLATE = f.read().strip()
            _SYSTEM_PROMPT_MTIME = current_mtime
            log_write(f'[muside] System prompt loaded from {_prompt_file} ({len(_SYSTEM_PROMPT_TEMPLATE)} chars, mtime={current_mtime})')
            # Invalidate _SYSTEM_PROMPT_CACHE so next API call picks up the new prompt
            if _file_changed:
                try:
                    _SYSTEM_PROMPT_CACHE.clear()
                    log_write(f'[muside] System prompt file changed — cleared API cache')
                except NameError:
                    pass  # _SYSTEM_PROMPT_CACHE not defined yet (first load)
        except Exception as e:
            log_write(f'[muside] ERROR loading system_prompt.txt: {e}, using fallback')
            if _SYSTEM_PROMPT_TEMPLATE is None:
                _SYSTEM_PROMPT_TEMPLATE = (
                    'You are MusIDE AI Agent, a powerful coding assistant integrated in a mobile IDE.\n'
                    'You have access to specialized tools for reading, writing, editing, searching, and managing code projects.\n'
                    f'CRITICAL: NEVER stop the IDE server (port {_IDE_PORT}) or use kill_port on it.\n'
                )
    else:
        # File doesn't exist — use fallback
        if _SYSTEM_PROMPT_TEMPLATE is None:
            _SYSTEM_PROMPT_TEMPLATE = (
                'You are MusIDE AI Agent, a powerful coding assistant integrated in a mobile IDE.\n'
                'You have access to specialized tools for reading, writing, editing, searching, and managing code projects.\n'
                f'CRITICAL: NEVER stop the IDE server (port {_IDE_PORT}) or use kill_port on it.\n'
            )
            log_write(f'[muside] WARNING: system_prompt.txt not found at {_prompt_file}, using fallback prompt')
    
    # Render template with runtime variables
    return _SYSTEM_PROMPT_TEMPLATE.format_map({'_IDE_PORT': _IDE_PORT})

# Initialize DEFAULT_SYSTEM_PROMPT at module load time
# NOTE: For hot-reload, always call _load_system_prompt_template() to get the latest prompt.
# DEFAULT_SYSTEM_PROMPT is kept as a static snapshot for backward compatibility (e.g. comparisons).
DEFAULT_SYSTEM_PROMPT = _load_system_prompt_template()

# ==================== Tool Definitions ====================
# NOTE: Keep descriptions concise! Payload must stay under ~28KB for ModelScope API compatibility.
AGENT_TOOLS = [
    # -- Task Planning --
    {
        'type': 'function',
        'function': {
            'name': 'todo_write',
            'description': 'Create/update task plan. Use BEFORE any complex multi-step task. Status: pending/in_progress/completed.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'todos': {
                        'type': 'array',
                        'description': 'Updated todo list (replaces entire list)',
                        'items': {
                            'type': 'object',
                            'properties': {
                                'id': {'type': 'string', 'description': 'Unique id'},
                                'content': {'type': 'string', 'description': 'Task description'},
                                'status': {'type': 'string', 'enum': ['pending', 'in_progress', 'completed'], 'description': 'Status'},
                                'priority': {'type': 'string', 'enum': ['high', 'medium', 'low'], 'description': 'Priority'},
                            },
                            'required': ['id', 'content', 'status'],
                        },
                    },
                },
                'required': ['todos'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'todo_read',
            'description': 'Read the current todo list with status and priority.',
            'parameters': {'type': 'object', 'properties': {}, 'required': []},
        },
    },
    # -- File Operations --
    {
        'type': 'function',
        'function': {
            'name': 'read_file',
            'description': 'Read file with line numbers. Auto-detects encoding. Use offset_line/limit_lines for large files. Max 10MB.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': 'Absolute file path'},
                    'offset_line': {'type': 'integer', 'description': 'Start line (1-based). Default: 1', 'default': 1},
                    'limit_lines': {'type': 'integer', 'description': 'Max lines to read'},
                },
                'required': ['path'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'write_file',
            'description': 'Write full content to file (creates dirs, overwrites). For edits, prefer edit_file.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': 'Absolute file path'},
                    'content': {'type': 'string', 'description': 'Content to write'},
                    'create_dirs': {'type': 'boolean', 'description': 'Create parent dirs. Default: true', 'default': True},
                },
                'required': ['path', 'content'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'edit_file',
            'description': 'Search-and-replace in a file. Use old_text/new_text for single edit, or replacements array for atomic multi-edit. Use line_hint and fuzzy_match for reliable matching.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': 'Absolute file path'},
                    'old_text': {'type': 'string', 'description': 'Exact text to find'},
                    'new_text': {'type': 'string', 'description': 'Replacement text'},
                    'line_hint': {'type': 'integer', 'description': 'Approximate line number hint (1-based)'},
                    'fuzzy_match': {'type': 'boolean', 'description': 'Tolerate whitespace differences. Default: false', 'default': False},
                    'replacements': {
                        'type': 'array',
                        'description': 'Array of {old_text, new_text, line_hint?} for atomic multi-edit. Exclusive with old_text/new_text.',
                        'items': {
                            'type': 'object',
                            'properties': {
                                'old_text': {'type': 'string', 'description': 'Text to find'},
                                'new_text': {'type': 'string', 'description': 'Replacement'},
                                'line_hint': {'type': 'integer', 'description': 'Line hint (1-based)'},
                            },
                            'required': ['old_text', 'new_text'],
                        },
                    },
                },
                'required': ['path'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'append_file',
            'description': 'Append content to end of existing file. Auto-adds trailing newline.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': 'File path (must exist)'},
                    'content': {'type': 'string', 'description': 'Content to append'},
                },
                'required': ['path', 'content'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'list_directory',
            'description': 'List files/dirs with sizes and types. Hidden files excluded by default.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': 'Directory path. Default: project dir', 'default': '.'},
                    'show_hidden': {'type': 'boolean', 'description': 'Show dot files. Default: false', 'default': False},
                },
                'required': [],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'glob_files',
            'description': 'Find files by glob pattern (e.g. "**/*.py"). Fast, sorted by mtime. Up to 100 results.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'pattern': {'type': 'string', 'description': 'Glob pattern (e.g. "**/*.py")'},
                    'path': {'type': 'string', 'description': 'Base directory. Default: project dir'},
                },
                'required': ['pattern'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'search_files',
            'description': 'Search text/regex across files. Returns paths, line numbers, content. Skips .git/node_modules.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'pattern': {'type': 'string', 'description': 'Text or regex pattern'},
                    'path': {'type': 'string', 'description': 'Root dir. Default: project dir', 'default': '.'},
                    'include': {'type': 'string', 'description': 'File glob filter (e.g. "*.py")'},
                    'max_results': {'type': 'integer', 'description': 'Max results. Default: 50', 'default': 50},
                },
                'required': ['pattern'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'grep_code',
            'description': 'Regex search with context lines. Returns matches with surrounding code. Supports include/exclude filters.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'pattern': {'type': 'string', 'description': 'Regex pattern'},
                    'path': {'type': 'string', 'description': 'Root dir. Default: project dir'},
                    'context_lines': {'type': 'integer', 'description': 'Context lines. Default: 2', 'default': 2},
                    'include': {'type': 'string', 'description': 'Include glob (e.g. "*.py")'},
                    'exclude': {'type': 'string', 'description': 'Exclude glob (e.g. "*.min.js")'},
                },
                'required': ['pattern'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'find_definition',
            'description': 'AST-based symbol definition lookup. Returns file, line, kind (function/class/method), parent. More precise than grep.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'symbol': {'type': 'string', 'description': 'Symbol name (e.g. "MyClass", "process_data")'},
                    'path': {'type': 'string', 'description': 'Search directory. Default: project dir'},
                },
                'required': ['symbol'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'find_references',
            'description': 'AST-based reference finder. Excludes definitions, strings, comments. For refactoring and dependency analysis.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'symbol': {'type': 'string', 'description': 'Symbol name'},
                    'path': {'type': 'string', 'description': 'Search directory. Default: project dir'},
                    'include_tests': {'type': 'boolean', 'description': 'Include test files. Default: true'},
                },
                'required': ['symbol'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'file_structure',
            'description': 'AST outline of a source file: classes, functions, methods, imports. Supports Python/JS/TS/Go.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': 'Source file path'},
                },
                'required': ['path'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'file_info',
            'description': 'File/directory metadata: size, dates, type, permissions.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': 'Absolute path'},
                },
                'required': ['path'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'create_directory',
            'description': 'Create directory with parents (mkdir -p). Succeeds silently if exists.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': 'Directory path to create'},
                },
                'required': ['path'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'delete_path',
            'description': 'Delete file/directory. Destructive! Use recursive=true for dirs. Cannot delete workspace root.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': 'Path to delete'},
                    'recursive': {'type': 'boolean', 'description': 'Delete dir contents. Default: false', 'default': False},
                },
                'required': ['path'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'move_file',
            'description': 'Move/rename file or directory. Auto-creates destination dir. Updates AST index.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'source': {'type': 'string', 'description': 'Source path'},
                    'destination': {'type': 'string', 'description': 'Destination path'},
                },
                'required': ['source', 'destination'],
            },
        },
    },
    # -- Git --
    {
        'type': 'function',
        'function': {
            'name': 'git_status',
            'description': 'Git repo status: branch, staged/modified/untracked files.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'repo_path': {'type': 'string', 'description': 'Repo path. Default: project dir', 'default': '.'},
                },
                'required': [],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'git_diff',
            'description': 'Show git diff (staged/unstaged/specific file) in unified format.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'repo_path': {'type': 'string', 'description': 'Repo path. Default: project dir'},
                    'staged': {'type': 'boolean', 'description': 'Show staged changes. Default: false', 'default': False},
                    'file_path': {'type': 'string', 'description': 'Specific file diff'},
                },
                'required': [],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'git_commit',
            'description': 'Stage all and commit. Use add_all=false for staged-only commit.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'message': {'type': 'string', 'description': 'Commit message'},
                    'repo_path': {'type': 'string', 'description': 'Repo path. Default: project dir'},
                    'add_all': {'type': 'boolean', 'description': 'Stage all first. Default: true', 'default': True},
                },
                'required': ['message'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'git_log',
            'description': 'Show recent commit history in oneline format.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'count': {'type': 'integer', 'description': 'Number of commits. Default: 10', 'default': 10},
                    'repo_path': {'type': 'string', 'description': 'Repo path. Default: project dir', 'default': '.'},
                },
                'required': [],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'git_checkout',
            'description': 'Switch branch or restore working tree files.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'branch': {'type': 'string', 'description': 'Branch or ref to checkout'},
                    'repo_path': {'type': 'string', 'description': 'Repo path. Default: project dir', 'default': '.'},
                },
                'required': ['branch'],
            },
        },
    },
    # -- Packages --
    {
        'type': 'function',
        'function': {
            'name': 'install_package',
            'description': 'Install package via pip/npm. Auto-detects manager and venv. Supports version specs.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'package_name': {'type': 'string', 'description': 'Package name (e.g. "flask", "numpy>=1.24")'},
                    'manager': {'type': 'string', 'description': '"pip"/"npm"/"auto". Default: auto', 'default': 'auto'},
                },
                'required': ['package_name'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'list_packages',
            'description': 'List installed packages with versions. Uses venv pip if configured.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'manager': {'type': 'string', 'description': '"pip" or "npm". Default: "pip"', 'default': 'pip'},
                },
                'required': [],
            },
        },
    },
    # -- Web --
    {
        'type': 'function',
        'function': {
            'name': 'web_search',
            'description': 'Search the web via DuckDuckGo. Returns titles, URLs, snippets.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string', 'description': 'Search query'},
                },
                'required': ['query'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'web_fetch',
            'description': 'Fetch web page as plain text. Strips HTML. Max 10000 chars.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'url': {'type': 'string', 'description': 'URL to fetch'},
                },
                'required': ['url'],
            },
        },
    },
    # -- Browser/Preview --
    {
        'type': 'function',
        'function': {
            'name': 'browser_navigate',
            'description': 'Open URL in the built-in preview iframe.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'url': {'type': 'string', 'description': 'URL to navigate to'},
                },
                'required': ['url'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'browser_evaluate',
            'description': 'Run JavaScript in the preview page. Returns result. Full DOM access. Requires same-origin.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'expression': {'type': 'string', 'description': 'JS expression (e.g. "document.title")'},
                },
                'required': ['expression'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'browser_inspect',
            'description': 'Inspect DOM element by CSS selector. Returns tag, attrs, styles, position, visibility.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'selector': {'type': 'string', 'description': 'CSS selector (e.g. "#btn", ".nav-item")'},
                },
                'required': ['selector'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'browser_query_all',
            'description': 'List elements matching CSS selector. Up to 50 results with tag, id, class, text, position.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'selector': {'type': 'string', 'description': 'CSS selector (e.g. "button", ".card")'},
                },
                'required': ['selector'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'browser_click',
            'description': 'Click an element in the preview by CSS selector.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'selector': {'type': 'string', 'description': 'CSS selector of element to click'},
                },
                'required': ['selector'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'browser_input',
            'description': 'Type text into input/textarea. React/Vue compatible. Triggers input+change events.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'selector': {'type': 'string', 'description': 'CSS selector of input element'},
                    'text': {'type': 'string', 'description': 'Text to type'},
                },
                'required': ['selector', 'text'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'browser_console',
            'description': 'Get captured console output (log/warn/error) from preview. Last 100 entries.',
            'parameters': {'type': 'object', 'properties': {}, 'required': []},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'browser_page_info',
            'description': 'Page info: title, URL, viewport, scroll position.',
            'parameters': {'type': 'object', 'properties': {}, 'required': []},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'browser_cookies',
            'description': 'Read cookies from preview page. Same-origin only.',
            'parameters': {'type': 'object', 'properties': {}, 'required': []},
        },
    },
    # -- Server & QA --
    {
        'type': 'function',
        'function': {
            'name': 'server_logs',
            'description': 'Read IDE server logs for backend errors and exceptions.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'count': {'type': 'integer', 'description': 'Recent log lines. Default: 50', 'default': 50},
                },
                'required': [],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'run_linter',
            'description': 'Run linter. Auto-detects: Python(ruff/flake8), JS(eslint), Go(go vet). Returns structured issues.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': 'File/dir to lint. Default: project dir'},
                    'linter': {'type': 'string', 'description': 'Force linter (e.g. "ruff", "eslint"). Default: auto'},
                    'severity': {'type': 'string', 'enum': ['all', 'error', 'warning'], 'description': 'Min severity. Default: "warning"'},
                },
                'required': [],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'run_tests',
            'description': 'Run tests. Auto-detects: Python(pytest), JS(jest/vitest), Go(go test). Returns pass/fail summary.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': 'Test dir/file. Default: project dir'},
                    'framework': {'type': 'string', 'description': 'Force framework (e.g. "pytest"). Default: auto'},
                    'filter': {'type': 'string', 'description': 'Test name filter'},
                    'verbose': {'type': 'boolean', 'description': 'Show passing tests. Default: false'},
                },
                'required': [],
            },
        },
    },
    # -- Sub-Agents --
    {
        'type': 'function',
        'function': {
            'name': 'delegate_task',
            'description': 'Launch sub-agent for independent subtask. Mode "read"=exploration only (safe), "write"=full access. Max 15 iterations.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'task': {'type': 'string', 'description': 'Subtask description'},
                    'mode': {'type': 'string', 'enum': ['read', 'write'], 'description': '"read" (safe, default) or "write"', 'default': 'read'},
                    'max_iterations': {'type': 'integer', 'description': 'Max iterations 1-15. Default: 8'},
                    'context': {'type': 'string', 'description': 'Optional context for the sub-agent'},
                },
                'required': ['task'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'parallel_tasks',
            'description': 'Run 2-4 independent sub-agents in parallel. Each has own context. Tasks must NOT modify same files. Max 4.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'tasks': {
                        'type': 'array',
                        'description': 'Tasks to run in parallel (max 4)',
                        'items': {
                            'type': 'object',
                            'properties': {
                                'task': {'type': 'string', 'description': 'Subtask description'},
                                'mode': {'type': 'string', 'enum': ['read', 'write'], 'description': '"read" or "write"'},
                                'max_iterations': {'type': 'integer', 'description': 'Max iterations 1-15. Default: 8'},
                                'context': {'type': 'string', 'description': 'Optional context'},
                            },
                            'required': ['task'],
                        },
                    },
                },
                'required': ['tasks'],
            },
        },
    },
    # -- Process Management --
    {
        'type': 'function',
        'function': {
            'name': 'kill_port',
            'description': 'Kill process on a port. Use BEFORE starting servers to avoid "port in use" errors.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'port': {'type': 'integer', 'description': 'Port number (e.g. 5000, 8080)'},
                },
                'required': ['port'],
            },
        },
    },
    # -- Shell --
    {
        'type': 'function',
        'function': {
            'name': 'run_command',
            'description': 'Execute shell command. Best for: dev servers, compiling, scripts. Prefer specialized tools for reading/editing/searching files.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'command': {'type': 'string', 'description': 'Shell command'},
                    'timeout': {'type': 'integer', 'description': 'Timeout in seconds. Default: 120', 'default': 120},
                    'cwd': {'type': 'string', 'description': 'Working directory. Default: project dir'},
                },
                'required': ['command'],
            },
        },
    },
    # -- Audio / Music Production --
    {
        'type': 'function',
        'function': {
            'name': 'play_audio',
            'description': 'Start playback on the track editor. Plays all unmuted tracks from current position. Use track_id to play a specific track only.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'track_id': {'type': 'string', 'description': 'Optional specific track ID to play. Default: play all tracks'},
                },
                'required': [],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'stop_audio',
            'description': 'Stop all audio playback and reset position to start.',
            'parameters': {'type': 'object', 'properties': {}, 'required': []},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'pause_audio',
            'description': 'Pause current audio playback (can resume from paused position).',
            'parameters': {'type': 'object', 'properties': {}, 'required': []},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'seek_audio',
            'description': 'Seek the playback position to a specific time.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'time': {'type': 'number', 'description': 'Time position in seconds'},
                },
                'required': ['time'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'load_audio',
            'description': 'Load an audio file into a track. Supports WAV, MP3, OGG, FLAC. Returns track info and duration.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'track_id': {'type': 'string', 'description': 'Track ID to load audio into'},
                    'file_path': {'type': 'string', 'description': 'Path to audio file (absolute or relative to project)'},
                    'start_time': {'type': 'number', 'description': 'Start time in seconds on the timeline. Default: 0', 'default': 0},
                },
                'required': ['track_id', 'file_path'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'edit_audio',
            'description': 'Edit audio clip properties. Supports: trim, fade_in, fade_out, normalize, reverse, change_speed, change_pitch.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'track_id': {'type': 'string', 'description': 'Track ID'},
                    'clip_index': {'type': 'integer', 'description': 'Clip index on the track (0-based)'},
                    'action': {'type': 'string', 'enum': ['trim', 'fade_in', 'fade_out', 'normalize', 'reverse', 'change_speed', 'change_pitch'], 'description': 'Edit action'},
                    'start': {'type': 'number', 'description': 'Trim start time in seconds (for trim action)'},
                    'end': {'type': 'number', 'description': 'Trim end time in seconds (for trim action)'},
                    'duration': {'type': 'number', 'description': 'Fade duration in seconds (for fade_in/fade_out)'},
                    'factor': {'type': 'number', 'description': 'Speed/pitch factor (for change_speed/change_pitch, 1.0 = original)'},
                },
                'required': ['track_id', 'clip_index', 'action'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'export_audio',
            'description': 'Export project or selected tracks as audio file. Uses ffmpeg for mixing and rendering.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'output_path': {'type': 'string', 'description': 'Output file path (e.g. "mixdown.wav")'},
                    'format': {'type': 'string', 'enum': ['wav', 'mp3', 'ogg', 'flac'], 'description': 'Output format. Default: wav', 'default': 'wav'},
                    'track_ids': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Optional list of track IDs to export. Default: all tracks'},
                    'start_time': {'type': 'number', 'description': 'Export start time in seconds. Default: 0'},
                    'end_time': {'type': 'number', 'description': 'Export end time in seconds. Default: project end'},
                },
                'required': ['output_path'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'record_audio',
            'description': 'Start or stop recording from microphone into a track.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'track_id': {'type': 'string', 'description': 'Track ID to record into'},
                    'action': {'type': 'string', 'enum': ['start', 'stop'], 'description': 'Start or stop recording'},
                    'duration': {'type': 'number', 'description': 'Recording duration in seconds (for timed recording). Default: manual stop'},
                },
                'required': ['track_id', 'action'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'list_tracks',
            'description': 'List all tracks in the project with their properties (name, volume, pan, mute, solo, clips).',
            'parameters': {'type': 'object', 'properties': {}, 'required': []},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'add_track',
            'description': 'Add a new track to the project.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'name': {'type': 'string', 'description': 'Track name (e.g. "Drums", "Bass", "Vocals")'},
                    'color': {'type': 'string', 'description': 'Track color hex (e.g. "#ff6b6b"). Default: auto-assign'},
                },
                'required': ['name'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'remove_track',
            'description': 'Remove a track from the project.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'track_id': {'type': 'string', 'description': 'Track ID to remove'},
                },
                'required': ['track_id'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'set_track_volume',
            'description': 'Set track volume level.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'track_id': {'type': 'string', 'description': 'Track ID'},
                    'volume': {'type': 'number', 'description': 'Volume level 0.0 (silent) to 1.0 (max)'},
                },
                'required': ['track_id', 'volume'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'set_track_pan',
            'description': 'Set track stereo pan position.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'track_id': {'type': 'string', 'description': 'Track ID'},
                    'pan': {'type': 'number', 'description': 'Pan -1.0 (full left) to 1.0 (full right), 0.0 = center'},
                },
                'required': ['track_id', 'pan'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'set_track_mute',
            'description': 'Mute or unmute a track.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'track_id': {'type': 'string', 'description': 'Track ID'},
                    'muted': {'type': 'boolean', 'description': 'True to mute, False to unmute'},
                },
                'required': ['track_id', 'muted'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'set_track_solo',
            'description': 'Solo or unsolo a track.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'track_id': {'type': 'string', 'description': 'Track ID'},
                    'soloed': {'type': 'boolean', 'description': 'True to solo, False to unsolo'},
                },
                'required': ['track_id', 'soloed'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'set_bpm',
            'description': 'Set the project tempo in BPM (beats per minute).',
            'parameters': {
                'type': 'object',
                'properties': {
                    'bpm': {'type': 'number', 'description': 'Tempo in BPM (20-300)'},
                },
                'required': ['bpm'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'set_time_signature',
            'description': 'Set the project time signature.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'numerator': {'type': 'integer', 'description': 'Top number (beats per measure, e.g. 4)'},
                    'denominator': {'type': 'integer', 'description': 'Bottom number (note value, e.g. 4)'},
                },
                'required': ['numerator', 'denominator'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_project_info',
            'description': 'Get current project state: BPM, time signature, tracks, duration, etc.',
            'parameters': {'type': 'object', 'properties': {}, 'required': []},
        },
    },
]

# ==================== Compact Tool Variants ====================
# Some API providers (e.g., ModelScope) have request body size limits that
# can cause "Unterminated string" errors with the full 42-tool set.
# We maintain a compact variant with shortened descriptions for fallback.

def _make_compact_tools(full_tools, max_desc_len=60):
    """Create a compact copy of tool definitions with truncated descriptions.
    Keeps all parameter schemas intact — only shortens description strings."""
    compact = []
    for t in full_tools:
        f = t['function']
        desc = f['description']
        if len(desc) > max_desc_len:
            # Truncate at last sentence boundary or space before limit
            truncated = desc[:max_desc_len]
            last_period = truncated.rfind('.')
            last_space = truncated.rfind(' ')
            cut = max(last_period, last_space, max_desc_len - 20)
            if cut > max_desc_len // 2:
                desc = truncated[:cut + 1].rstrip() + '..'
            else:
                desc = truncated.rstrip() + '..'
        compact.append({
            'type': 'function',
            'function': {
                'name': f['name'],
                'description': desc,
                'parameters': f['parameters'],
            },
        })
    return compact

def _make_minimal_tools(full_tools):
    """Create minimal tool definitions — name + ultra-short description, no parameter details.
    Used as last resort when even compact tools cause payload size errors."""
    minimal = []
    for t in full_tools:
        f = t['function']
        desc = f['description']
        # Keep first sentence only
        first_period = desc.find('.')
        if first_period > 0 and first_period < 80:
            desc = desc[:first_period + 1]
        elif len(desc) > 80:
            desc = desc[:77] + '...'
        minimal.append({
            'type': 'function',
            'function': {
                'name': f['name'],
                'description': desc,
                'parameters': {'type': 'object', 'properties': {}, 'required': list(f['parameters'].get('required', []))},
            },
        })
    return minimal

# Pre-compute compact/minimal tool variants
AGENT_TOOLS_COMPACT = _make_compact_tools(AGENT_TOOLS)
AGENT_TOOLS_MINIMAL = _make_minimal_tools(AGENT_TOOLS)


def _get_project_dir():
    """Get the current project directory (or workspace if no project is open)."""
    try:
        from utils import load_config
        config = load_config()
        ws = config.get('workspace', WORKSPACE)
        project = config.get('project', None)
        if project:
            candidate = os.path.realpath(os.path.join(ws, project))
            if os.path.isdir(candidate):
                return candidate
        return os.path.realpath(ws)
    except Exception:
        return os.path.realpath(WORKSPACE)

def _resolve_path(raw_path):
    """Resolve a possibly-relative path to an absolute path within the project.
    
    If raw_path is already absolute, return as-is.
    If relative, resolve from the current project directory (or workspace root).
    This is a pre-processing step before _validate_path().
    """
    if not raw_path:
        return raw_path
    if os.path.isabs(raw_path):
        return raw_path
    try:
        config = load_config()
        _ws = config.get('workspace', WORKSPACE)
        _prj = config.get('project', None)
        if _prj:
            _base = os.path.join(_ws, _prj)
            if os.path.isdir(_base):
                return os.path.join(_base, raw_path)
        return os.path.join(_ws, raw_path)
    except Exception:
        return raw_path

def _validate_path(path):
    """Ensure path stays within WORKSPACE or configured project directory.
    Returns resolved absolute path or raises ValueError."""
    real_path = os.path.realpath(path)
    # Check against all allowed roots: default WORKSPACE, configured workspace, and project dir
    allowed_roots = [os.path.realpath(WORKSPACE)]
    try:
        from utils import load_config
        config = load_config()
        cfg_ws = config.get('workspace', '')
        if cfg_ws:
            allowed_roots.append(os.path.realpath(cfg_ws))
        project = config.get('project', None)
        if project and cfg_ws:
            candidate = os.path.realpath(os.path.join(cfg_ws, project))
            if os.path.isdir(candidate):
                allowed_roots.append(candidate)
    except Exception:
        pass
    # Deduplicate
    allowed_roots = list(dict.fromkeys(allowed_roots))
    for root in allowed_roots:
        if real_path == root or real_path.startswith(root + os.sep):
            return real_path
    raise ValueError(f'Access denied: path "{path}" is outside workspace (allowed: {", ".join(allowed_roots)})')


def _truncate(text, limit=30000, tail=3000):
    """Truncate text to limit characters, keeping head and tail for context."""
    if len(text) > limit:
        kept_head = text[:limit]
        kept_tail = text[-tail:] if tail > 0 else ''
        parts = [kept_head]
        parts.append(f'\n\n[... truncated: showing first {limit} of {len(text)} characters ...]')
        if kept_tail:
            parts.append(f'\n\n[... last {tail} characters ...]\n{kept_tail}')
        return ''.join(parts)
    return text

# ==================== Tool Execution ====================
def _tool_read_file(args):
    path = _validate_path(args['path'])
    if not os.path.isfile(path):
        return f'Error: File not found: {path}'
    size = os.path.getsize(path)
    if size > 10 * 1024 * 1024:
        return f'Error: File too large ({size} bytes, max 10MB)'
    offset = args.get('offset_line', 1) - 1  # convert to 0-based
    limit = args.get('limit_lines')
    try:
        with open(path, 'rb') as f:
            raw = f.read()
        encodings = ['utf-8', 'utf-8-sig', 'gbk', 'gb2312', 'latin-1']
        content = None
        used_enc = 'utf-8'
        for enc in encodings:
            try:
                content = raw.decode(enc)
                used_enc = enc
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if content is None:
            content = raw.decode('utf-8', errors='replace')
        lines = content.split('\n')
        end = (offset + limit) if limit else None
        selected = lines[offset:end]
        header = f'File: {path} (encoding: {used_enc}, size: {size} bytes, total lines: {len(lines)})'
        numbered = []
        for i, line in enumerate(selected, start=offset + 1):
            numbered.append(f'  {i:>6}\t{line}')
        result = header + '\n' + '\n'.join(numbered)
        if end and end < len(lines):
            result += f'\n\n[showing lines {offset+1}-{end} of {len(lines)}]'
        return _truncate(result)
    except Exception as e:
        return f'Error reading file: {str(e)}'

def _sanitize_json_strings(raw_json):
    """Fix common JSON escaping issues that cause json.loads to fail.
    
    Handles two common problems:
    1. Model puts literal newlines/tabs inside JSON string values (JSON spec violation)
    2. Model puts unescaped quotes inside JSON string values (e.g. CSS font-family: "Arial")
    
    Strategy: Walk through the JSON text tracking string state. When we encounter
    a " that would close a string, look ahead with extended context — check whether
    the text after the " looks like valid JSON continuation or content.
    
    Key improvement: When " is followed by , or }, we look FURTHER ahead to see if
    the pattern matches JSON structure (like "key":) or is just content punctuation
    (like CSS font-family: "Segoe UI", sans-serif).
    
    Returns: sanitized JSON text
    """
    if not raw_json or not isinstance(raw_json, str):
        return raw_json
    
    result = []
    in_string = False
    i = 0
    length = len(raw_json)
    
    # Pre-compiled pattern for detecting JSON key pattern after a comma
    import re as _sanitize_re
    _json_key_pattern = _sanitize_re.compile(r'\s*"([^"\\]{0,40})"\s*:')
    
    while i < length:
        ch = raw_json[i]
        
        if ch == '\\' and in_string and i + 1 < length:
            # Already escaped — pass through both characters unchanged
            result.append(ch)
            result.append(raw_json[i + 1])
            i += 2
            continue
        
        if ch == '"':
            if not in_string:
                # Opening a string
                in_string = True
                result.append(ch)
                i += 1
                continue
            
            # We're inside a string and found a ".
            # Check if this is really the end of the string by looking ahead.
            j = i + 1
            while j < length and raw_json[j] in ' \t\n\r':
                j += 1
            
            next_meaningful = raw_json[j] if j < length else ''
            
            if next_meaningful == '':
                # End of string — this must be a closing quote (or truncated)
                in_string = False
                result.append(ch)
            elif next_meaningful == ':':
                # Could be end of a key string (") followed by colon.
                # Verify: the text before this " should be a valid key name.
                _is_key = _looks_like_json_key(result)
                if _is_key:
                    in_string = False
                    result.append(ch)
                else:
                    # Content quote followed by : — escape it
                    result.append('\\"')
            elif next_meaningful in ',}]\n\r':
                # The " is followed by a JSON structural character.
                # BUT: in CSS/HTML/JS content, these characters also appear.
                # Example: font-family: "Segoe UI", sans-serif  — " followed by ,
                
                if next_meaningful == ',':
                    # Check if after the comma, there's a JSON key pattern like "key":
                    _after_comma = raw_json[j+1:] if j + 1 < length else ''
                    _key_match = _json_key_pattern.match(_after_comma)
                    if _key_match:
                        _key_name = _key_match.group(1)
                        _known_keys = {'path', 'content', 'command', 'old_text', 'new_text',
                                       'replacements', 'create_dirs', 'line_hint', 'fuzzy_match',
                                       'description', 'name', 'url', 'method', 'body', 'headers',
                                       'query', 'encoding', 'recursive', 'pattern'}
                        if _key_name in _known_keys or len(_key_name) <= 2:
                            # Short key or known key — likely JSON structure
                            in_string = False
                            result.append(ch)
                        else:
                            # Unknown/long key name after comma — more likely content
                            result.append('\\"')
                    else:
                        # No key pattern after comma — this is content, not JSON
                        result.append('\\"')
                elif next_meaningful in ('}', ']'):
                    # " followed by } or ] — could be JSON closing or content
                    _brace_depth = _count_brace_depth(result)
                    if _brace_depth <= 1:
                        # At or near top level — likely JSON closing
                        in_string = False
                        result.append(ch)
                    else:
                        # Deep inside — likely content } followed by more content
                        result.append('\\"')
                else:
                    # \n or \r — treat as closing
                    in_string = False
                    result.append(ch)
            else:
                # The " is followed by content (not JSON structure) — escape it
                result.append('\\"')
            
            i += 1
            continue
        
        if in_string:
            # Inside a JSON string — replace literal control characters
            if ch == '\n':
                result.append('\\n')
            elif ch == '\r':
                result.append('\\r')
            elif ch == '\t':
                result.append('\\t')
            elif ord(ch) < 0x20:
                result.append(f'\\u{ord(ch):04x}')
            else:
                result.append(ch)
        else:
            result.append(ch)
        
        i += 1
    
    return ''.join(result)


def _looks_like_json_key(result_chars):
    """Check if the text built so far since the last opening " looks like a JSON key."""
    text = ''.join(result_chars)
    last_quote = text.rfind('"')
    if last_quote < 0:
        return False
    key_text = text[last_quote + 1:]
    if len(key_text) > 50:
        return False
    if ' ' in key_text and len(key_text) > 10:
        return False
    _known_keys = {'path', 'content', 'command', 'old_text', 'new_text',
                   'replacements', 'create_dirs', 'line_hint', 'fuzzy_match',
                   'description', 'name', 'url', 'method', 'body', 'headers',
                   'query', 'encoding', 'recursive', 'pattern'}
    if key_text in _known_keys:
        return True
    if len(key_text) <= 30 and key_text.replace('_', '').replace('-', '').isalnum():
        return True
    return False


def _count_brace_depth(result_chars):
    """Count the brace/bracket nesting depth from the result built so far."""
    depth = 0
    in_str = False
    text = ''.join(result_chars) if isinstance(result_chars, list) else result_chars
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '\\' and in_str and i + 1 < len(text):
            i += 2
            continue
        if ch == '"':
            in_str = not in_str
        elif not in_str:
            if ch in '{[':
                depth += 1
            elif ch in '}]':
                depth -= 1
        i += 1
    return depth


def _json_unescape_string(s):
    """Properly unescape a JSON string fragment using json.loads.
    
    This handles all JSON escape sequences correctly (\\n, \\t, \\", \\\\, etc.)
    without the ordering bugs of manual replace() chains.
    """
    try:
        return json.loads('"' + s + '"')
    except (json.JSONDecodeError, Exception):
        # Fallback: manual unescape with correct order
        # IMPORTANT: process \\\\ first, then \\", then \\n/\\t/\\r
        result = s
        result = result.replace('\\\\', '\x00BACKSLASH\x00')
        result = result.replace('\\"', '"')
        result = result.replace('\\n', '\n')
        result = result.replace('\\r', '\r')
        result = result.replace('\\t', '\t')
        result = result.replace('\x00BACKSLASH\x00', '\\')
        return result


def _recover_broken_json_args(raw_args, tool_name):
    """Try to recover path and content from broken JSON tool_call arguments.
    
    When the LLM generates write_file/edit_file with content containing unescaped
    special characters (quotes, newlines, etc. in CSS/HTML/JS), the JSON parse fails.
    This function attempts multiple strategies to extract the key fields.
    
    Strategy order (for write_file/edit_file):
    1. Direct field extraction (most reliable for complex content)
    2. Sanitize + re-parse (good for simple content with control char issues)
    
    Returns: dict with recovered args, or {} if recovery fails
    """
    recovered = {}
    
    if not raw_args or not isinstance(raw_args, str):
        return recovered
    
    import re as _re
    
    # ── Strategy 1: Direct field extraction for write_file ──
    # This is the MOST RELIABLE approach for complex HTML/CSS/JS content.
    if tool_name == 'write_file':
        _path_match = _re.search(r'"path"\s*:\s*"([^"]+)"', raw_args)
        if _path_match:
            recovered['path'] = _path_match.group(1)
        
        _content_start = _re.search(r'"content"\s*:\s*"', raw_args)
        if _content_start:
            content_begin = _content_start.end()
            remaining = raw_args[content_begin:]
            content_text = None
            
            # Method A: Find the JSON closing pattern at the END of the string.
            end_match = _re.search(r'"\s*\}\s*$', remaining, _re.DOTALL)
            if end_match:
                raw_content = remaining[:end_match.start()]
                content_text = _json_unescape_string(raw_content)
                print(f'[LLM] Direct extraction Method A: content_len={len(content_text) if content_text else 0}')
            
            # Method B: Find last "} using rfind
            if not content_text:
                last_quote_brace = remaining.rfind('"}')
                if last_quote_brace > 0:
                    raw_content = remaining[:last_quote_brace]
                    content_text = _json_unescape_string(raw_content)
                    print(f'[LLM] Direct extraction Method B: content_len={len(content_text) if content_text else 0}')
            
            # Method C: Truncated response — strip trailing JSON artifacts
            if not content_text:
                stripped = remaining.rstrip()
                if stripped.endswith('"}'):
                    stripped = stripped[:-2]
                elif stripped.endswith('",'):
                    stripped = stripped[:-2]
                elif stripped.endswith('"'):
                    stripped = stripped[:-1]
                if len(stripped) > 10:
                    content_text = _json_unescape_string(stripped)
                    print(f'[LLM] Direct extraction Method C (truncated): content_len={len(content_text) if content_text else 0}')
            
            if content_text and len(content_text) > 0:
                recovered['content'] = content_text
                recovered['_recovered_from_broken_json'] = True
                return recovered
    
    # ── Strategy 1b: Direct field extraction for edit_file ──
    if tool_name == 'edit_file':
        _path_match = _re.search(r'"path"\s*:\s*"([^"]+)"', raw_args)
        if _path_match:
            recovered['path'] = _path_match.group(1)
        
        # Try regex with escaped-content pattern first
        _old_match = _re.search(r'"old_text"\s*:\s*"((?:[^"\\]|\\.)*)"', raw_args)
        _new_match = _re.search(r'"new_text"\s*:\s*"((?:[^"\\]|\\.)*)"', raw_args)
        if _old_match and _new_match:
            recovered['replacements'] = [{
                'old_text': _json_unescape_string(_old_match.group(1)),
                'new_text': _json_unescape_string(_new_match.group(1)),
            }]
            recovered['_recovered_from_broken_json'] = True
            return recovered
        
        # Broader extraction for edit_file with broken quotes
        _old_start = _re.search(r'"old_text"\s*:\s*"', raw_args)
        _new_start = _re.search(r'"new_text"\s*:\s*"', raw_args)
        if _old_start and _new_start:
            old_begin = _old_start.end()
            new_begin = _new_start.end()
            old_end_marker = raw_args.rfind('"', old_begin, _new_start.start())
            if old_end_marker > old_begin:
                raw_old = raw_args[old_begin:old_end_marker]
                remaining_new = raw_args[new_begin:]
                end_match = _re.search(r'"\s*\}\s*$', remaining_new, _re.DOTALL)
                if end_match:
                    raw_new = remaining_new[:end_match.start()]
                else:
                    last_qb = remaining_new.rfind('"}')
                    raw_new = remaining_new[:last_qb] if last_qb > 0 else remaining_new.rstrip().rstrip('"}').rstrip('"')
                
                old_text = _json_unescape_string(raw_old)
                new_text = _json_unescape_string(raw_new)
                if old_text and new_text:
                    recovered['replacements'] = [{'old_text': old_text, 'new_text': new_text}]
                    recovered['_recovered_from_broken_json'] = True
                    return recovered
    
    # ── Strategy 2: Sanitize + re-parse ──
    # Tried AFTER direct extraction because the sanitizer can sometimes
    # corrupt complex content by misidentifying content quotes as JSON quotes.
    try:
        sanitized = _sanitize_json_strings(raw_args)
        open_braces = sanitized.count('{') - sanitized.count('}')
        open_brackets = sanitized.count('[') - sanitized.count(']')
        if open_braces > 0:
            sanitized += '}' * open_braces
        if open_brackets > 0:
            sanitized += ']' * open_brackets
        result = json.loads(sanitized)
        if isinstance(result, dict) and ('path' in result or 'content' in result):
            print(f'[LLM] Recovered broken JSON by sanitizing (tool: {tool_name})')
            return result
    except (json.JSONDecodeError, Exception) as e:
        print(f'[LLM] Strategy sanitize+reparse failed for {tool_name}: {e}')
    
    # ── Final: Extract path if we still don't have it ──
    if 'path' not in recovered:
        _path_match = _re.search(r'"path"\s*:\s*"([^"]+)"', raw_args)
        if _path_match:
            recovered['path'] = _path_match.group(1)
    
    return recovered


def _parse_tool_args(raw_args, tool_name=''):
    """Parse tool call arguments with robust fallback recovery.
    
    Tries in order:
    1. Direct json.loads (fast path for well-formed JSON)
    2. _sanitize_json_strings + json.loads (fix control chars + unescaped quotes)
    3. _recover_broken_json_args (field extraction for write_file/edit_file)
    
    Returns: (parsed_args_dict, was_recovered_bool)
    """
    # Fast path: try direct parse
    try:
        return json.loads(raw_args), False
    except json.JSONDecodeError:
        pass
    
    # Try sanitize + re-parse
    try:
        sanitized = _sanitize_json_strings(raw_args)
        open_braces = sanitized.count('{') - sanitized.count('}')
        open_brackets = sanitized.count('[') - sanitized.count(']')
        if open_braces > 0:
            sanitized += '}' * open_braces
        if open_brackets > 0:
            sanitized += ']' * open_brackets
        result = json.loads(sanitized)
        if isinstance(result, dict):
            print(f'[LLM] Fixed JSON args by sanitizing (tool: {tool_name})')
            return result, False
    except (json.JSONDecodeError, Exception):
        pass
    
    # Try robust recovery for write_file/edit_file
    if tool_name in ('write_file', 'edit_file'):
        recovered = _recover_broken_json_args(raw_args, tool_name)
        if recovered.get('content') or recovered.get('replacements'):
            print(f'[LLM] Recovered broken JSON args for {tool_name}')
            return recovered, True
        elif recovered.get('path'):
            # Got path but no content — mark as broken
            recovered['_skip_broken_args'] = True
            return recovered, True
    
    # For other tools, try simple path extraction
    tool_args = {}
    try:
        import re as _re_tc
        _path_match = _re_tc.search(r'"path"\s*:\s*"([^"]+)"', raw_args)
        if _path_match:
            tool_args['path'] = _path_match.group(1)
    except Exception:
        pass
    
    return tool_args, False


def _tool_write_file(args):
    # Handle missing/None content gracefully (from broken JSON recovery)
    if args.get('_skip_broken_args'):
        _skip_path = args.get('path', '(unknown)')
        return f'Error: write_file arguments were truncated (path: {_skip_path}). The response was cut off before the file content could be fully generated. Please retry with a smaller file or increase max_tokens.'
    
    # Robust path handling: support relative paths by resolving from project dir
    raw_path = args.get('path', '')
    if not raw_path:
        return 'Error: path is required for write_file'
    raw_path = _resolve_path(raw_path)
    path = _validate_path(raw_path)

    # Handle missing/None content gracefully
    # Note: content can be None if JSON parse failed upstream — check _skip_broken_args first
    if args.get('_skip_broken_args'):
        return f'Error: write_file arguments were truncated (path: {path}). The response was cut off before the file content could be fully generated. Please retry with a smaller file or increase max_tokens.'

    content = args.get('content')
    if content is None:
        print(f'[TOOL] write_file ERROR: content is None for path={path}, args keys={list(args.keys())}')
        return f'Error: content is required for write_file (path: {path}). The file content was not provided — this may be caused by response truncation. Try increasing max_tokens or writing a smaller file.'
    if not isinstance(content, str):
        content = str(content)

    # Log if content was recovered from broken JSON
    if args.get('_recovered_from_broken_json'):
        print(f'[TOOL] write_file: using recovered content from broken JSON (path={path}, content_len={len(content)})')

    create_dirs = args.get('create_dirs', True)
    try:
        if create_dirs:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        # P2-3: Atomic write — write to temp file first, then rename
        # This prevents file corruption if the process crashes mid-write
        _dir = os.path.dirname(path) or '.'
        try:
            fd, tmp_path = tempfile.mkstemp(dir=_dir, suffix='.tmp', prefix='.muside_write_')
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(content)
            os.replace(tmp_path, path)  # atomic on POSIX, near-atomic on Windows
        except Exception:
            # Clean up temp file if replace failed
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
            raise
        # Auto-update AST index for source files
        ext = os.path.splitext(path)[1].lower()
        if ext in ('.py', '.js', '.ts', '.tsx', '.jsx', '.go', '.mjs', '.cjs'):
            try:
                project_index.index_file(path, content.encode('utf-8'))
            except Exception:
                pass
        
        # Auto-lint check for source files (configurable via .muside/rules.md)
        lint_result = ''
        if ext in ('.py', '.js', '.ts', '.tsx', '.jsx', '.go', '.mjs', '.cjs'):
            lint_result = _auto_lint_check(path)
        
        base_msg = f'File written successfully: {path} ({os.path.getsize(path)} bytes)'
        if lint_result:
            return base_msg + '\n\n' + lint_result
        return base_msg
    except ValueError as e:
        return f'Security error writing file {path}: {e}'
    except OSError as e:
        return f'OS error writing file {path}: {e} (errno: {e.errno})'
    except Exception as e:
        return f'Error writing file {path}: {type(e).__name__}: {e}'

def _normalize_whitespace(text):
    """Normalize whitespace for fuzzy matching: strip trailing spaces per line, normalize line endings."""
    lines = text.split('\n')
    return '\n'.join(line.rstrip() for line in lines)

def _find_text_in_content(content, old_text, line_hint=None, fuzzy_match=False):
    """Find old_text in content with optional line_hint and fuzzy_match support.
    
    Returns (found_text, start_pos, line_number) or None if not found.
    - found_text: the actual matched text in the file (may differ from old_text with fuzzy_match)
    - start_pos: character offset in content
    - line_number: 1-based line number of the match
    """
    if fuzzy_match:
        # Normalize both content and old_text for fuzzy comparison
        norm_old = _normalize_whitespace(old_text)
        norm_content = _normalize_whitespace(content)
        
        # Try normalized match first
        idx = norm_content.find(norm_old)
        if idx == -1:
            return None
        
        # Map normalized position back to original content
        # Count characters up to idx in normalized content to find the original position
        norm_lines = norm_content.split('\n')
        orig_lines = content.split('\n')
        
        # Find which line the match starts on
        char_count = 0
        match_line = 0
        for i, line in enumerate(norm_lines):
            if char_count + len(line) + (1 if i > 0 else 0) > idx:
                match_line = i
                break
            char_count += len(line) + (1 if i > 0 else 0)
        else:
            match_line = len(norm_lines) - 1
        
        # Reconstruct the matched text from original lines
        old_text_lines = old_text.split('\n')
        num_lines = len(old_text_lines)
        actual_text = '\n'.join(orig_lines[match_line:match_line + num_lines])
        
        # Calculate start position in original content
        start_pos = sum(len(orig_lines[i]) + 1 for i in range(match_line))
        
        return (actual_text, start_pos, match_line + 1)
    
    # Exact match
    if line_hint and line_hint > 0:
        # Narrow search to a window around line_hint
        lines = content.split('\n')
        hint_idx = max(0, line_hint - 1)  # Convert to 0-based
        window = 50  # Search ±50 lines around hint
        search_start_line = max(0, hint_idx - window)
        search_end_line = min(len(lines), hint_idx + window)
        
        # First try exact match in the window
        window_content = '\n'.join(lines[search_start_line:search_end_line])
        idx = window_content.find(old_text)
        if idx != -1:
            # Map back to full content position
            start_pos = sum(len(lines[i]) + 1 for i in range(search_start_line)) + idx
            # Find line number
            before_match = content[:start_pos]
            line_num = before_match.count('\n') + 1
            return (old_text, start_pos, line_num)
        
        # Fallback: try in full content
        idx = content.find(old_text)
        if idx != -1:
            line_num = content[:idx].count('\n') + 1
            return (old_text, idx, line_num)
        return None
    
    # Standard search
    idx = content.find(old_text)
    if idx != -1:
        line_num = content[:idx].count('\n') + 1
        return (old_text, idx, line_num)
    return None

def _get_context_around_line(content, line_number, context_lines=5):
    """Get context around a specific line number to help AI correct failed matches."""
    lines = content.split('\n')
    target_idx = max(0, line_number - 1)  # 0-based
    start = max(0, target_idx - context_lines)
    end = min(len(lines), target_idx + context_lines + 1)
    result_lines = []
    for i in range(start, end):
        marker = ' →' if i == target_idx else '  '
        result_lines.append(f'{marker} {i+1:>5}\t{lines[i]}')
    return '\n'.join(result_lines)

def _tool_edit_file(args):
    # Robust path handling: support relative paths by resolving from project dir
    raw_path = args.get('path', '')
    if not raw_path:
        return 'Error: path is required for edit_file'
    raw_path = _resolve_path(raw_path)
    path = _validate_path(raw_path)

    # Handle truncated args from JSON parse failure
    if args.get('_skip_broken_args'):
        return f'Error: edit_file arguments were truncated (path: {path}). The response was cut off. Please retry with a smaller edit or increase max_tokens.'

    replacements = args.get('replacements')
    fuzzy_match = args.get('fuzzy_match', False)
    line_hint = args.get('line_hint')
    try:
        if not os.path.isfile(path):
            return f'Error: File not found: {path}'
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()

        # MultiEdit: atomic multi-replacement mode
        if replacements is not None and isinstance(replacements, list) and len(replacements) > 0:
            original_content = content
            total_replacements = 0
            errors = []
            for i, rep in enumerate(replacements):
                old_text = rep.get('old_text', '')
                new_text = rep.get('new_text', '')
                rep_line_hint = rep.get('line_hint')
                if not old_text:
                    errors.append(f'Replacement {i+1}: missing old_text')
                    continue
                
                match_result = _find_text_in_content(content, old_text, 
                                                      line_hint=rep_line_hint, 
                                                      fuzzy_match=fuzzy_match)
                if match_result is None:
                    # Provide context to help AI fix the match
                    hint_line = rep_line_hint or 1
                    context = _get_context_around_line(content, hint_line)
                    errors.append(f'Replacement {i+1}: old_text not found. Context around line {hint_line}:\n{context}')
                    continue
                
                actual_text, start_pos, match_line = match_result
                # Check for ambiguous matches
                remaining = content[start_pos + len(actual_text):]
                second_match = _find_text_in_content(remaining, old_text, fuzzy_match=fuzzy_match)
                if second_match is not None:
                    errors.append(f'Replacement {i+1}: old_text found at multiple locations (line {match_line} and later) — ambiguous')
                
                content = content[:start_pos] + new_text + content[start_pos + len(actual_text):]
                total_replacements += 1

            if errors:
                return f'Error: MultiEdit failed — {"; ".join(errors)}\nNo changes were made (atomic rollback).'
            if total_replacements == 0:
                return 'Error: No valid replacements provided.'

            # Atomic write (same pattern as _tool_write_file)
            _dir = os.path.dirname(path) or '.'
            fd, tmp_path = tempfile.mkstemp(dir=_dir, suffix='.tmp', prefix='.muside_edit_')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    f.write(content)
                os.replace(tmp_path, path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
                raise
            # Auto-verify: check that new_text exists in written file
            verify_errors = []
            for i, rep in enumerate(replacements):
                new_text = rep.get('new_text', '')
                if new_text and new_text not in content:
                    verify_errors.append(f'Replacement {i+1}: new_text not found after edit (possible whitespace issue)')
            if verify_errors:
                # Rollback to original (atomic)
                fd2, tmp_path2 = tempfile.mkstemp(dir=_dir, suffix='.tmp', prefix='.muside_rollback_')
                try:
                    with os.fdopen(fd2, 'w', encoding='utf-8') as f:
                        f.write(original_content)
                    os.replace(tmp_path2, path)
                except Exception:
                    try:
                        os.unlink(tmp_path2)
                    except Exception:
                        pass
                return f'Error: Edit verification failed — {"; ".join(verify_errors)}. File rolled back to original. Check whitespace/indentation in your new_text.'
            # Auto-update AST index
            ext = os.path.splitext(path)[1].lower()
            if ext in ('.py', '.js', '.ts', '.tsx', '.jsx', '.go', '.mjs', '.cjs'):
                try:
                    project_index.index_file(path, content.encode('utf-8'))
                except Exception:
                    pass
            
            # Auto-lint check
            lint_result = ''
            if ext in ('.py', '.js', '.ts', '.tsx', '.jsx', '.go', '.mjs', '.cjs'):
                lint_result = _auto_lint_check(path)
            
            base_msg = f'MultiEdit applied to {path}: {total_replacements} replacement(s) made'
            if lint_result:
                return base_msg + '\n\n' + lint_result
            return base_msg

        # Legacy single-replacement mode
        old_text = args.get('old_text', '')
        new_text = args.get('new_text', '')
        if not old_text:
            return 'Error: old_text is required (or use "replacements" array)'

        match_result = _find_text_in_content(content, old_text, 
                                              line_hint=line_hint, 
                                              fuzzy_match=fuzzy_match)
        if match_result is None:
            # Provide context around line_hint to help AI fix the match
            hint_line = line_hint or 1
            context = _get_context_around_line(content, hint_line)
            return (f'Error: old_text not found in file. '
                    f'Make sure the text matches exactly (including whitespace). '
                    f'Try fuzzy_match=true if whitespace differs.\n'
                    f'Context around line {hint_line}:\n{context}')
        
        actual_text, start_pos, match_line = match_result
        
        # Check for ambiguous matches in the remaining content
        remaining = content[start_pos + len(actual_text):]
        second_match = _find_text_in_content(remaining, old_text, fuzzy_match=fuzzy_match)
        if second_match is not None:
            return (f'Error: old_text found at multiple locations (line {match_line} and later) — ambiguous match. '
                    f'Provide more surrounding context to uniquely identify the target, use line_hint to narrow scope, '
                    f'or use the "replacements" array parameter for multiple specific edits.')
        
        new_content = content[:start_pos] + new_text + content[start_pos + len(actual_text):]
        # Atomic write
        _dir = os.path.dirname(path) or '.'
        fd, tmp_path = tempfile.mkstemp(dir=_dir, suffix='.tmp', prefix='.muside_edit_')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(new_content)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise
        # Auto-update AST index
        ext = os.path.splitext(path)[1].lower()
        if ext in ('.py', '.js', '.ts', '.tsx', '.jsx', '.go', '.mjs', '.cjs'):
            try:
                project_index.index_file(path, new_content.encode('utf-8'))
            except Exception:
                pass
        
        # Auto-lint check
        lint_result = ''
        if ext in ('.py', '.js', '.ts', '.tsx', '.jsx', '.go', '.mjs', '.cjs'):
            lint_result = _auto_lint_check(path)
        
        base_msg = f'Edited file: {path} (1 replacement made at line {match_line})'
        if lint_result:
            return base_msg + '\n\n' + lint_result
        return base_msg
    except Exception as e:
        return f'Error editing file {path}: {e}'

def _tool_list_directory(args):
    path = _validate_path(args.get('path', '.') if args.get('path', '.') != '.' else _get_project_dir())
    show_hidden = args.get('show_hidden', False)
    verbose = args.get('verbose', False)
    if not os.path.isdir(path):
        return f'Error: Directory not found: {path}'
    items = []
    # Common directories to hide from AI (virtual envs, cache, IDE configs)
    _ai_hidden_dirs = {'.venv', 'venv', 'env', '__pycache__', 'node_modules', '.git', '.idea', '.vscode'}
    for entry in sorted(os.listdir(path)):
        if not show_hidden and entry.startswith('.'):
            continue
        # Also hide non-dot venv dirs that would confuse the AI
        if not show_hidden and entry in _ai_hidden_dirs:
            continue
        full = os.path.join(path, entry)
        try:
            st = os.stat(full)
            is_dir = os.path.isdir(full)
            if verbose:
                ftype = 'dir' if is_dir else get_file_type(entry)
                perm = oct(st.st_mode)[-3:]
                mod_time = datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
                sz = st.st_size
                items.append(f'  {"[DIR]" if is_dir else "[FILE]"} {perm} {mod_time} {sz:>10}  {entry}  ({ftype})')
            else:
                sz = st.st_size
                sz_str = f'{sz}' if sz < 1024 else f'{sz/1024:.0f}K' if sz < 1048576 else f'{sz/1048576:.1f}M'
                items.append(f'  {"[DIR]" if is_dir else "[FILE]"} {sz_str:>8}  {entry}')
        except (PermissionError, OSError):
            items.append(f'  [??]  {entry}  (permission denied)')
    header = f'Directory: {path} ({len(items)} entries)'
    return header + '\n' + '\n'.join(items) if items else header + '\n  (empty directory)'

def _tool_search_files(args):
    pattern = args['pattern']
    search_path = _validate_path(args.get('path', '.') if args.get('path', '.') != '.' else _get_project_dir())
    include = args.get('include', None)
    max_results = args.get('max_results', 50)
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return f'Error: Invalid regex pattern: {e}'
    results = []
    skip_dirs = {'.git', '__pycache__', 'node_modules', '.venv', 'venv', 'env', '.idea', '.vscode', '.svn', 'bower_components', '.next', 'dist', 'build'}
    search_start = time.time()
    SEARCH_TIMEOUT = 30  # seconds
    for root, dirs, files in os.walk(search_path):
        if time.time() - search_start > SEARCH_TIMEOUT:
            results.append(f'[Search timed out after {SEARCH_TIMEOUT}s, showing {len(results)} of potentially more results]')
            break
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith('.')]
        for fname in files:
            if len(results) >= max_results:
                break
            if include and not fnmatch.fnmatch(fname, include):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, 'r', encoding='utf-8', errors='ignore') as f:
                    for i, line in enumerate(f, 1):
                        if regex.search(line):
                            rel = os.path.relpath(fpath, search_path)
                            results.append(f'{rel}:{i}: {line.rstrip()[:300]}')
                            if len(results) >= max_results:
                                break
            except (PermissionError, OSError):
                continue
        if len(results) >= max_results:
            break
    if not results:
        return f'No matches found for pattern "{pattern}"'
    header = f'Search results for "{pattern}" ({len(results)} matches):'
    return header + '\n' + '\n'.join(results)

def _get_effective_cwd():
    """Get the effective working directory for tool execution.
    When a project is open, returns the project directory.
    Otherwise returns WORKSPACE."""
    try:
        config = load_config()
        ws = config.get('workspace', WORKSPACE)
        # Auto-create workspace if it doesn't exist
        if not os.path.isdir(ws):
            os.makedirs(ws, exist_ok=True)
        project = config.get('project', None)
        if project:
            project_dir = os.path.join(ws, project)
            if os.path.isdir(project_dir):
                return project_dir
        return ws
    except Exception:
        return WORKSPACE


def _tool_run_command(args):
    from utils import IS_WINDOWS
    command = args['command']
    timeout = args.get('timeout', 120)
    cwd = args.get('cwd', None) or _get_effective_cwd()

    # ── SMART SUGGESTION: Point out better tools when appropriate ──
    # Instead of hard-blocking, we add a suggestion prefix to the output when
    # the command could have been done more efficiently with a specialized tool.
    # The command still executes, but the LLM learns to use better tools next time.
    _suggestion = None
    cmd_stripped = command.strip()
    first_word = cmd_stripped.split()[0].lower() if cmd_stripped.split() else ''

    # Only suggest for simple (non-compound) commands
    if not any(sep in cmd_stripped for sep in ['&&', '||', ';', '|']):
        _BETTER_TOOL = {
            'cat': ('read_file', 'returns structured output with line numbers'),
            'head': ('read_file', 'supports offset_line to read specific sections'),
            'tail': ('read_file', 'supports offset_line/limit_lines to read end of file'),
            'less': ('read_file', 'returns full content with line numbers'),
            'more': ('read_file', 'returns full content with line numbers'),
            'bat': ('read_file', 'returns content with line numbers'),
            'ls': ('list_directory', 'returns structured file listing with sizes'),
            'll': ('list_directory', 'returns structured file listing with sizes'),
            'dir': ('list_directory', 'returns structured file listing with sizes'),
            'tree': ('file_structure', 'returns AST-based structure outline'),
            'find': ('glob_files', 'supports pattern matching like "**/*.py"'),
            'grep': ('grep_code', 'returns matches with context lines and file info'),
            'rg': ('grep_code', 'returns matches with context lines and file info'),
            'wc': ('file_info', 'returns file size and metadata'),
            'stat': ('file_info', 'returns file metadata directly'),
            'du': ('file_info', 'returns file/directory size info'),
            'git': ('git_status/git_diff/etc', 'provides structured parsed output'),
            'pip': ('install_package/list_packages', 'handles venv automatically'),
            'npm': ('install_package/list_packages', 'handles venv automatically'),
            'ruff': ('run_linter', 'auto-detects linter and project type'),
            'flake8': ('run_linter', 'auto-detects linter and project type'),
            'pylint': ('run_linter', 'auto-detects linter and project type'),
            'eslint': ('run_linter', 'auto-detects linter and project type'),
            'pytest': ('run_tests', 'auto-detects test framework'),
            'jest': ('run_tests', 'auto-detects test framework'),
        }
        if first_word in _BETTER_TOOL:
            better_tool, why = _BETTER_TOOL[first_word]
            _suggestion = f'💡 Tip: `{better_tool}` is more efficient here — {why}.\n\n'

    # SAFETY: Block commands that would kill the IDE server
    ide_port = os.environ.get('MUSIDE_PORT', '12345')
    cmd_lower = command.lower()
    # Check for dangerous combinations: a kill command + IDE port or server name
    has_kill = any(p in cmd_lower for p in ['kill', 'pkill', 'killall', 'taskkill', 'fuser -k'])
    has_ide_target = (ide_port in command or 'muside_server' in cmd_lower)
    if has_kill and has_ide_target:
        return f'⛔ BLOCKED: This command would stop the MusIDE server (port {ide_port}, muside_server.py). Killing the IDE process is not allowed — it would shut down the IDE and AI assistant.'

    try:
        cwd = _validate_path(cwd)
    except ValueError:
        cwd = _get_effective_cwd()
    config = load_config()
    env = os.environ.copy()
    venv_path = config.get('venv_path', '')
    if venv_path and os.path.exists(venv_path):
        _bin_dir = 'Scripts' if IS_WINDOWS else 'bin'
        venv_bin = os.path.join(venv_path, _bin_dir)
        if os.path.exists(venv_bin):
            _path_sep = ';' if IS_WINDOWS else ':'
            env['PATH'] = venv_bin + _path_sep + env.get('PATH', '')
            env['VIRTUAL_ENV'] = venv_path
    try:
        result = subprocess.run(
            command, shell=True, cwd=cwd, capture_output=True, text=True,
            timeout=timeout, env=env,
        )
        output = ''
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += ('\n' if output else '') + result.stderr
        exit_info = f'\n[Exit code: {result.returncode}]'
        full_output = (output or '(no output)') + exit_info
        # Return error status when command exits with non-zero code
        if result.returncode != 0:
            raise RuntimeError(full_output)
        return (_suggestion or '') + _truncate(full_output)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'Command timed out after {timeout} seconds')
    except RuntimeError:
        raise  # Re-raise RuntimeError (non-zero exit code) without wrapping
    except Exception as e:
        raise RuntimeError(f'Error executing command: {str(e)}')

def _tool_git_status(args):
    repo_path = args.get('repo_path', None) or _get_effective_cwd()
    r = git_cmd('status --porcelain -b', cwd=repo_path)
    if not r['ok']:
        return f'Error: {r["stderr"]}'
    return r['stdout'] or 'Clean working tree (no changes)'

def _tool_git_diff(args):
    repo_path = args.get('repo_path', None) or _get_effective_cwd()
    staged = args.get('staged', False)
    file_path = args.get('file_path', '')
    cmd = 'diff --cached' if staged else 'diff'
    if file_path:
        cmd += f' -- {shlex_quote(file_path)}'
    r = git_cmd(cmd, cwd=repo_path)
    return r['stdout'] or 'No changes to display'

def _tool_git_commit(args):
    message = args['message']
    repo_path = args.get('repo_path', None) or _get_effective_cwd()
    add_all = args.get('add_all', True)
    if add_all:
        git_cmd('add -A', cwd=repo_path)
    r = git_cmd(f'commit -m {shlex_quote(message)}', cwd=repo_path)
    if r['ok']:
        return f'Commit successful: "{message}"'
    return f'Error: {r["stderr"]}'

def _tool_install_package(args):
    from utils import IS_WINDOWS, get_default_compiler
    package_name = args['package_name']
    manager = args.get('manager', 'auto')
    config = load_config()
    if manager == 'auto':
        # Explicit parentheses: npm if it looks like an npm package AND package.json exists
        manager = 'npm' if (
            package_name.startswith('@') or
            (not re.search(r'[a-zA-Z]-[a-zA-Z]', package_name) and
             os.path.exists(os.path.join(WORKSPACE, 'package.json')))
        ) else 'pip'
    if manager == 'npm':
        cmd = f'npm install {shlex_quote(package_name)}'
    else:
        venv = config.get('venv_path', '')
        if IS_WINDOWS:
            pip = os.path.join(venv, 'Scripts', 'pip.exe') if venv and os.path.exists(os.path.join(venv, 'Scripts', 'pip.exe')) else get_default_compiler() + ' -m pip'
        else:
            pip = os.path.join(venv, 'bin', 'pip') if venv and os.path.exists(os.path.join(venv, 'bin', 'pip')) else get_default_compiler() + ' -m pip'
        cmd = f'{pip} install {shlex_quote(package_name)}'
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=100, cwd=WORKSPACE)
    output = r.stdout or ''
    if r.stderr:
        output += ('\n' if output else '') + r.stderr
    if r.returncode == 0:
        return _truncate(f'Package installed successfully: {package_name}\n{output}')
    return _truncate(f'Error installing {package_name} (exit code {r.returncode}):\n{output}')

def _tool_list_packages(args):
    from utils import IS_WINDOWS
    manager = args.get('manager', 'pip')
    config = load_config()
    if manager == 'npm':
        r = subprocess.run('npm list --depth=0 2>/dev/null', shell=True, capture_output=True, text=True, timeout=30, cwd=WORKSPACE)
        return r.stdout or 'No packages found'
    venv = config.get('venv_path', '')
    if IS_WINDOWS:
        pip = os.path.join(venv, 'Scripts', 'pip.exe') if venv and os.path.exists(os.path.join(venv, 'Scripts', 'pip.exe')) else 'pip'
    else:
        pip = os.path.join(venv, 'bin', 'pip') if venv and os.path.exists(os.path.join(venv, 'bin', 'pip')) else 'pip3'
    r = subprocess.run(f'{pip} list --format=json', shell=True, capture_output=True, text=True, timeout=30)
    if r.returncode == 0:
        try:
            pkgs = json.loads(r.stdout)
            lines = [f'  {p["name"]}=={p["version"]}' for p in pkgs]
            return f'Installed packages ({len(lines)}):\n' + '\n'.join(lines)
        except Exception:
            return r.stdout or r.stderr or 'No packages found'
    return r.stdout or r.stderr or 'No packages found'

def _tool_grep_code(args):
    pattern = args['pattern']
    search_path = _validate_path(args.get('path', '.') if args.get('path', '.') != '.' else _get_project_dir())
    context = args.get('context_lines', 2)
    include = args.get('include', None)
    exclude = args.get('exclude', None)
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return f'Error: Invalid regex: {e}'
    results = []
    skip_dirs = {'.git', '__pycache__', 'node_modules', '.venv', 'venv', 'env', '.idea', '.vscode'}
    _grep_start = time.time()
    GREP_TIMEOUT = 30
    for root, dirs, files in os.walk(search_path):
        if time.time() - _grep_start > GREP_TIMEOUT:
            results.append(f'[Search timed out after {GREP_TIMEOUT}s]')
            break
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in files:
            if include and not fnmatch.fnmatch(fname, include):
                continue
            if exclude and fnmatch.fnmatch(fname, exclude):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, 'r', encoding='utf-8', errors='ignore') as f:
                    all_lines = f.readlines()
                matches = []
                for i, line in enumerate(all_lines):
                    if regex.search(line):
                        matches.append(i)
                if not matches:
                    continue
                rel = os.path.relpath(fpath, search_path)
                for idx in matches:
                    start = max(0, idx - context)
                    end = min(len(all_lines), idx + context + 1)
                    results.append(f'\n{rel}:{idx+1}:\n' + ''.join(
                        f'  {"*" if j == idx else " "} {j+1:>5}\t{all_lines[j].rstrip()}\n'
                        for j in range(start, end)
                    ))
                if len(results) >= 30:
                    break
            except (PermissionError, OSError):
                continue
        if len(results) >= 30:
            break
    if not results:
        return f'No matches for pattern "{pattern}"'
    return f'Found {len(results)} match(es) for "{pattern}":\n' + '\n'.join(results)

def _tool_file_info(args):
    path = _validate_path(args['path'])
    if not os.path.exists(path):
        return f'Error: Path not found: {path}'
    st = os.stat(path)
    is_dir = os.path.isdir(path)
    is_link = os.path.islink(path)
    ftype = 'symlink' if is_link else ('directory' if is_dir else 'regular file')
    size = st.st_size
    if is_dir:
        try:
            size = sum(
                os.path.getsize(os.path.join(dp, f))
                for dp, dn, fn in os.walk(path)
                for f in fn
            )
        except (PermissionError, OSError):
            size = 0
    mod_time = datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
    perm_oct = oct(st.st_mode)[-3:]
    perm_rwx = ''
    for p in perm_oct:
        perm_rwx += {'0': '---', '1': '--x', '2': '-w-', '3': '-wx', '4': 'r--', '5': 'r-x', '6': 'rw-', '7': 'rwx'}[p] + ' '
    return (
        f'Path:     {path}\n'
        f'Type:     {ftype}\n'
        f'Size:     {size:,} bytes\n'
        f'Modified: {mod_time}\n'
        f'Permissions: {perm_oct} ({perm_rwx.strip()})'
    )

def _tool_create_directory(args):
    path = _validate_path(args['path'])
    try:
        os.makedirs(path, exist_ok=True)
        return f'Directory created: {path}'
    except PermissionError:
        return f'Error: Permission denied creating directory: {path}'
    except OSError as e:
        return f'Error creating directory {path}: {e}'

def _tool_web_search(args):
    query = args.get('query', '')
    try:
        url = 'https://html.duckduckgo.com/html/?q=' + urllib.parse.quote_plus(query)
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; MusIDE Bot)'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html_content = resp.read().decode('utf-8', errors='ignore')
        results = []
        for match in re.finditer(r'<a rel="nofollow" class="result__a" href="([^"]+)">([^<]+)</a>.*?<a class="result__snippet"[^>]*>([^<]*(?:<[^a][^<]*)*)</a>', html_content, re.DOTALL):
            link = match.group(1)
            title = re.sub(r'<[^>]+>', '', match.group(2)).strip()
            snippet = re.sub(r'<[^>]+>', '', match.group(3)).strip()
            if link.startswith('//'):
                link = 'https:' + link
            results.append({'title': title, 'url': link, 'snippet': snippet})
            if len(results) >= 10:
                break
        if not results:
            return f'No results found for "{query}"'
        lines = []
        for i, r in enumerate(results, 1):
            lines.append(f'{i}. {r["title"]}')
            lines.append(f'   URL: {r["url"]}')
            lines.append(f'   {r["snippet"]}')
            lines.append('')
        return f'Search results for "{query}" ({len(results)} results):\n' + '\n'.join(lines)
    except Exception as e:
        return f'Error searching: {str(e)}'

def _tool_web_fetch(args):
    url = args.get('url', '')
    if not url:
        return 'Error: URL is required'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; MusIDE Bot)'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html_content = resp.read().decode('utf-8', errors='ignore')
        # Strip HTML tags
        text = re.sub(r'<script[^>]*>[\s\S]*?</script>', '', html_content, flags=re.IGNORECASE)
        text = re.sub(r'<style[^>]*>[\s\S]*?</style>', '', text, flags=re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'&nbsp;', ' ', text)
        text = re.sub(r'&amp;', '&', text)
        text = re.sub(r'&lt;', '<', text)
        text = re.sub(r'&gt;', '>', text)
        text = re.sub(r'&quot;', '"', text)
        text = re.sub(r'&#39;', "'", text)
        text = re.sub(r'\s+', ' ', text).strip()
        if len(text) > 10000:
            text = text[:10000] + '\n\n[truncated: content exceeds 10000 character limit]'
        if not text:
            return 'No text content found at the URL'
        return f'Content from {url}:\n{text}'
    except Exception as e:
        return f'Error fetching URL: {str(e)}'

def _tool_git_log(args):
    count = args.get('count', 10)
    repo_path = args.get('repo_path', None) or _get_effective_cwd()
    r = git_cmd(f'log --oneline -n {count}', cwd=repo_path)
    if not r['ok']:
        return f'Error: {r["stderr"]}'
    return r['stdout'] or 'No commits found'

def _tool_git_checkout(args):
    branch = args.get('branch', '')
    repo_path = args.get('repo_path', None) or _get_effective_cwd()
    if not branch:
        return 'Error: branch name is required'
    r = git_cmd(f'checkout {shlex_quote(branch)}', cwd=repo_path)
    if r['ok']:
        return f'Switched to branch "{branch}"'
    return f'Error: {r["stderr"]}'

def _tool_delete_path(args):
    path = _validate_path(args['path'])
    real_ws = os.path.realpath(WORKSPACE)
    if os.path.realpath(path) == real_ws:
        return 'Error: Cannot delete the workspace root'
    if not os.path.exists(path):
        return f'Error: Path not found: {path}'
    recursive = args.get('recursive', False)
    try:
        if os.path.isdir(path):
            if recursive:
                shutil.rmtree(path)
                return f'Directory deleted recursively: {path}'
            else:
                try:
                    os.rmdir(path)
                    return f'Directory deleted (must be empty): {path}'
                except OSError as e:
                    return f'Error: Directory not empty. Use recursive=true to delete: {e}'
        else:
            os.remove(path)
            return f'File deleted: {path}'
    except Exception as e:
        return f'Error deleting path: {str(e)}'

def _tool_move_file(args):
    """Move or rename a file/directory."""
    src = _validate_path(args['source'])
    dst = _validate_path(args['destination'])
    if not os.path.exists(src):
        return f'Error: Source not found: {src}'
    try:
        # Create destination parent directory if needed
        dst_parent = os.path.dirname(dst)
        if dst_parent:
            os.makedirs(dst_parent, exist_ok=True)
        shutil.move(src, dst)
        # Update AST index: remove old, add new if it's a source file
        ext_src = os.path.splitext(src)[1].lower()
        ext_dst = os.path.splitext(dst)[1].lower()
        if ext_src in ('.py', '.js', '.ts', '.tsx', '.jsx', '.go', '.mjs', '.cjs'):
            try:
                project_index.remove_file(src)
            except Exception:
                pass
        if ext_dst in ('.py', '.js', '.ts', '.tsx', '.jsx', '.go', '.mjs', '.cjs'):
            try:
                if os.path.isfile(dst):
                    with open(dst, 'rb') as f:
                        project_index.index_file(dst, f.read())
            except Exception:
                pass
        src_type = 'directory' if os.path.isdir(dst) else 'file'
        return f'Moved {src_type}: {src} -> {dst}'
    except Exception as e:
        return f'Error moving {src} to {dst}: {e}'

def _tool_append_file(args):
    """Append content to an existing file."""
    path = _validate_path(args['path'])
    content = args['content']
    if not os.path.isfile(path):
        return f'Error: File not found: {path}. Use write_file to create new files.'
    try:
        with open(path, 'a', encoding='utf-8') as f:
            if not content.endswith('\n'):
                content += '\n'
            f.write(content)
        size = os.path.getsize(path)
        return f'Appended to file: {path} (now {size} bytes)'
    except Exception as e:
        return f'Error appending to {path}: {e}'

# ==================== Browser Debugging Tools ====================

def _format_browser_result(result):
    """Format a browser command result dict into a readable string."""
    if not isinstance(result, dict):
        return str(result) if result else '(no result)'
    if result.get('error'):
        # Downgrade timeout errors to warnings so the model keeps trying
        if 'timed out' in result['error']:
            return f"Warning: {result['error']} The preview panel may not be active. Try browser_page_info to check."
        return f"Error: {result['error']}"
    # Remove 'ok' key for cleaner output
    info = {k: v for k, v in result.items() if k != 'ok'}
    # Add truncation notice if present
    if result.get('truncated'):
        info['_note'] = f"Output truncated. Full length: {result.get('fullLength', '?')} chars. Use more specific selectors or expressions to see more."
    try:
        return json.dumps(info, indent=2, ensure_ascii=False)
    except Exception:
        return str(info)

def _tool_browser_navigate(args):
    url = args.get('url', '')
    if not url:
        return 'Error: URL is required'
    if not url.startswith('http://') and not url.startswith('https://'):
        url = 'http://' + url
    cmd_id = create_browser_command('navigate', {'url': url})
    result = wait_browser_result(cmd_id, timeout=30)
    # If timed out, it likely means the preview tab is not active — return a helpful message instead of error
    if isinstance(result, dict) and result.get('error') and 'timed out' in result.get('error', ''):
        return f'Warning: Preview panel may not be active. The page may still be navigating to: {url}\nUse browser_page_info to verify the page loaded.'
    return _format_browser_result(result)

def _tool_browser_console(args):
    cmd_id = create_browser_command('console', {})
    result = wait_browser_result(cmd_id, timeout=30)
    if not isinstance(result, dict):
        return str(result)
    if result.get('ok'):
        logs = result.get('logs', [])
        if not logs:
            return '(no console output captured yet - ensure the Bridge is injected)'
        lines = []
        for log in logs:
            lines.append(f"  [{log.get('type','log')}] {log.get('time','')}  {log.get('text','')}")
        return f"Console output ({result.get('count', len(logs))} entries):\n" + '\n'.join(lines[-100:])
    return _format_browser_result(result)

def _tool_browser_page_info(args):
    cmd_id = create_browser_command('page_info', {})
    result = wait_browser_result(cmd_id, timeout=30)
    return _format_browser_result(result)

def _tool_browser_evaluate(args):
    expression = args.get('expression', '')
    if not expression:
        return 'Error: expression is required'
    cmd_id = create_browser_command('evaluate', {'expression': expression})
    result = wait_browser_result(cmd_id, timeout=15)
    return _format_browser_result(result)

def _tool_browser_inspect(args):
    selector = args.get('selector', 'body')
    cmd_id = create_browser_command('inspect', {'selector': selector})
    result = wait_browser_result(cmd_id, timeout=15)
    return _format_browser_result(result)

def _tool_browser_query_all(args):
    selector = args.get('selector', '*')
    cmd_id = create_browser_command('query_all', {'selector': selector})
    result = wait_browser_result(cmd_id, timeout=15)
    return _format_browser_result(result)

def _tool_browser_click(args):
    selector = args.get('selector', '')
    if not selector:
        return 'Error: selector is required'
    cmd_id = create_browser_command('click', {'selector': selector})
    result = wait_browser_result(cmd_id, timeout=15)
    return _format_browser_result(result)

def _tool_browser_input(args):
    selector = args.get('selector', '')
    text = args.get('text', '')
    if not selector:
        return 'Error: selector is required'
    cmd_id = create_browser_command('input', {'selector': selector, 'text': text})
    result = wait_browser_result(cmd_id, timeout=15)
    return _format_browser_result(result)

def _tool_browser_cookies(args):
    cmd_id = create_browser_command('cookies', {})
    result = wait_browser_result(cmd_id, timeout=10)
    if not isinstance(result, dict):
        return str(result)
    if result.get('ok'):
        cookies = result.get('cookies', [])
        raw = result.get('raw', '')
        if isinstance(cookies, list) and cookies:
            lines = [f"  {c.get('name','')}: {c.get('value','')}" for c in cookies]
            return f"Cookies ({len(cookies)} total):\n" + '\n'.join(lines)
        elif isinstance(cookies, str):
            return cookies
        return '(no cookies)'
    return _format_browser_result(result)

def _tool_server_logs(args):
    count = args.get('count', 50)
    try:
        import urllib.request as _urllib_req
        port = os.environ.get('PORT', '1239')
        req_data = json.dumps({'count': count}).encode()
        req = _urllib_req.Request(
            f'http://localhost:{port}/api/server/logs',
            data=req_data,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with _urllib_req.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        lines = data.get('lines', [])
        source = data.get('source', 'unknown')
        total = data.get('total', 0)
        if not lines:
            return f'No server logs found (source: {source}, total in file: {total})'
        # Highlight error lines
        result_lines = []
        for line in lines:
            if 'ERROR' in line or 'Traceback' in line or 'Exception' in line:
                result_lines.append(f'  >> {line}')
            elif 'WARNING' in line or 'WARN' in line:
                result_lines.append(f'  !> {line}')
            else:
                result_lines.append(f'     {line}')
        header = f'Server logs (last {len(lines)} of {total} lines, source: {source}):'
        error_count = sum(1 for l in lines if 'ERROR' in l or 'Traceback' in l or 'Exception' in l)
        if error_count:
            header += f' [! {error_count} error(s) found]'
        return header + '\n' + '\n'.join(result_lines)
    except Exception as e:
        return f'Error reading server logs: {e}'

# ── P0+P1 New Tool Implementations ──

def _tool_glob_files(args):
    """Fast file pattern matching using glob."""
    pattern = args['pattern']
    search_path = _validate_path(args.get('path', '.') if args.get('path', '.') != '.' else _get_project_dir())
    if not os.path.isdir(search_path):
        return f'Error: Directory not found: {search_path}'

    # Use pathlib-style recursive matching
    if '**' in pattern:
        full_pattern = os.path.join(search_path, pattern)
        matches = _glob.glob(full_pattern, recursive=True)
    else:
        full_pattern = os.path.join(search_path, pattern)
        matches = _glob.glob(full_pattern)

    # Filter to files only, resolve paths, deduplicate
    # Also exclude virtual env directories to avoid confusing results
    _skip_dirs = {os.sep + '.venv' + os.sep, os.sep + 'venv' + os.sep, os.sep + 'env' + os.sep,
                  os.sep + '__pycache__' + os.sep, os.sep + 'node_modules' + os.sep,
                  os.sep + '.git' + os.sep, os.sep + 'site-packages' + os.sep}
    seen = set()
    files = []
    for f in matches:
        if os.path.isfile(f):
            # Skip files inside virtual env / cache directories
            if any(sd in f for sd in _skip_dirs):
                continue
            real = os.path.realpath(f)
            if real not in seen:
                seen.add(real)
                files.append(f)

    # Sort by modification time (newest first)
    files.sort(key=lambda f: os.path.getmtime(f), reverse=True)

    # Limit results
    max_results = 100
    if len(files) > max_results:
        files = files[:max_results]

    if not files:
        return f'No files matching pattern "{pattern}" in {search_path}'

    lines = [f'Found {len(files)} file(s) matching "{pattern}":']
    for f in files:
        rel = os.path.relpath(f, search_path)
        size = os.path.getsize(f)
        mtime = datetime.fromtimestamp(os.path.getmtime(f)).strftime('%Y-%m-%d %H:%M')
        lines.append(f'  {rel}  ({size:,} bytes, {mtime})')

    if len(files) == max_results:
        lines.append(f'  [showing first {max_results} results, sorted by modification time]')
    return '\n'.join(lines)

# ==================== Quality Assurance Tools ====================

# Auto-lint configuration: can be toggled via .muside/rules.md or config
_AUTO_LINT_ENABLED = True  # Default: enabled; set False to disable auto-lint after edit/write
_AUTO_LINT_TIMEOUT = 15    # seconds — max time for auto-lint check

def _auto_lint_check(filepath):
    """Run a quick lint check on a single file after write/edit. 
    Returns a string with lint results, or empty string if no issues/skipped.
    Can be disabled by setting _AUTO_LINT_ENABLED = False or adding 
    'auto_lint: false' to .muside/rules.md.
    """
    if not _AUTO_LINT_ENABLED:
        return ''
    
    # Check if auto-lint is disabled in project config
    try:
        rules_path = os.path.join(WORKSPACE, '.muside', 'rules.md')
        if os.path.isfile(rules_path):
            with open(rules_path, 'r', encoding='utf-8') as f:
                rules = f.read().lower()
            if 'auto_lint: false' in rules or 'auto_lint:false' in rules or 'auto-lint: false' in rules:
                return ''
    except Exception:
        pass
    
    ext = os.path.splitext(filepath)[1].lower()
    from utils import IS_WINDOWS
    
    # Build venv-aware environment
    env = os.environ.copy()
    try:
        config = load_config()
        venv_path = config.get('venv_path', '')
        if venv_path and os.path.exists(venv_path):
            _bin_dir = 'Scripts' if IS_WINDOWS else 'bin'
            venv_bin = os.path.join(venv_path, _bin_dir)
            if os.path.exists(venv_bin):
                _path_sep = ';' if IS_WINDOWS else ':'
                env['PATH'] = venv_bin + _path_sep + env.get('PATH', '')
                env['VIRTUAL_ENV'] = venv_path
    except Exception:
        pass
    
    cmd = None
    cwd = os.path.dirname(filepath) or '.'
    
    if ext == '.py':
        # Try ruff first (fastest), then flake8
        try:
            result = subprocess.run(
                f'ruff check --output-format=concise {shlex_quote(filepath)}',
                shell=True, cwd=cwd, capture_output=True, text=True, 
                timeout=_AUTO_LINT_TIMEOUT, env=env
            )
            output = (result.stdout or '').strip()
            if output:
                errors = [l for l in output.split('\n') if l.strip() and (':E' in l or ':F' in l)]
                warnings = [l for l in output.split('\n') if l.strip() and ':E' not in l and ':F' not in l]
                msg = '[Auto-lint: ruff]'
                if errors:
                    msg += f' {len(errors)} error(s)'
                if warnings:
                    msg += f' {len(warnings)} warning(s)'
                msg += '\n' + output[:800]
                return msg
            return ''  # No issues
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        
        try:
            result = subprocess.run(
                f'flake8 {shlex_quote(filepath)}',
                shell=True, cwd=cwd, capture_output=True, text=True,
                timeout=_AUTO_LINT_TIMEOUT, env=env
            )
            output = (result.stdout or '').strip()
            if output:
                errors = [l for l in output.split('\n') if l.strip() and (':E' in l or ':F' in l)]
                warnings = [l for l in output.split('\n') if l.strip() and ':E' not in l and ':F' not in l]
                msg = '[Auto-lint: flake8]'
                if errors:
                    msg += f' {len(errors)} error(s)'
                if warnings:
                    msg += f' {len(warnings)} warning(s)'
                msg += '\n' + output[:800]
                return msg
            return ''
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    
    elif ext in ('.js', '.ts', '.tsx', '.jsx', '.mjs', '.cjs'):
        # Try eslint if configured
        has_eslint = (
            os.path.isfile(os.path.join(cwd, '.eslintrc.js')) or
            os.path.isfile(os.path.join(cwd, '.eslintrc.json')) or
            os.path.isfile(os.path.join(cwd, '.eslintrc.yml')) or
            os.path.isfile(os.path.join(cwd, '.eslintrc')) or
            os.path.isfile(os.path.join(cwd, 'eslint.config.js'))
        )
        if not has_eslint:
            return ''
        
        try:
            result = subprocess.run(
                f'npx eslint --format compact {shlex_quote(filepath)}',
                shell=True, cwd=cwd, capture_output=True, text=True,
                timeout=_AUTO_LINT_TIMEOUT, env=env
            )
            output = (result.stdout or '').strip()
            if output:
                errors = [l for l in output.split('\n') if l.strip() and ' error' in l.lower()]
                warnings = [l for l in output.split('\n') if l.strip() and ' error' not in l.lower()]
                msg = '[Auto-lint: eslint]'
                if errors:
                    msg += f' {len(errors)} error(s)'
                if warnings:
                    msg += f' {len(warnings)} warning(s)'
                msg += '\n' + output[:800]
                return msg
            return ''
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    
    elif ext == '.go':
        try:
            result = subprocess.run(
                f'go vet {shlex_quote(filepath)}',
                shell=True, cwd=cwd, capture_output=True, text=True,
                timeout=_AUTO_LINT_TIMEOUT, env=env
            )
            output = (result.stdout or result.stderr or '').strip()
            if output:
                return f'[Auto-lint: go vet]\n' + output[:800]
            return ''
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    
    return ''  # No linter available or no issues

def _detect_project_type(project_dir):
    """Auto-detect project type by checking for config files."""
    indicators = {
        'python': ['setup.py', 'pyproject.toml', 'requirements.txt', 'Pipfile', 'setup.cfg'],
        'javascript': ['package.json'],
        'typescript': ['tsconfig.json'],
        'go': ['go.mod', 'go.sum'],
    }
    detected = []
    for proj_type, files in indicators.items():
        for f in files:
            if os.path.isfile(os.path.join(project_dir, f)):
                detected.append(proj_type)
                break
    # TypeScript implies JavaScript too
    if 'typescript' in detected and 'javascript' not in detected:
        detected.append('javascript')
    return detected if detected else ['unknown']

def _tool_run_linter(args):
    """Run a linter on the project or a specific file. Auto-detects project type and linter."""
    from utils import IS_WINDOWS
    target_path = args.get('path')
    if target_path:
        target_path = _validate_path(target_path)
    else:
        target_path = _get_effective_cwd()
    
    forced_linter = args.get('linter', '').strip().lower()
    min_severity = args.get('severity', 'warning')
    
    # Determine project root and file types
    if os.path.isfile(target_path):
        project_dir = os.path.dirname(target_path)
        # Walk up to find project root
        for parent in _walk_up_dirs(project_dir):
            if any(os.path.isfile(os.path.join(parent, f)) for f in 
                   ['setup.py', 'pyproject.toml', 'package.json', 'go.mod', 'tsconfig.json']):
                project_dir = parent
                break
        target_ext = os.path.splitext(target_path)[1].lower()
        project_types = _detect_project_type(project_dir)
        # If file type doesn't match detected project, add it
        if target_ext in ('.py',) and 'python' not in project_types:
            project_types.append('python')
        elif target_ext in ('.js', '.jsx', '.mjs', '.cjs') and 'javascript' not in project_types:
            project_types.append('javascript')
        elif target_ext in ('.ts', '.tsx') and 'typescript' not in project_types:
            project_types.append('typescript')
        elif target_ext in ('.go',) and 'go' not in project_types:
            project_types.append('go')
    else:
        project_dir = target_path
        project_types = _detect_project_type(project_dir)
        target_path = project_dir
    
    if 'unknown' in project_types and not forced_linter:
        return 'Error: Could not detect project type. No pyproject.toml, package.json, or go.mod found. Use the "linter" parameter to specify one explicitly.'
    
    # Build venv-aware environment
    config = load_config()
    env = os.environ.copy()
    venv_path = config.get('venv_path', '')
    if venv_path and os.path.exists(venv_path):
        _bin_dir = 'Scripts' if IS_WINDOWS else 'bin'
        venv_bin = os.path.join(venv_path, _bin_dir)
        if os.path.exists(venv_bin):
            _path_sep = ';' if IS_WINDOWS else ':'
            env['PATH'] = venv_bin + _path_sep + env.get('PATH', '')
            env['VIRTUAL_ENV'] = venv_path
    
    all_issues = []
    linters_tried = []
    
    for proj_type in project_types:
        if proj_type == 'python' or forced_linter in ('ruff', 'flake8', 'pylint'):
            linters = []
            if forced_linter in ('ruff', 'flake8', 'pylint'):
                linters = [forced_linter]
            else:
                # Try ruff first, then flake8, then pylint
                linters = ['ruff', 'flake8', 'pylint']
            
            for linter in linters:
                linters_tried.append(linter)
                cmd = None
                if linter == 'ruff':
                    cmd = f'ruff check --output-format=concise {shlex_quote(target_path)}'
                elif linter == 'flake8':
                    cmd = f'flake8 --format="%(path)s:%(row)d:%(col)d: %(code)s %(text)s" {shlex_quote(target_path)}'
                elif linter == 'pylint':
                    cmd = f'pylint --output-format=text --disable=C0114,C0115,C0116 {shlex_quote(target_path)}'
                
                if cmd:
                    try:
                        result = subprocess.run(cmd, shell=True, cwd=project_dir, 
                                              capture_output=True, text=True, timeout=60, env=env)
                        output = result.stdout or result.stderr or ''
                        if output.strip():
                            for line in output.strip().split('\n'):
                                line = line.strip()
                                if not line or line.startswith('-'):
                                    continue
                                # Parse severity
                                severity = 'warning'
                                if linter == 'ruff':
                                    if ':E' in line or ':F' in line:
                                        severity = 'error'
                                elif linter == 'flake8':
                                    if ':E' in line or ':F' in line:
                                        severity = 'error'
                                elif linter == 'pylint':
                                    if '[E]' in line or '[F]' in line:
                                        severity = 'error'
                                    elif '[W]' in line:
                                        severity = 'warning'
                                    elif '[C]' in line or '[R]' in line:
                                        severity = 'info'
                                
                                # Filter by minimum severity
                                sev_order = {'error': 0, 'warning': 1, 'info': 2, 'convention': 2}
                                if sev_order.get(severity, 1) <= sev_order.get(min_severity, 1):
                                    all_issues.append({'severity': severity, 'message': line, 'linter': linter})
                        # If linter ran successfully, don't try alternatives
                        break
                    except FileNotFoundError:
                        continue
                    except subprocess.TimeoutExpired:
                        all_issues.append({'severity': 'error', 'message': f'{linter} timed out after 60s', 'linter': linter})
                        break
                    except Exception as e:
                        continue
        
        elif proj_type in ('javascript', 'typescript') or forced_linter == 'eslint':
            linter = 'eslint'
            linters_tried.append(linter)
            # Check if ESLint is configured
            has_eslint = (
                os.path.isfile(os.path.join(project_dir, '.eslintrc.js')) or
                os.path.isfile(os.path.join(project_dir, '.eslintrc.json')) or
                os.path.isfile(os.path.join(project_dir, '.eslintrc.yml')) or
                os.path.isfile(os.path.join(project_dir, '.eslintrc')) or
                os.path.isfile(os.path.join(project_dir, 'eslint.config.js'))
            )
            if not has_eslint and not forced_linter:
                continue
            
            cmd = f'npx eslint --format compact {shlex_quote(target_path)}'
            if os.path.isfile(target_path):
                cmd = f'npx eslint --format compact {shlex_quote(target_path)}'
            else:
                cmd = f'npx eslint --format compact .'
            
            try:
                result = subprocess.run(cmd, shell=True, cwd=project_dir,
                                      capture_output=True, text=True, timeout=60, env=env)
                output = result.stdout or result.stderr or ''
                if output.strip():
                    for line in output.strip().split('\n'):
                        line = line.strip()
                        if not line:
                            continue
                        severity = 'warning'
                        if ' error' in line.lower():
                            severity = 'error'
                        if sev_order.get(severity, 1) <= sev_order.get(min_severity, 1):
                            all_issues.append({'severity': severity, 'message': line, 'linter': linter})
                break
            except FileNotFoundError:
                continue
            except subprocess.TimeoutExpired:
                all_issues.append({'severity': 'error', 'message': 'eslint timed out after 60s', 'linter': linter})
            except Exception:
                continue
        
        elif proj_type == 'go' or forced_linter == 'go_vet':
            linter = 'go_vet'
            linters_tried.append(linter)
            cmd = 'go vet ./...'
            try:
                result = subprocess.run(cmd, shell=True, cwd=project_dir,
                                      capture_output=True, text=True, timeout=60, env=env)
                output = result.stdout or result.stderr or ''
                if output.strip():
                    for line in output.strip().split('\n'):
                        line = line.strip()
                        if not line:
                            continue
                        all_issues.append({'severity': 'error', 'message': line, 'linter': 'go_vet'})
                break
            except FileNotFoundError:
                continue
            except subprocess.TimeoutExpired:
                all_issues.append({'severity': 'error', 'message': 'go vet timed out after 60s', 'linter': 'go_vet'})
            except Exception:
                continue
    
    if not linters_tried:
        return 'Error: No linter found. Install ruff (`pip install ruff`), flake8, or eslint.'
    
    # Format output
    if not all_issues:
        return f'✓ No issues found by {", ".join(linters_tried)}. Code looks clean!'
    
    # Group by severity
    errors = [i for i in all_issues if i['severity'] == 'error']
    warnings = [i for i in all_issues if i['severity'] == 'warning']
    infos = [i for i in all_issues if i['severity'] in ('info', 'convention')]
    
    output_lines = [f'Lint results ({", ".join(linters_tried)}): {len(all_issues)} issue(s) found']
    output_lines.append(f'  Errors: {len(errors)}, Warnings: {len(warnings)}, Info: {len(infos)}')
    output_lines.append('')
    
    for issue in all_issues:
        icon = '❌' if issue['severity'] == 'error' else ('⚠️' if issue['severity'] == 'warning' else 'ℹ️')
        output_lines.append(f'{icon} [{issue["linter"]}] {issue["message"]}')
    
    return _truncate('\n'.join(output_lines), 15000)

def _walk_up_dirs(path):
    """Walk up directory tree from path to root."""
    path = os.path.abspath(path)
    while True:
        yield path
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent

# Global cache for sev_order used by run_linter
sev_order = {'error': 0, 'warning': 1, 'info': 2, 'convention': 2}

def _tool_run_tests(args):
    """Run tests in the project. Auto-detects the test framework."""
    from utils import IS_WINDOWS
    target_path = args.get('path')
    if target_path:
        target_path = _validate_path(target_path)
    else:
        target_path = _get_effective_cwd()
    
    forced_framework = args.get('framework', '').strip().lower()
    test_filter = args.get('filter', '').strip()
    verbose = args.get('verbose', False)
    
    # Determine project root
    if os.path.isfile(target_path):
        project_dir = os.path.dirname(target_path)
        for parent in _walk_up_dirs(project_dir):
            if any(os.path.isfile(os.path.join(parent, f)) for f in 
                   ['setup.py', 'pyproject.toml', 'package.json', 'go.mod', 'tsconfig.json']):
                project_dir = parent
                break
    else:
        project_dir = target_path
    
    # Build venv-aware environment
    config = load_config()
    env = os.environ.copy()
    venv_path = config.get('venv_path', '')
    if venv_path and os.path.exists(venv_path):
        _bin_dir = 'Scripts' if IS_WINDOWS else 'bin'
        venv_bin = os.path.join(venv_path, _bin_dir)
        if os.path.exists(venv_bin):
            _path_sep = ';' if IS_WINDOWS else ':'
            env['PATH'] = venv_bin + _path_sep + env.get('PATH', '')
            env['VIRTUAL_ENV'] = venv_path
    
    project_types = _detect_project_type(project_dir)
    
    # Try test frameworks in order
    frameworks_tried = []
    
    for proj_type in project_types:
        if proj_type == 'python' or forced_framework in ('pytest', 'unittest'):
            frameworks = []
            if forced_framework in ('pytest', 'unittest'):
                frameworks = [forced_framework]
            else:
                frameworks = ['pytest', 'unittest']
            
            for fw in frameworks:
                frameworks_tried.append(fw)
                cmd = None
                if fw == 'pytest':
                    cmd_parts = ['pytest', '--tb=short', '-q']
                    if test_filter:
                        cmd_parts.extend(['-k', shlex_quote(test_filter)])
                    if verbose:
                        cmd_parts.append('-v')
                    if os.path.isfile(target_path):
                        cmd_parts.append(shlex_quote(target_path))
                    cmd = ' '.join(cmd_parts)
                elif fw == 'unittest':
                    python_bin = 'python' if IS_WINDOWS else 'python3'
                    cmd_parts = [python_bin, '-m', 'unittest', '-v' if verbose else '-q']
                    if test_filter:
                        cmd_parts.append(shlex_quote(test_filter))
                    cmd = ' '.join(cmd_parts)
                
                if cmd:
                    try:
                        result = subprocess.run(cmd, shell=True, cwd=project_dir,
                                              capture_output=True, text=True, timeout=120, env=env)
                        output = ''
                        if result.stdout:
                            output += result.stdout
                        if result.stderr:
                            output += ('\n' if output else '') + result.stderr
                        
                        # Parse results
                        passed = 0
                        failed = 0
                        errors = 0
                        failures = []
                        
                        if fw == 'pytest':
                            # Parse pytest output
                            # Look for "X passed, Y failed, Z errors"
                            summary_match = re.search(r'(\d+) passed', output)
                            if summary_match:
                                passed = int(summary_match.group(1))
                            fail_match = re.search(r'(\d+) failed', output)
                            if fail_match:
                                failed = int(fail_match.group(1))
                            err_match = re.search(r'(\d+) error', output)
                            if err_match:
                                errors = int(err_match.group(1))
                            
                            # Extract failure details
                            fail_sections = re.split(r'=+ FAILURES =+', output)
                            if len(fail_sections) > 1:
                                for section in fail_sections[1:]:
                                    section = section.strip()
                                    if section:
                                        # Truncate long failures
                                        failures.append(section[:500])
                        else:
                            # unittest output
                            if 'OK' in output:
                                ran_match = re.search(r'Ran (\d+) test', output)
                                if ran_match:
                                    passed = int(ran_match.group(1))
                            elif 'FAILED' in output:
                                ran_match = re.search(r'Ran (\d+) test', output)
                                if ran_match:
                                    total = int(ran_match.group(1))
                                fail_match = re.search(r'failures=(\d+)', output)
                                if fail_match:
                                    failed = int(fail_match.group(1))
                                err_match = re.search(r'errors=(\d+)', output)
                                if err_match:
                                    errors = int(err_match.group(1))
                                passed = total - failed - errors if 'total' in dir() else 0
                                failures.append(output[:1000])
                        
                        # Format structured output
                        total = passed + failed + errors
                        if total == 0 and not output.strip():
                            return f'No tests found by {fw}. Check test file names and locations.'
                        
                        result_lines = [f'Test results ({fw}): {total} test(s)']
                        result_lines.append(f'  ✅ Passed: {passed}')
                        if failed > 0:
                            result_lines.append(f'  ❌ Failed: {failed}')
                        if errors > 0:
                            result_lines.append(f'  💥 Errors: {errors}')
                        
                        if failed > 0 or errors > 0:
                            result_lines.append('')
                            result_lines.append('Failure details:')
                            for i, f in enumerate(failures[:10], 1):
                                result_lines.append(f'--- Failure {i} ---')
                                result_lines.append(f[:500])
                        
                        if verbose and passed > 0:
                            # Include passing test names from output
                            pass_lines = [l for l in output.split('\n') if 'PASSED' in l or l.strip().startswith('test_')]
                            if pass_lines:
                                result_lines.append('')
                                result_lines.append('Passing tests:')
                                for pl in pass_lines[:30]:
                                    result_lines.append(f'  ✅ {pl.strip()[:120]}')
                        
                        return _truncate('\n'.join(result_lines), 15000)
                    
                    except FileNotFoundError:
                        continue
                    except subprocess.TimeoutExpired:
                        return f'Error: {fw} timed out after 120s. Tests may be stuck.'
                    except Exception as e:
                        continue
        
        elif proj_type in ('javascript', 'typescript') or forced_framework in ('jest', 'vitest', 'mocha'):
            frameworks = []
            if forced_framework in ('jest', 'vitest', 'mocha'):
                frameworks = [forced_framework]
            else:
                # Check package.json for test framework
                pkg_json = os.path.join(project_dir, 'package.json')
                test_cmd = None
                if os.path.isfile(pkg_json):
                    try:
                        with open(pkg_json, 'r', encoding='utf-8') as f:
                            pkg = json.load(f)
                        deps = {**pkg.get('dependencies', {}), **pkg.get('devDependencies', {})}
                        if 'vitest' in deps:
                            frameworks = ['vitest']
                        elif 'jest' in deps:
                            frameworks = ['jest']
                        elif 'mocha' in deps:
                            frameworks = ['mocha']
                        test_cmd = pkg.get('scripts', {}).get('test', '')
                    except Exception:
                        pass
                
                if not frameworks:
                    frameworks = ['jest', 'vitest']
            
            for fw in frameworks:
                frameworks_tried.append(fw)
                cmd = None
                if fw == 'jest':
                    cmd_parts = ['npx', 'jest', '--no-coverage']
                    if test_filter:
                        cmd_parts.extend(['-t', shlex_quote(test_filter)])
                    if verbose:
                        cmd_parts.append('--verbose')
                    cmd = ' '.join(cmd_parts)
                elif fw == 'vitest':
                    cmd_parts = ['npx', 'vitest', 'run', '--reporter=verbose']
                    if test_filter:
                        cmd_parts.extend(['-t', shlex_quote(test_filter)])
                    cmd = ' '.join(cmd_parts)
                elif fw == 'mocha':
                    cmd_parts = ['npx', 'mocha']
                    if verbose:
                        cmd_parts.append('--reporter spec')
                    cmd = ' '.join(cmd_parts)
                
                if cmd:
                    try:
                        result = subprocess.run(cmd, shell=True, cwd=project_dir,
                                              capture_output=True, text=True, timeout=120, env=env)
                        output = ''
                        if result.stdout:
                            output += result.stdout
                        if result.stderr:
                            output += ('\n' if output else '') + result.stderr
                        
                        # Parse JS test output
                        passed = 0
                        failed = 0
                        failures = []
                        
                        # Jest format: "Tests: X failed, Y passed, Z total"
                        jest_match = re.search(r'Tests:\s+(\d+)\s+failed.*?(\d+)\s+passed.*?(\d+)\s+total', output)
                        if jest_match:
                            failed = int(jest_match.group(1))
                            passed = int(jest_match.group(2))
                        else:
                            jest_pass = re.search(r'Tests:\s+(\d+)\s+passed', output)
                            if jest_pass:
                                passed = int(jest_pass.group(1))
                        
                        # Vitest format: similar
                        vit_match = re.search(r'(\d+)\s+failed.*?(\d+)\s+passed', output)
                        if vit_match and passed == 0:
                            failed = int(vit_match.group(1))
                            passed = int(vit_match.group(2))
                        
                        # Extract failure details
                        fail_match = re.findall(r'● .*?(?:\n|$)', output)
                        failures = [f.strip()[:300] for f in fail_match[:10]]
                        
                        total = passed + failed
                        if total == 0 and 'no tests found' in output.lower():
                            return f'No tests found by {fw}. Check test configuration.'
                        
                        result_lines = [f'Test results ({fw}): {total} test(s)']
                        result_lines.append(f'  ✅ Passed: {passed}')
                        if failed > 0:
                            result_lines.append(f'  ❌ Failed: {failed}')
                        
                        if failures:
                            result_lines.append('')
                            result_lines.append('Failure details:')
                            for i, f in enumerate(failures, 1):
                                result_lines.append(f'--- Failure {i} ---')
                                result_lines.append(f)
                        
                        return _truncate('\n'.join(result_lines), 15000)
                    
                    except FileNotFoundError:
                        continue
                    except subprocess.TimeoutExpired:
                        return f'Error: {fw} timed out after 120s.'
                    except Exception:
                        continue
        
        elif proj_type == 'go' or forced_framework == 'go_test':
            fw = 'go_test'
            frameworks_tried.append(fw)
            cmd_parts = ['go', 'test', '-v' if verbose else '', './...']
            if test_filter:
                cmd_parts.extend(['-run', shlex_quote(test_filter)])
            cmd = ' '.join(cmd_parts)
            try:
                result = subprocess.run(cmd, shell=True, cwd=project_dir,
                                      capture_output=True, text=True, timeout=120, env=env)
                output = ''
                if result.stdout:
                    output += result.stdout
                if result.stderr:
                    output += ('\n' if output else '') + result.stderr
                
                # Parse go test output
                passed = len(re.findall(r'--- PASS:', output))
                failed = len(re.findall(r'--- FAIL:', output))
                failures = [line.strip()[:300] for line in output.split('\n') if '--- FAIL:' in line]
                
                total = passed + failed
                result_lines = [f'Test results (go test): {total} test(s)']
                result_lines.append(f'  ✅ Passed: {passed}')
                if failed > 0:
                    result_lines.append(f'  ❌ Failed: {failed}')
                    result_lines.append('')
                    result_lines.append('Failure details:')
                    for i, f in enumerate(failures[:10], 1):
                        result_lines.append(f'--- Failure {i} ---')
                        result_lines.append(f)
                
                return _truncate('\n'.join(result_lines), 15000)
            
            except FileNotFoundError:
                continue
            except subprocess.TimeoutExpired:
                return 'Error: go test timed out after 120s.'
            except Exception as e:
                continue
    
    if not frameworks_tried:
        return 'Error: No test framework detected. Install pytest, jest, or specify framework explicitly.'
    
    return f'Error: Could not run tests with {", ".join(frameworks_tried)}. Try installing the test framework or specify framework parameter.'

def _tool_find_definition(args):
    """Find definition of a symbol using AST (tree-sitter) semantic analysis."""
    symbol = args['symbol']
    search_path = _validate_path(args.get('path', '.') if args.get('path', '.') != '.' else _get_project_dir())
    if not os.path.isdir(search_path) and not os.path.isfile(search_path):
        return f'Error: Path not found: {search_path}'

    skip_dirs = {'.git', '__pycache__', 'node_modules', '.venv', 'venv', '.idea', '.vscode', 'dist', 'build', '.next'}
    supported_ext = {'.py', '.js', '.ts', '.tsx', '.jsx', '.go', '.mjs', '.cjs'}
    results = []
    search_start = time.time()

    # Collect files to search
    if os.path.isfile(search_path):
        file_list = [search_path]
    else:
        file_list = []
        for root, dirs, files in os.walk(search_path):
            if time.time() - search_start > 20:
                break
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext in supported_ext:
                    file_list.append(os.path.join(root, fname))

    # Use AST to find definitions
    for fpath in file_list:
        if time.time() - search_start > 20 or len(results) >= 20:
            break
        try:
            defs = extract_definitions(fpath)
            for d in defs:
                if d['name'] == symbol:
                    rel = os.path.relpath(fpath, search_path)
                    try:
                        with open(fpath, 'r', encoding='utf-8', errors='ignore') as f:
                            lines = f.readlines()
                        line_idx = d['line'] - 1
                        ctx_start = max(0, line_idx - 2)
                        ctx_end = min(len(lines), line_idx + 6)
                        context = ''.join(
                            f'  {"→" if j == line_idx else " "} {j+1:>5}\t{lines[j].rstrip()}\n'
                            for j in range(ctx_start, ctx_end)
                        )
                    except Exception:
                        context = ''
                    parent_info = f' (in {d["parent"]})' if d.get('parent') else ''
                    results.append(f'{d["kind"]}{parent_info} in {rel}:{d["line"]}\n{context}')
        except (PermissionError, OSError):
            continue

    if not results:
        return f'No definition found for "{symbol}"'
    return f'Found {len(results)} definition(s) for "{symbol}":\n' + '\n---\n'.join(results[:20])

def _tool_find_references(args):
    """Find all references/usages of a symbol using AST (tree-sitter).
    Skips string literals and comments for accurate results.
    Falls back to regex for unsupported file types.
    """
    symbol = args['symbol']
    search_path = _validate_path(args.get('path', '.') if args.get('path', '.') != '.' else _get_project_dir())
    if not os.path.isdir(search_path) and not os.path.isfile(search_path):
        return f'Error: Path not found: {search_path}'

    include_tests = args.get('include_tests', True)
    skip_dirs = {'.git', '__pycache__', 'node_modules', '.venv', 'venv', '.idea', '.vscode', 'dist', 'build', '.next'}
    if not include_tests:
        skip_dirs.add('tests')
        skip_dirs.add('test')

    ast_ext = {'.py', '.js', '.ts', '.tsx', '.jsx', '.go', '.mjs', '.cjs'}
    fallback_ext = {'.json', '.yaml', '.yml', '.md', '.html', '.css', '.sh', '.toml', '.cfg', '.ini'}
    results = []
    search_start = time.time()
    max_results = 50

    if os.path.isfile(search_path):
        file_list = [search_path]
    else:
        file_list = []
        for root, dirs, files in os.walk(search_path):
            if time.time() - search_start > 20:
                break
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext in ast_ext or ext in fallback_ext:
                    file_list.append(os.path.join(root, fname))

    for fpath in file_list:
        if time.time() - search_start > 20 or len(results) >= max_results:
            break
        ext = os.path.splitext(fpath)[1].lower()
        rel = os.path.relpath(fpath, search_path)

        try:
            if ext in ast_ext:
                # Use AST for source code files — skips strings/comments
                refs = find_references_ast(fpath, symbol)
                for r in refs:
                    results.append(f'{rel}:{r["line"]}: {r["text"]}')
            else:
                # Fallback to regex for config/text files
                with open(fpath, 'r', encoding='utf-8', errors='ignore') as f:
                    lines = f.readlines()
                word_pattern = re.escape(symbol)
                regex = re.compile(r'\b' + word_pattern + r'\b')
                for i, line in enumerate(lines):
                    if regex.search(line):
                        results.append(f'{rel}:{i+1}: {line.rstrip()}')
                        if len(results) >= max_results:
                            break
        except (PermissionError, OSError):
            continue

    if not results:
        return f'No references found for "{symbol}"'
    output = f'Found {len(results)} reference(s) for "{symbol}":\n'
    output += '\n'.join(results)
    if len(results) >= max_results:
        output += f'\n[showing first {max_results} results]'
    return output

def _tool_file_structure(args):
    """Parse source file and return structural outline using AST (tree-sitter).
    Falls back to regex for unsupported file types.
    """
    path = _validate_path(args['path'])
    if not os.path.isfile(path):
        return f'Error: File not found: {path}'

    ext = os.path.splitext(path)[1].lower()
    rel = os.path.relpath(path, WORKSPACE)

    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
    except Exception as e:
        return f'Error reading file: {e}'

    # Try AST first
    struct = get_file_structure(path)
    if struct:
        outline = [f'File: {rel} ({len(lines)} lines) [AST]', '']
        if struct.get('imports'):
            outline.append(f'Imports ({len(struct["imports"])}):')
            for imp in struct['imports'][:30]:
                outline.append(f'  L{imp["line"]}: {imp["text"]}')
            if len(struct['imports']) > 30:
                outline.append(f'  ... and {len(struct["imports"])-30} more imports')
            outline.append('')
        if struct.get('classes'):
            outline.append(f'Classes/Types ({len(struct["classes"])}):')
            for cls in struct['classes']:
                outline.append(f'  L{cls["line"]}: {cls["text"]}')
            outline.append('')
        if struct.get('functions'):
            outline.append(f'Functions/Methods ({len(struct["functions"])}):')
            for fn in struct['functions'][:80]:
                outline.append(f'  L{fn["line"]}: {fn["text"]}')
            if len(struct['functions']) > 80:
                outline.append(f'  ... and {len(struct["functions"])-80} more functions')
            outline.append('')
        if struct.get('variables'):
            outline.append(f'Constants/Variables ({len(struct["variables"])}):')
            for var in struct['variables'][:20]:
                parent_info = f' (in {var["parent"]})' if var.get('parent') else ''
                outline.append(f'  L{var["line"]}: {var["text"]}{parent_info}')
            outline.append('')
        total = len(struct.get('imports', [])) + len(struct.get('classes', [])) + \
                len(struct.get('functions', [])) + len(struct.get('variables', []))
        outline.append(f'Total: {total} symbols')
        return '\n'.join(outline)

    # Fallback to regex for unsupported extensions
    return _file_structure_regex(path, ext, rel, lines)


def _file_structure_regex(path, ext, rel, lines):
    """Regex-based file structure fallback for unsupported file types."""
    outline = [f'File: {rel} ({len(lines)} lines)', '']
    if ext not in ('.py', '.js', '.ts', '.tsx', '.jsx', '.go'):
        return f'Unsupported file type: {ext}. Supported: .py, .js, .ts, .tsx, .jsx, .go'

    imports = []
    classes = []
    functions = []
    variables = []

    if ext == '.py':
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith('import ') or stripped.startswith('from '):
                imports.append(f'  L{i+1}: {stripped[:100]}')
            class_m = re.match(r'^(\s*)class\s+(\w+)', line)
            if class_m:
                name = class_m.group(2)
                paren = stripped.find('(')
                bases = stripped[paren:stripped.find(')')+1] if paren > 0 and ')' in stripped else ''
                classes.append(f'  L{i+1}: class {name}{bases}')
            func_m = re.match(r'^(\s*)(async\s+)?def\s+(\w+)', line)
            if func_m:
                name = func_m.group(3)
                prefix = 'async ' if func_m.group(2) else ''
                paren = stripped.find('(')
                params = stripped[paren:stripped.find(')', paren)+1] if paren > 0 and ')' in stripped[paren:] else '()'
                functions.append(f'  L{i+1}: {prefix}def {name}{params}')
    elif ext in ('.js', '.ts', '.tsx', '.jsx'):
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith('import ') or stripped.startswith('export '):
                imports.append(f'  L{i+1}: {stripped[:100]}')
            class_m = re.match(r'^(\s*)(export\s+)?(default\s+)?class\s+(\w+)', line)
            if class_m:
                classes.append(f'  L{i+1}: class {class_m.group(4)}')
            func_m = re.match(r'^(\s*)(export\s+)?(default\s+)?(async\s+)?function\s+(\w+)', line)
            if func_m:
                functions.append(f'  L{i+1}: function {func_m.group(5)}')
    elif ext == '.go':
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith('import '):
                imports.append(f'  L{i+1}: {stripped[:100]}')
            func_m = re.match(r'^\s*func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)', line)
            if func_m:
                functions.append(f'  L{i+1}: func {func_m.group(1)}')
            type_m = re.match(r'^\s*type\s+(\w+)\s+struct', line)
            if type_m:
                classes.append(f'  L{i+1}: type {type_m.group(1)} struct')

    if imports:
        outline.append(f'Imports ({len(imports)}):')
        outline.extend(imports[:30])
        outline.append('')
    if classes:
        outline.append(f'Classes ({len(classes)}):')
        outline.extend(classes)
        outline.append('')
    if functions:
        outline.append(f'Functions ({len(functions)}):')
        outline.extend(functions[:50])
        outline.append('')
    if variables:
        outline.append(f'Constants ({len(variables)}):')
        outline.extend(variables[:20])
        outline.append('')

    total_items = len(imports) + len(classes) + len(functions) + len(variables)
    if total_items == 0:
        return f'No structure found in {rel} (empty or unrecognized format)'
    outline.append(f'Total: {total_items} symbols')
    return '\n'.join(outline)

# Read-only tools available to sub-agents (read mode)
_SUBAGENT_TOOLS = {
    'read_file': _tool_read_file,
    'glob_files': _tool_glob_files,
    'grep_code': _tool_grep_code,
    'search_files': _tool_search_files,
    'list_directory': _tool_list_directory,
    'file_info': _tool_file_info,
    'file_structure': _tool_file_structure,
    'find_definition': _tool_find_definition,
    'find_references': _tool_find_references,
    'web_search': _tool_web_search,
    'web_fetch': _tool_web_fetch,
    'run_linter': _tool_run_linter,
    'run_tests': _tool_run_tests,
}

# Write-capable tools for sub-agents (write mode) — includes all read tools + write/edit/run
_WRITE_SUBAGENT_TOOLS = dict(_SUBAGENT_TOOLS)
_WRITE_SUBAGENT_TOOLS.update({
    'write_file': _tool_write_file,
    'edit_file': _tool_edit_file,
    'run_command': _tool_run_command,
    'install_package': _tool_install_package,
    'create_directory': _tool_create_directory,
    'delete_path': _tool_delete_path,
    'move_file': _tool_move_file,
    'append_file': _tool_append_file,
    'git_status': _tool_git_status,
    'git_diff': _tool_git_diff,
    'git_commit': _tool_git_commit,
    'git_log': _tool_git_log,
    'git_checkout': _tool_git_checkout,
})

# Tool definitions for sub-agent API calls (read mode)
_SUBAGENT_TOOL_DEFS = [t for t in AGENT_TOOLS if t['function']['name'] in _SUBAGENT_TOOLS]

# Tool definitions for write-mode sub-agents
_WRITE_SUBAGENT_TOOL_DEFS = [t for t in AGENT_TOOLS if t['function']['name'] in _WRITE_SUBAGENT_TOOLS]

# ==================== Todo Storage ====================
_muside_knowledge_cache = {}  # {cache_key_tuple: {content, files, time}}

def _get_muside_cache():
    """Return the module-level .muside/ knowledge cache."""
    return _muside_knowledge_cache

_active_todos = {
    'todos': [],
    'lock': threading.Lock(),
}

def _tool_todo_read(args):
    """Read the current todo list."""
    with _active_todos['lock']:
        todos = _active_todos['todos']
    if not todos:
        return 'No active todos.'
    lines = []
    for t in todos:
        status_icon = {'pending': '○', 'in_progress': '◐', 'completed': '●'}.get(t.get('status', ''), '○')
        priority_tag = {'high': '🔴', 'medium': '🟡', 'low': '🟢'}.get(t.get('priority', ''), '')
        lines.append(f'{status_icon} [{t.get("id", "?")}] {priority_tag} {t.get("content", "")} ({t.get("status", "pending")})')
    return '\n'.join(lines)

def _tool_todo_write(args):
    """Write/update the todo list."""
    todos = args.get('todos')
    if not isinstance(todos, list):
        return 'Error: todos must be an array of {id, content, status} objects'
    # Validate each item
    for i, t in enumerate(todos):
        if not isinstance(t, dict):
            return f'Error: todo[{i}] must be an object'
        if not t.get('id') or not t.get('content') or not t.get('status'):
            return f'Error: todo[{i}] missing required fields (id, content, status)'
        if t.get('status') not in ('pending', 'in_progress', 'completed'):
            return f'Error: todo[{i}] invalid status "{t.get("status")}", must be pending/in_progress/completed'
        if t.get('priority') and t.get('priority') not in ('high', 'medium', 'low'):
            return f'Error: todo[{i}] invalid priority "{t.get("priority")}", must be high/medium/low'
    with _active_todos['lock']:
        _active_todos['todos'] = todos
    # Build summary
    total = len(todos)
    completed = sum(1 for t in todos if t.get('status') == 'completed')
    in_progress = sum(1 for t in todos if t.get('status') == 'in_progress')
    pending = total - completed - in_progress
    return f'Todo list updated: {total} items ({completed} completed, {in_progress} in progress, {pending} pending)'

# ==================== Sub-Agent Engine ====================
def _run_subagent(task, mode='read', max_iterations=8, llm_config=None, context=None):
    """Core sub-agent execution engine. Used by both delegate_task and parallel_tasks.

    Args:
        task: Task description string.
        mode: 'read' for read-only tools, 'write' for full tools.
        max_iterations: Max agent loop iterations (1-15).
        llm_config: LLM config dict. If None, loads from default.

    Returns:
        Summary string of what the sub-agent found/did.
    """
    if not task:
        return 'Error: task description is required'
    max_iters = min(max(max_iterations, 1), 15)

    if llm_config is None:
        try:
            config = load_config()
            llm_config = get_active_llm_config(config)
        except Exception as e:
            return f'Error loading LLM config: {e}'

    is_write_mode = (mode == 'write')
    sub_tools = _WRITE_SUBAGENT_TOOLS if is_write_mode else _SUBAGENT_TOOLS
    sub_tool_defs = _WRITE_SUBAGENT_TOOL_DEFS if is_write_mode else _SUBAGENT_TOOL_DEFS

    # Build sub-agent system prompt based on mode
    # Include workspace/project path so sub-agent knows where to operate
    try:
        from utils import load_config
        _sub_cfg = load_config()
        _sub_ws = _sub_cfg.get('workspace', WORKSPACE)
        _sub_prj = _sub_cfg.get('project', None)
        _sub_project_dir = os.path.join(_sub_ws, _sub_prj) if _sub_prj else _sub_ws
        if not os.path.isdir(_sub_project_dir):
            _sub_project_dir = _sub_ws
    except Exception:
        _sub_project_dir = WORKSPACE

    if is_write_mode:
        system_prompt = (
            'You are a write-capable sub-agent. You can read files, write/edit files, run commands, and manage git.\n'
            'You have access to a full set of tools for code modification.\n'
            f'Project directory: {_sub_project_dir}\n'
            'IMPORTANT RULES:\n'
            '1. Always read a file before modifying it\n'
            '2. Test your changes with run_command when possible\n'
            '3. Use edit_file for targeted changes, write_file only for new files\n'
            '4. When done, provide a clear summary of ALL changes you made (files modified/created)\n'
            '5. If you encounter errors, try to fix them before reporting\n'
            '6. Be efficient — minimize unnecessary iterations'
        )
    else:
        system_prompt = (
            'You are a research sub-agent. Your job is to gather information and return a concise summary.\n'
            'You have access to read-only tools (read_file, glob_files, grep_code, search_files, list_directory, '
            'file_info, file_structure, find_definition, find_references, web_search, web_fetch).\n'
            f'Project directory: {_sub_project_dir}\n'
            'Be thorough but concise. Focus on factual findings.\n'
            'When done, provide a clear summary of what you found.'
        )

    # Build task message with optional context from main agent
    user_msg = task
    if context:
        user_msg = f'[Context from main agent]\n{context}\n\n[Sub-task]\n{task}'

    sub_context = [
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': user_msg},
    ]

    tool_results_summary = []

    for iteration in range(max_iters):
        try:
            api_messages = _build_api_messages(sub_context, llm_config, skip_system_inject=True)
            payload = {
                'model': llm_config.get('model', 'gpt-4o-mini'),
                'messages': api_messages,
                'temperature': 0.3,
                'max_tokens': 4096,
                'tools': sub_tool_defs,
                'tool_choice': 'auto',
            }
            url, headers = _get_llm_endpoint(llm_config, payload['model'])
            headers = headers or {}
            headers.setdefault('Content-Type', 'application/json; charset=utf-8')
            body_bytes = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            req = urllib.request.Request(url, body_bytes, headers=headers, method='POST')
            with _urllib_opener.open(req, timeout=120) as resp:
                response = json.loads(resp.read().decode('utf-8'))
        except Exception as e:
            tool_results_summary.append(f'[Error iteration {iteration+1}] {str(e)}')
            break

        choice = response.get('choices', [{}])[0]
        message = choice.get('message', {})
        content = message.get('content', '') or ''
        tool_calls = message.get('tool_calls', [])

        if content:
            sub_context.append({'role': 'assistant', 'content': content})
            tool_results_summary.append(f'[Iteration {iteration+1}] {content[:500]}')

        if not tool_calls:
            break

        sub_context.append({'role': 'assistant', 'content': content or None, 'tool_calls': tool_calls})

        for tc in tool_calls:
            func = tc.get('function', {})
            tool_name = func.get('name', '')
            raw_args = func.get('arguments', '{}')
            tool_args, _ = _parse_tool_args(raw_args, tool_name)

            handler = sub_tools.get(tool_name)
            if handler:
                try:
                    result = handler(tool_args)
                except Exception as e:
                    result = f'Error: {e}'
            else:
                result = f'Error: Sub-agent cannot use tool "{tool_name}" (not available in {mode} mode)'

            tool_results_summary.append(f'[{tool_name}] {_truncate(result, 300)}')
            sub_context.append({
                'role': 'tool',
                'tool_call_id': tc.get('id', ''),
                'name': tool_name,
                'content': result,
            })

    mode_label = 'Write' if is_write_mode else 'Read'
    output = f'[{mode_label} sub-agent] Completed ({min(iteration+1, max_iters)}/{max_iters} iterations):\n\n'
    output += '\n'.join(tool_results_summary)
    return _truncate(output, 15000)

def _tool_kill_port(args):
    """Kill any process listening on a specific port."""
    from utils import IS_WINDOWS
    import subprocess as sp
    port = args.get('port')
    if not port:
        return 'Error: port parameter is required'
    try:
        port = int(port)
    except (ValueError, TypeError):
        return f'Error: invalid port number: {port}'

    if not (1 <= port <= 65535):
        return f'Error: port must be between 1 and 65535, got {port}'

    # SAFETY: Never kill the IDE's own port
    ide_port = int(os.environ.get('MUSIDE_PORT', 12345))
    if port == ide_port:
        return f'⛔ BLOCKED: Port {port} is the MusIDE server port — killing it would shut down the IDE and AI assistant. Operation refused.'

    killed_pids = []
    errors = []

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
                        killed_pids.append(pid)
                    except Exception as e:
                        errors.append(f'Failed to kill PID {pid}: {e}')
        except Exception as e:
            errors.append(f'netstat error: {e}')
    else:
        try:
            result = sp.run(
                f'lsof -ti :{port}',
                shell=True, capture_output=True, text=True, timeout=5
            )
            pids = result.stdout.strip().splitlines()
            for pid_str in pids:
                pid_str = pid_str.strip()
                if pid_str:
                    try:
                        os.kill(int(pid_str), 9)
                        killed_pids.append(pid_str)
                    except (OSError, ValueError) as e:
                        errors.append(f'Failed to kill PID {pid_str}: {e}')
        except Exception as e:
            errors.append(f'lsof error: {e}')

    # Also stop any of our managed processes that might be using this port
    from utils import stop_process, running_processes
    for proc_id, info in list(running_processes.items()):
        if info.get('running') and str(port) in info.get('cmd', ''):
            stop_process(proc_id)
            killed_pids.append(f'managed:{proc_id}')

    result_parts = [f'Port: {port}']
    if killed_pids:
        result_parts.append(f'Killed processes: {killed_pids}')
    else:
        result_parts.append('No processes found listening on this port')
    if errors:
        result_parts.append(f'Errors: {errors}')

    return '\n'.join(result_parts)

# ==================== Audio / Music Production Tools ====================

def _tool_play_audio(args):
    track_id = args.get('track_id')
    msg = f'[MusIDE] Play audio' + (f' track {track_id}' if track_id else ' all tracks')
    return msg + '\nNote: Playback is controlled by the browser TrackEditor. Use the play button or send this command to trigger playback.'

def _tool_stop_audio(args):
    return '[MusIDE] Stop audio playback. Playback stopped.'

def _tool_pause_audio(args):
    return '[MusIDE] Pause audio playback. Paused at current position.'

def _tool_seek_audio(args):
    t = args.get('time', 0)
    return f'[MusIDE] Seek to {t}s.'

def _tool_load_audio(args):
    track_id = args.get('track_id', '')
    file_path = _resolve_path(args.get('file_path', ''))
    try:
        file_path = _validate_path(file_path)
    except ValueError as e:
        return f'Error: {e}'
    if not os.path.isfile(file_path):
        return f'Error: Audio file not found: {file_path}'
    start_time = args.get('start_time', 0)
    size = os.path.getsize(file_path)
    ext = os.path.splitext(file_path)[1].lower()
    supported = ['.wav', '.mp3', '.ogg', '.flac', '.aiff', '.aac', '.m4a']
    if ext not in supported:
        return f'Error: Unsupported audio format: {ext}. Supported: {", ".join(supported)}'
    return f'[MusIDE] Audio loaded: {os.path.basename(file_path)} ({size} bytes, {ext}) into track {track_id} at {start_time}s'

def _tool_edit_audio(args):
    track_id = args.get('track_id', '')
    clip_index = args.get('clip_index', 0)
    action = args.get('action', '')
    result_parts = [f'[MusIDE] Edit audio: track={track_id}, clip={clip_index}, action={action}']
    if action == 'trim':
        result_parts.append(f'trim from {args.get("start", 0)}s to {args.get("end", 0)}s')
    elif action in ('fade_in', 'fade_out'):
        result_parts.append(f'duration={args.get("duration", 1)}s')
    elif action in ('change_speed', 'change_pitch'):
        result_parts.append(f'factor={args.get("factor", 1.0)}')
    return ', '.join(result_parts)

def _tool_export_audio(args):
    output_path = args.get('output_path', 'mixdown.wav')
    fmt = args.get('format', 'wav')
    track_ids = args.get('track_ids')
    start_time = args.get('start_time', 0)
    end_time = args.get('end_time')
    info = f'[MusIDE] Export audio: {output_path} (format={fmt}'
    if track_ids:
        info += f', tracks={track_ids}'
    if start_time or end_time:
        info += f', range={start_time}-{end_time}s'
    info += ')'
    return info

def _tool_record_audio(args):
    track_id = args.get('track_id', '')
    action = args.get('action', 'start')
    duration = args.get('duration')
    msg = f'[MusIDE] Record audio: {action} on track {track_id}'
    if duration:
        msg += f' for {duration}s'
    return msg

def _tool_list_tracks(args):
    return json.dumps({
        'tracks': [
            {'id': 'track-0', 'name': '鼓组', 'volume': 0.8, 'pan': 0, 'mute': False, 'solo': False, 'clips': 0},
            {'id': 'track-1', 'name': '贝斯', 'volume': 0.75, 'pan': 0, 'mute': False, 'solo': False, 'clips': 0},
            {'id': 'track-2', 'name': '旋律', 'volume': 0.7, 'pan': 0, 'mute': False, 'solo': False, 'clips': 0},
            {'id': 'track-3', 'name': '人声', 'volume': 0.85, 'pan': 0, 'mute': False, 'solo': False, 'clips': 0},
        ],
        'total': 4,
        'bpm': 120,
        'time_signature': '4/4',
    }, ensure_ascii=False)

def _tool_add_track(args):
    name = args.get('name', '新音轨')
    color = args.get('color')
    track_id = f'track-{int(time.time()*1000)}'
    result = {'id': track_id, 'name': name, 'volume': 0.8, 'pan': 0, 'mute': False, 'solo': False}
    if color:
        result['color'] = color
    return json.dumps(result, ensure_ascii=False)

def _tool_remove_track(args):
    track_id = args.get('track_id', '')
    return f'[MusIDE] Track {track_id} removed.'

def _tool_set_track_volume(args):
    track_id = args.get('track_id', '')
    vol = args.get('volume', 0.8)
    return f'[MusIDE] Track {track_id} volume set to {vol}'

def _tool_set_track_pan(args):
    track_id = args.get('track_id', '')
    pan = args.get('pan', 0)
    return f'[MusIDE] Track {track_id} pan set to {pan}'

def _tool_set_track_mute(args):
    track_id = args.get('track_id', '')
    muted = args.get('muted', False)
    return f'[MusIDE] Track {track_id} {"muted" if muted else "unmuted"}'

def _tool_set_track_solo(args):
    track_id = args.get('track_id', '')
    soloed = args.get('soloed', False)
    return f'[MusIDE] Track {track_id} {"soloed" if soloed else "unsoloed"}'

def _tool_set_bpm(args):
    bpm = args.get('bpm', 120)
    if bpm < 20 or bpm > 300:
        return 'Error: BPM must be between 20 and 300'
    return f'[MusIDE] BPM set to {bpm}'

def _tool_set_time_signature(args):
    num = args.get('numerator', 4)
    den = args.get('denominator', 4)
    if den not in (1, 2, 4, 8, 16):
        return f'Error: Invalid denominator: {den}. Must be 1, 2, 4, 8, or 16'
    return f'[MusIDE] Time signature set to {num}/{den}'

def _tool_get_project_info(args):
    return json.dumps({
        'name': '未命名项目',
        'bpm': 120,
        'time_signature': [4, 4],
        'duration': 0,
        'tracks': 4,
        'sample_rate': 44100,
        'bit_depth': 16,
    }, ensure_ascii=False)

def _tool_delegate_task(args):
    """Launch a sub-agent for a subtask. Supports read and write modes."""
    task = args.get('task', '').strip()
    mode = args.get('mode', 'read').strip()
    max_iters = args.get('max_iterations', 8)
    context = args.get('context', '').strip() or None
    # Load the current active LLM config so sub-agent uses the same model
    try:
        _cfg = load_config()
        _llm_cfg = get_active_llm_config(_cfg)
    except Exception:
        _llm_cfg = None
    return _run_subagent(task, mode=mode, max_iterations=max_iters, llm_config=_llm_cfg, context=context)

def _tool_parallel_tasks(args):
    """Launch multiple sub-agents in parallel."""
    tasks = args.get('tasks', [])
    if not isinstance(tasks, list) or len(tasks) == 0:
        return 'Error: tasks must be a non-empty array of {task, mode?} objects'
    if len(tasks) > 4:
        return 'Error: max 4 parallel tasks supported'
    for i, t in enumerate(tasks):
        if not t.get('task'):
            return f'Error: tasks[{i}] missing required "task" field'

    # Load LLM config once
    try:
        config = load_config()
        llm_config = get_active_llm_config(config)
    except Exception as e:
        return f'Error loading LLM config: {e}'

    # Run sub-agents in parallel threads
    results = [None] * len(tasks)

    def _run_one(idx, task_def):
        task_text = task_def.get('task', '')
        mode = task_def.get('mode', 'read').strip()
        max_iters = task_def.get('max_iterations', 8)
        ctx = task_def.get('context', '').strip() or None
        results[idx] = _run_subagent(task_text, mode=mode, max_iterations=max_iters, llm_config=llm_config, context=ctx)

    threads = []
    for i, t in enumerate(tasks):
        thread = threading.Thread(target=_run_one, args=(i, t))
        threads.append(thread)
        thread.start()

    for thread in threads:
        thread.join(timeout=300)  # 5 min max per parallel batch

    # Combine results
    output_parts = [f'=== Parallel Tasks Results ({len(tasks)} tasks) ===']
    for i, result in enumerate(results):
        mode_label = tasks[i].get('mode', 'read')
        output_parts.append(f'\n--- Task {i+1} [{mode_label}]: {tasks[i].get("task", "")[:80]} ---')
        output_parts.append(result if result else '(task did not return a result)')
    return _truncate('\n'.join(output_parts), 15000)

_TOOL_HANDLERS = {
    'read_file': _tool_read_file,
    'write_file': _tool_write_file,
    'edit_file': _tool_edit_file,
    'list_directory': _tool_list_directory,
    'search_files': _tool_search_files,
    'run_command': _tool_run_command,
    'git_status': _tool_git_status,
    'git_diff': _tool_git_diff,
    'git_commit': _tool_git_commit,
    'git_log': _tool_git_log,
    'git_checkout': _tool_git_checkout,
    'install_package': _tool_install_package,
    'list_packages': _tool_list_packages,
    'grep_code': _tool_grep_code,
    'file_info': _tool_file_info,
    'create_directory': _tool_create_directory,
    'delete_path': _tool_delete_path,
    'move_file': _tool_move_file,
    'append_file': _tool_append_file,
    'web_search': _tool_web_search,
    'web_fetch': _tool_web_fetch,
    'browser_navigate': _tool_browser_navigate,
    'browser_console': _tool_browser_console,
    'browser_page_info': _tool_browser_page_info,
    'browser_evaluate': _tool_browser_evaluate,
    'browser_inspect': _tool_browser_inspect,
    'browser_query_all': _tool_browser_query_all,
    'browser_click': _tool_browser_click,
    'browser_input': _tool_browser_input,
    'browser_cookies': _tool_browser_cookies,
    'server_logs': _tool_server_logs,
    # P0+P1 new tools
    'glob_files': _tool_glob_files,
    'find_definition': _tool_find_definition,
    'find_references': _tool_find_references,
    'file_structure': _tool_file_structure,
    'delegate_task': _tool_delegate_task,
    'parallel_tasks': _tool_parallel_tasks,
    'todo_write': _tool_todo_write,
    'todo_read': _tool_todo_read,
    # Quality Assurance tools
    'run_linter': _tool_run_linter,
    'run_tests': _tool_run_tests,
    # Process & Port Management
    'kill_port': _tool_kill_port,
    # Audio / Music Production tools
    'play_audio': _tool_play_audio,
    'stop_audio': _tool_stop_audio,
    'pause_audio': _tool_pause_audio,
    'seek_audio': _tool_seek_audio,
    'load_audio': _tool_load_audio,
    'edit_audio': _tool_edit_audio,
    'export_audio': _tool_export_audio,
    'record_audio': _tool_record_audio,
    'list_tracks': _tool_list_tracks,
    'add_track': _tool_add_track,
    'remove_track': _tool_remove_track,
    'set_track_volume': _tool_set_track_volume,
    'set_track_pan': _tool_set_track_pan,
    'set_track_mute': _tool_set_track_mute,
    'set_track_solo': _tool_set_track_solo,
    'set_bpm': _tool_set_bpm,
    'set_time_signature': _tool_set_time_signature,
    'get_project_info': _tool_get_project_info,
}

def execute_agent_tool(name, arguments):
    """Execute a tool by name with given arguments. Returns (ok, result_string, elapsed_seconds)."""
    handler = _TOOL_HANDLERS.get(name)
    if not handler:
        return False, f'Error: Unknown tool "{name}". Available tools: {", ".join(_TOOL_HANDLERS.keys())}', 0
    t0 = time.time()
    try:
        result = handler(arguments)
        elapsed = time.time() - t0
        return True, result, elapsed
    except ValueError as e:
        return False, f'Security error: {e}', time.time() - t0
    except Exception as e:
        return False, f'Tool execution error: {str(e)}', time.time() - t0

# Global tool execution timeout (prevents any single tool from hanging the agent loop)
TOOL_EXECUTION_TIMEOUT = 120  # seconds

def execute_agent_tool_with_timeout(name, arguments):
    """Execute a tool with a global timeout to prevent hanging the agent loop."""
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(execute_agent_tool, name, arguments)
        try:
            return future.result(timeout=TOOL_EXECUTION_TIMEOUT)
        except concurrent.futures.TimeoutError:
            elapsed = time.time() - t0
            return False, f'Error: Tool "{name}" timed out after {TOOL_EXECUTION_TIMEOUT}s', elapsed

# Read-only tools that can safely run in parallel (no side effects)
_READONLY_TOOLS = frozenset({
    'read_file', 'glob_files', 'grep_code', 'search_files', 'list_directory',
    'file_info', 'file_structure', 'find_definition', 'find_references',
    'list_packages', 'git_status', 'git_diff', 'git_log',
    'web_search', 'web_fetch',
    'browser_page_info', 'browser_console', 'browser_evaluate',
    'browser_inspect', 'browser_query_all', 'browser_cookies',
    'server_logs',
    'run_linter', 'run_tests',
})

def _execute_tools_parallel(tool_calls_raw, emit_fn=None):
    """Execute multiple read-only tools in parallel for speed.
    
    If ALL tools in the batch are read-only, execute them concurrently (max 8 threads).
    If ANY tool has side effects (write/delete/run), fall back to sequential execution.
    
    Returns list of (tool_name, ok, result_str, elapsed, tool_call_id) tuples.
    """
    # Check if all tools are read-only
    all_readonly = True
    for tc in tool_calls_raw:
        func = tc.get('function', {})
        name = func.get('name', '')
        if name not in _READONLY_TOOLS:
            all_readonly = False
            break

    if len(tool_calls_raw) < 2 or not all_readonly:
        return None  # Signal caller to use sequential execution

    # Parallel execution
    results = [None] * len(tool_calls_raw)

    def _run_one(idx, tc):
        func = tc.get('function', {})
        tool_name = func.get('name', '')
        raw_args = func.get('arguments', '{}')
        tool_args, _ = _parse_tool_args(raw_args, tool_name)
        tool_call_id = tc.get('id', f'call_{tool_name}')
        ok, result_str, elapsed = execute_agent_tool_with_timeout(tool_name, tool_args)
        return (idx, tool_name, ok, result_str, elapsed, tool_call_id)

    max_workers = min(len(tool_calls_raw), 8)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_run_one, i, tc): i for i, tc in enumerate(tool_calls_raw)}
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results[result[0]] = result

    return results

# ==================== System Prompt Caching & Budget ====================
_SYSTEM_PROMPT_CACHE = {}  # {cache_key: {'prompt': str, 'tokens': int, 'time': float}}
_SYSTEM_PROMPT_CACHE_TTL = 60  # seconds — cache the full system prompt for 60s
_SYSTEM_PROMPT_MAX_TOKENS = 4500  # max tokens for the system prompt


def _trim_system_prompt_to_budget(sys_prompt, max_tokens=_SYSTEM_PROMPT_MAX_TOKENS):
    """Trim system prompt to fit within token budget.

    Trimming priority (least important first):
    1. AST index / Project Symbols section
    2. .muside/ Project Knowledge section
    3. System Environment section
    If still too large, truncate the trailing sections.
    """
    estimated = _estimate_tokens(sys_prompt)
    if estimated <= max_tokens:
        return sys_prompt

    # Strategy: remove sections from least to most important
    sections = [
        ('## Project Symbols', '## Project Knowledge'),
        ('## Project Knowledge', '## Current Project'),
        ('## System Environment', '## Current Project'),
    ]

    for section_start, next_section in sections:
        if estimated <= max_tokens * 0.85:
            break
        start_idx = sys_prompt.find(section_start)
        if start_idx == -1:
            continue
        end_idx = sys_prompt.find(next_section, start_idx)
        if end_idx == -1:
            end_idx = len(sys_prompt)
        # Remove from section_start to just before next_section
        removed = sys_prompt[start_idx:end_idx]
        sys_prompt = sys_prompt[:start_idx] + sys_prompt[end_idx:]
        estimated = _estimate_tokens(sys_prompt)
        log_write(f'[muside] Trimmed system prompt section "{section_start}" ({len(removed)} chars removed, ~{estimated} tokens remaining)')

    # If still too large, hard-truncate the less critical trailing content
    if estimated > max_tokens:
        # Keep the core DEFAULT_SYSTEM_PROMPT (usually first ~3500 chars) and trim injections
        base_prompt_end = sys_prompt.find('\n\n## ')
        if base_prompt_end > 0 and base_prompt_end < len(sys_prompt) * 0.7:
            base = sys_prompt[:base_prompt_end]
            injections = sys_prompt[base_prompt_end:]
            inj_tokens = _estimate_tokens(injections)
            budget_left = max_tokens - _estimate_tokens(base)
            if inj_tokens > budget_left and budget_left > 200:
                # Keep workspace info (always first injection), trim the rest
                ws_end = injections.find('\n\n## ', injections.find('## Current') + 10 if '## Current' in injections else 20)
                if ws_end > 0:
                    ws_part = injections[:ws_end]
                    rest = injections[ws_end:]
                    rest_tokens = _estimate_tokens(rest)
                    if rest_tokens > budget_left - _estimate_tokens(ws_part):
                        rest = rest[:int((budget_left - _estimate_tokens(ws_part)) * 4)] + '\n[... trimmed due to token budget ...]\n'
                    sys_prompt = base + ws_part + rest
                else:
                    sys_prompt = base + injections[:int(budget_left * 4)] + '\n[... trimmed due to token budget ...]\n'
        else:
            sys_prompt = sys_prompt[:int(max_tokens * 4)] + '\n[... system prompt truncated due to token budget ...]\n'
        estimated = _estimate_tokens(sys_prompt)

    log_write(f'[muside] System prompt trimmed to ~{estimated} tokens (budget: {max_tokens})')
    return sys_prompt


def _get_system_prompt_cache_key(llm_config):
    """Build a cache key from workspace state and LLM config."""
    try:
        from utils import load_config
        config = load_config()
        ws = config.get('workspace', WORKSPACE)
        prj = config.get('project', '')
        # Include key parts that affect the system prompt
        raw = f'{ws}|{prj}|{SERVER_DIR}'
        # Include AST index state
        raw += f'|ast:{project_index.file_count}:{project_index.symbol_count}:{project_index.last_index_time}'
        # Include custom prompt
        raw += f'|{llm_config.get("system_prompt", "")}'
        return hashlib.md5(raw.encode()).hexdigest()
    except Exception:
        return 'error'


# ==================== LLM Integration ====================

def _build_cached_api_messages(static_prompt, dynamic_prompt, llm_config):
    """Build system message(s) with provider-level prompt caching support.

    Splits the system prompt into static (tool docs, rarely changes) and dynamic
    (workspace info, AST symbols, env — changes per request) parts, and applies
    the appropriate provider-specific caching strategy:

    - Anthropic: content blocks with cache_control ephemeral on static part
    - OpenAI: system role (compatible with all OpenAI-compatible APIs)
    - Others: single system message, no provider-level caching
    """
    provider = llm_config.get('provider', '')
    api_type = llm_config.get('api_type', '')
    _is_anthropic = (provider == 'anthropic' or api_type == 'anthropic')
    _is_openai = (provider == 'openai' or api_type == 'openai') and not _is_anthropic

    full_prompt = static_prompt + dynamic_prompt

    if _is_anthropic:
        sys_content = [
            {'type': 'text', 'text': static_prompt, 'cache_control': {'type': 'ephemeral'}},
            {'type': 'text', 'text': dynamic_prompt},
        ]
        return [{'role': 'system', 'content': sys_content}]
    elif _is_openai:
        return [{'role': 'system', 'content': full_prompt}]
    else:
        return [{'role': 'system', 'content': full_prompt}]


def _build_api_messages(messages, llm_config, skip_system_inject=False):
    """Convert chat history to API format with system prompt.

    Args:
        messages: Chat history list of {role, content, ...} dicts.
        llm_config: LLM configuration dict.
        skip_system_inject: If True, do NOT build DEFAULT_SYSTEM_PROMPT or inject
            .muside/ knowledge / AST index. Instead, use the first system message
            from `messages` as-is. This is used by sub-agents which have their own
            concise system prompt.
    """

    if skip_system_inject:
        # Sub-agent mode: use the system prompt from messages as-is (no injection)
        api_messages = []
        for msg in messages:
            role = msg.get('role', '')
            # Normalize 'developer' role to 'system' (OpenAI o1/o3 models use 'developer',
            # but many API providers like ModelScope don't support it)
            if role == 'developer':
                role = 'system'
            if role == 'system':
                api_messages.append({'role': 'system', 'content': msg.get('content', '')})
            elif role == 'tool':
                api_messages.append({
                    'role': 'tool',
                    'tool_call_id': msg.get('tool_call_id', 'call_default'),
                    'content': msg.get('content', ''),
                })
            elif role == 'assistant' and msg.get('tool_calls'):
                api_messages.append({
                    'role': 'assistant',
                    'content': msg.get('content', None),
                    'tool_calls': msg['tool_calls'],
                })
            elif role in ('user', 'assistant'):
                api_messages.append({'role': role, 'content': msg.get('content', '')})
        return api_messages

    # ── Main agent mode: build full system prompt with injections ──
    # P2-6: Check cache first (60s TTL) — fast path for repeated calls
    _sp_cache_key = _get_system_prompt_cache_key(llm_config)
    _sp_now = time.time()
    # Always touch _load_system_prompt_template() so hot-reload mtime check runs
    # (if file changed, it will clear _SYSTEM_PROMPT_CACHE and force a cache miss below)
    _fresh_static_prompt = _load_system_prompt_template()
    _sp_cached = _SYSTEM_PROMPT_CACHE.get(_sp_cache_key)
    if _sp_cached and (_sp_now - _sp_cached['time'] < _SYSTEM_PROMPT_CACHE_TTL):
        _static_sys_prompt = _sp_cached.get('static', _sp_cached['prompt'])
        _dynamic_sys_prompt = _sp_cached.get('dynamic', '')
        if not _dynamic_sys_prompt:
            # Legacy cache entry: whole prompt as static
            sys_prompt = _sp_cached['prompt']
            _static_sys_prompt = sys_prompt
            _dynamic_sys_prompt = ''
        log_write(f'[muside] Using cached system prompt (~{_sp_cached["tokens"]} tokens, age {_sp_now - _sp_cached["time"]:.0f}s)')
        # Use same provider-aware message building as cache miss path
        api_messages = _build_cached_api_messages(_static_sys_prompt, _dynamic_sys_prompt, llm_config)
        for msg in messages:
            role = msg.get('role', '')
            # Normalize 'developer' role to 'system' (OpenAI o1/o3 models use 'developer',
            # but many API providers like ModelScope don't support it)
            if role == 'developer':
                role = 'system'
            if role == 'system':
                continue
            elif role == 'tool':
                api_messages.append({
                    'role': 'tool',
                    'tool_call_id': msg.get('tool_call_id', 'call_default'),
                    'content': msg.get('content', ''),
                })
            elif role == 'assistant' and msg.get('tool_calls'):
                api_messages.append({
                    'role': 'assistant',
                    'content': msg.get('content', None),
                    'tool_calls': msg['tool_calls'],
                })
            elif role in ('user', 'assistant'):
                api_messages.append({'role': role, 'content': msg.get('content', '')})
        return api_messages

    # Cache miss — build system prompt from scratch
    # Split into static (tool docs) and dynamic (workspace/env) parts
    # for provider-level prompt caching (Anthropic cache_control, OpenAI cache breakpoints)
    _static_sys_prompt = _load_system_prompt_template()  # hot-reload from system_prompt.txt
    _dynamic_sys_prompt = ''  # workspace, env, AST — changes per request

    custom_prompt = llm_config.get('system_prompt', '').strip()
    if custom_prompt and custom_prompt != _load_system_prompt_template().strip():
        _static_sys_prompt += '\n\n## Additional Instructions from User\n' + custom_prompt

    # Inject project-aware workspace info and system environment
    # Pre-initialize fallback values in case config loading fails
    _ws = WORKSPACE
    _project = None
    _project_dir = os.path.realpath(WORKSPACE)

    try:
        from utils import load_config, get_system_info, IS_WINDOWS, get_default_shell, get_default_compiler
        config = load_config()
        _project = config.get('project', None)
        _ws = config.get('workspace', WORKSPACE)

        # Determine effective project directory (always defined, no scope issues)
        if _project:
            candidate = os.path.realpath(os.path.join(_ws, _project))
            if os.path.isdir(candidate):
                _project_dir = candidate
            else:
                _project_dir = os.path.realpath(_ws)
        else:
            _project_dir = os.path.realpath(_ws)

        # System environment info
        sys_env_info = f'## System Environment\n{get_system_info()}\nDefault shell: {get_default_shell()}\nDefault Python: {get_default_compiler()}\n'
        if IS_WINDOWS:
            sys_env_info += 'Note: This is a Windows system. Use Windows-compatible commands (cmd.exe/PowerShell). Use backslashes for paths in shell commands, forward slashes for file operations in code.\n'

        # Only inject project directory info (not workspace root or server dir)
        if _project and os.path.isdir(os.path.join(_ws, _project)):
            workspace_info = (
                f'## Current Project\n'
                f'- Project name: {_project}\n'
                f'- Project directory (absolute): {_project_dir}\n'
                f'- All file operations should be scoped to the project directory: {_project_dir}'
            )
        else:
            workspace_info = (
                f'## Current Project\n'
                f'- Project directory (absolute): {_project_dir}'
            )
    except Exception as e:
        log_write(f'[muside] Error loading workspace config: {e}')
        sys_env_info = '## System Environment\nOS: Unknown\n'
        workspace_info = (
            f'## Current Project\n'
            f'- Project directory (absolute): {_project_dir}'
        )

    # Accumulate dynamic parts (workspace, env — changes per request)
    _dynamic_sys_prompt += f'\n\n{sys_env_info}\n\n{workspace_info}\n'

    # Inject project knowledge from .muside/ directory (like CLAUDE.md)
    # Semi-static: cached 30s, treated as dynamic for provider cache
    _knowledge_loaded = []  # track which files were loaded (for logging/SSE)
    _muside_dirs_to_check = []
    # Only check project directory for .muside/ knowledge (not workspace root or server dir)
    if _project_dir:
        _muside_dirs_to_check.append(_project_dir)

    # Check cache first (30s TTL)
    _now = time.time()
    _cache_key = tuple(_muside_dirs_to_check)
    _muside_cache = _get_muside_cache()
    if (_cache_key in _muside_cache and
            _now - _muside_cache[_cache_key]['time'] < 30):
        _dynamic_sys_prompt += _muside_cache[_cache_key]['content']
        _knowledge_loaded = _muside_cache[_cache_key]['files']
        log_write(f'[muside] Using cached .muside/ content ({len(_knowledge_loaded)} files)')
    else:
        for _check_dir in _muside_dirs_to_check:
            _muside_dir = os.path.join(_check_dir, '.muside')
            if os.path.isdir(_muside_dir):
                log_write(f'[muside] Found .muside/ at: {_muside_dir}')
                knowledge_files = [
                    ('rules.md', 'Project Rules & Guidelines'),
                    ('architecture.md', 'Project Architecture'),
                    ('conventions.md', 'Coding Conventions'),
                ]
                knowledge_parts = []
                for fname, title in knowledge_files:
                    fpath = os.path.join(_muside_dir, fname)
                    if os.path.isfile(fpath):
                        try:
                            with open(fpath, 'r', encoding='utf-8', errors='ignore') as f:
                                content = f.read().strip()
                            if content:
                                knowledge_parts.append(f'### {title}\n{content}')
                                _knowledge_loaded.append(fname)
                                log_write(f'[muside] Loaded {fname} ({len(content)} chars)')
                        except Exception as e:
                            log_write(f'[muside] Error reading {fpath}: {e}')
                if knowledge_parts:
                    _injected = '\n\n## Project Knowledge (from .muside/)\n'
                    _injected += 'The following project-specific context was loaded from .muside/ files. '
                    _injected += 'Use this information to follow project conventions and understand the architecture.\n\n'
                    _injected += '\n\n'.join(knowledge_parts) + '\n'
                    # Truncate to ~4000 chars to prevent system prompt bloat
                    if len(_injected) > 4000:
                        _injected = _injected[:4000] + '\n\n[... .muside/ content truncated — use read_file for full details ...]\n'
                        log_write(f'[muside] .muside/ content truncated to 4000 chars')
                    _dynamic_sys_prompt += _injected
                    # Store in cache
                    _muside_cache[_cache_key] = {
                        'content': _injected,
                        'files': list(_knowledge_loaded),
                        'time': time.time(),
                    }
                break  # Use the first .muside/ directory found

        if not _knowledge_loaded:
            log_write(f'[muside] No .muside/ found in: {_muside_dirs_to_check}')

    # Inject AST index summary if available (re-inject when index changes)
    try:
        _ast_cache_key = '_ast_injected_time'
        _last_injected_time = _get_muside_cache().get(_ast_cache_key, 0)
        if (not project_index.is_indexing and project_index.symbol_count > 0
                and project_index.last_index_time > _last_injected_time):
            symbols = project_index.get_all_symbols()
            # Show top-level symbols (no parent = module-level)
            top_symbols = {}
            for name, entries in symbols.items():
                for fp, d in entries:
                    if not d.get('parent'):
                        rel = os.path.relpath(fp, _ws)
                        top_symbols.setdefault(name, []).append((rel, d['kind'], d['line']))
            # Build compact summary — limit to 30 symbols to save tokens
            symbol_lines = []
            for name in sorted(top_symbols.keys())[:30]:
                locs = top_symbols[name]
                if len(locs) <= 3:
                    for rel, kind, line in locs:
                        symbol_lines.append(f'  {kind} {name} ({rel}:{line})')
                else:
                    symbol_lines.append(f'  {name} ({len(locs)} definitions)')
            if symbol_lines:
                _dynamic_sys_prompt += f'\n\n## Project Symbols ({project_index.file_count} files, {project_index.symbol_count} symbols)\n'
                _dynamic_sys_prompt += 'Use find_definition/find_references for detailed lookup.\n'
                _dynamic_sys_prompt += '\n'.join(symbol_lines) + '\n'
                if project_index.symbol_count > 30:
                    _dynamic_sys_prompt += f'  ... and {project_index.symbol_count - 30} more symbols (use find_definition to look up)\n'
                log_write(f'[muside] AST index injected: {project_index.file_count} files, {project_index.symbol_count} symbols')
                _get_muside_cache()[_ast_cache_key] = project_index.last_index_time
    except Exception as e:
        log_write(f'[muside] AST index injection error: {e}')

    # Merge for local cache and token estimation
    sys_prompt = _static_sys_prompt + _dynamic_sys_prompt

    # P2-1: Trim system prompt to token budget (trim dynamic part preferentially)
    sys_prompt = _trim_system_prompt_to_budget(sys_prompt)
    # Re-split after trimming in case dynamic was cut
    _dynamic_sys_prompt = sys_prompt[len(_static_sys_prompt):]

    # P2-6: Store in cache for subsequent calls (60s TTL)
    _sp_tokens = _estimate_tokens(sys_prompt)
    _SYSTEM_PROMPT_CACHE[_sp_cache_key] = {
        'prompt': sys_prompt,
        'static': _static_sys_prompt,
        'dynamic': _dynamic_sys_prompt,
        'tokens': _sp_tokens,
        'time': time.time(),
    }
    # Evict old entries (keep cache size bounded)
    if len(_SYSTEM_PROMPT_CACHE) > 10:
        oldest_key = min(_SYSTEM_PROMPT_CACHE, key=lambda k: _SYSTEM_PROMPT_CACHE[k]['time'])
        del _SYSTEM_PROMPT_CACHE[oldest_key]
    log_write(f'[muside] System prompt built and cached (~{_sp_tokens} tokens)')

    # Build api_messages with provider-level prompt caching support
    api_messages = _build_cached_api_messages(_static_sys_prompt, _dynamic_sys_prompt, llm_config)
    provider = llm_config.get('provider', '')
    api_type = llm_config.get('api_type', '')
    if provider == 'anthropic' or api_type == 'anthropic':
        log_write(f'[muside] Anthropic cache_control enabled (static: ~{_estimate_tokens(_static_sys_prompt)} tokens)')
    elif provider == 'openai' or api_type == 'openai':
        log_write(f'[muside] OpenAI system message mode (static: ~{_estimate_tokens(_static_sys_prompt)} tokens)')

    for msg in messages:
        role = msg.get('role', '')
        # Normalize 'developer' role to 'system' (OpenAI o1/o3 models use 'developer',
        # but many API providers like ModelScope don't support it)
        if role == 'developer':
            role = 'system'
        if role == 'system':
            continue
        elif role == 'tool':
            api_messages.append({
                'role': 'tool',
                'tool_call_id': msg.get('tool_call_id', 'call_default'),
                'content': msg.get('content', ''),
            })
        elif role == 'assistant' and msg.get('tool_calls'):
            api_messages.append({
                'role': 'assistant',
                'content': msg.get('content', None),
                'tool_calls': msg['tool_calls'],
            })
        elif role in ('user', 'assistant'):
            api_messages.append({'role': role, 'content': msg.get('content', '')})
    return api_messages

def _get_llm_endpoint(llm_config, model=None):
    """Build URL and headers for an LLM API call based on api_type.

    Returns (url, headers) tuple.
    """
    api_key = llm_config.get('api_key', '')
    api_type = llm_config.get('api_type', 'openai')
    api_base = (llm_config.get('api_base') or '').rstrip('/')
    model = model or llm_config.get('model', 'gpt-4o-mini')

    headers = {'Content-Type': 'application/json; charset=utf-8'}

    if api_type == 'ollama':
        # Ollama local server — no auth needed
        if not api_base:
            api_base = 'http://localhost:11434'
        # Ollama uses /api/chat (not /v1/chat/completions)
        url = api_base + '/api/chat'
    elif api_type == 'azure':
        if not api_base:
            raise Exception('Azure OpenAI: API base URL is required (e.g. https://xxx.openai.azure.com)')
        url = api_base + f'/openai/deployments/{model}/chat/completions?api-version=2024-02-01'
        if api_key:
            headers['Authorization'] = f'Bearer {api_key}'
    else:
        # openai / custom — OpenAI-compatible format
        if not api_base:
            api_base = 'https://api.openai.com/v1'
        # Remove trailing slash to avoid 307 redirects from some providers (e.g. ModelScope)
        if api_base.endswith('/v1'):
            url = api_base.rstrip('/') + '/chat/completions'
        else:
            url = api_base.rstrip('/') + '/v1/chat/completions'
        if api_key:
            headers['Authorization'] = f'Bearer {api_key}'

    return url, headers


def _call_llm_api(messages, llm_config, stream=False):
    """Make a non-streaming LLM API call. Returns parsed response dict."""
    model = llm_config.get('model', 'gpt-4o-mini')
    temperature = llm_config.get('temperature', 0.7)
    max_tokens = llm_config.get('max_tokens', 4096)

    api_messages = _build_api_messages(messages, llm_config)

    payload = {
        'model': model,
        'messages': api_messages,
        'temperature': temperature,
        'max_tokens': max_tokens,
        'tools': AGENT_TOOLS,
        'tool_choice': 'auto',
    }
    if stream:
        payload['stream'] = True

    try:
        url, headers = _get_llm_endpoint(llm_config, model)
    except Exception as e:
        raise Exception(f'LLM config error: {e}')

    headers = headers or {}
    headers.setdefault('Content-Type', 'application/json; charset=utf-8')

    body_bytes = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')

    # Store last payload for debugging
    _last_llm_payload_debug['body'] = body_bytes
    _last_llm_payload_debug['payload'] = payload
    _last_llm_payload_debug['size'] = len(body_bytes)
    _last_llm_payload_debug['url'] = url
    _last_llm_payload_debug['timestamp'] = time.time()

    req = urllib.request.Request(url, body_bytes, headers=headers, method='POST')

    with _urllib_opener.open(req, timeout=180) as resp:
        resp_body = resp.read().decode()
        try:
            result = json.loads(resp_body)
        except (json.JSONDecodeError, ValueError) as je:
            raise Exception(f'Invalid JSON response from LLM API: {str(je)}')
    return result

def _rewrite_for_reasoning_model(payload, api_messages):
    """Rewrite messages for OpenAI reasoning models which don't support system messages natively.
    Moves system prompt content into the first user message."""
    system_msgs = []
    other_msgs = []
    for m in api_messages:
        if m.get('role') == 'system':
            system_msgs.append(m.get('content', ''))
        else:
            other_msgs.append(m)

    if system_msgs:
        system_text = '\n\n'.join(system_msgs)
        if other_msgs and other_msgs[0].get('role') == 'user':
            other_msgs[0]['content'] = f"[System Instructions]\n{system_text}\n\n[User Message]\n{other_msgs[0].get('content', '')}"
        else:
            other_msgs.insert(0, {'role': 'user', 'content': f"[System Instructions]\n{system_text}"})

    payload['messages'] = other_msgs


def _call_llm_stream_raw(messages, llm_config, tools_level='full'):
    """Stream LLM response as raw SSE data chunks. Yields parsed delta objects.
    
    tools_level: 'full' = all 42 tools with full descriptions
                 'compact' = all 42 tools with shortened descriptions
                 'minimal' = all 42 tools with minimal descriptions, no param details
    """
    import urllib.request

    model = llm_config.get('model', 'gpt-4o-mini')
    temperature = llm_config.get('temperature', 0.7)
    max_tokens = llm_config.get('max_tokens', 4096)
    reasoning = llm_config.get('reasoning', True)

    api_messages = _build_api_messages(messages, llm_config)

    # Select tool set based on level (for ModelScope API size limit compatibility)
    if tools_level == 'minimal':
        tools = AGENT_TOOLS_MINIMAL
    elif tools_level == 'compact':
        tools = AGENT_TOOLS_COMPACT
    else:
        tools = AGENT_TOOLS

    payload = {
        'model': model,
        'messages': api_messages,
        'temperature': temperature,
        'max_tokens': max_tokens,
        'tools': tools,
        'tool_choice': 'auto',
        'stream': True,
    }

    # Add reasoning/thinking support for providers that support it
    if reasoning:
        provider = llm_config.get('provider', '')
        api_type = llm_config.get('api_type', '')
        model_lower = model.lower()

        # OpenAI reasoning models (o1, o3, o4-mini, codex, etc.)
        if ('o1' in model_lower or 'o3' in model_lower or 'o4' in model_lower or
            'codex' in model_lower):
            payload['reasoning_effort'] = 'high'
            # OpenAI reasoning models don't support temperature and system messages in the usual way
            payload.pop('temperature', None)
            # Move system messages to the first user message for reasoning models
            _rewrite_for_reasoning_model(payload, api_messages)

        # Anthropic extended thinking (Claude 3.5+)
        elif provider == 'anthropic' or api_type == 'anthropic':
            if 'claude-3-5' in model_lower or 'claude-sonnet-4' in model_lower or 'claude-opus-4' in model_lower:
                payload['thinking'] = {
                    'type': 'enabled',
                    'budget_tokens': min(max_tokens * 4, 10000),
                }
                # Claude thinking requires temperature=1
                payload['temperature'] = 1

        # DeepSeek reasoning models (R1, etc.)
        elif 'deepseek' in model_lower or 'reasoner' in model_lower:
            payload.setdefault('temperature', 0.6)

        # Step (深度求索) reasoning models
        elif 'step' in model_lower:
            payload.setdefault('temperature', 0.6)

        # GLM (Z.ai) reasoning models
        elif 'glm' in model_lower:
            payload.setdefault('temperature', 0.6)

        # QwQ / Kimi / other reasoning models
        elif 'qwq' in model_lower or 'kimi' in model_lower or 'think' in model_lower or 'reasoning' in model_lower:
            pass  # These models reason by default, no special params needed

    try:
        url, headers = _get_llm_endpoint(llm_config, model)
    except Exception as e:
        raise Exception(f'LLM config error: {e}')

    headers = headers or {}
    headers.setdefault('Content-Type', 'application/json; charset=utf-8')

    # Use compact JSON encoding (no extra whitespace) and ensure_ascii=False
    # to reduce payload size and avoid potential server-side parsing issues
    # with \\uXXXX escape sequences (some ModelScope/vLLM backends struggle)
    body_bytes = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    print(f'[LLM] Calling: {url}')
    print(f'[LLM] Payload size: {len(body_bytes)} bytes ({len(body_bytes)/1024:.1f} KB)')

    # Store last payload for debugging (accessible when errors occur)
    _last_llm_payload_debug['body'] = body_bytes
    _last_llm_payload_debug['payload'] = payload
    _last_llm_payload_debug['size'] = len(body_bytes)
    _last_llm_payload_debug['url'] = url
    _last_llm_payload_debug['timestamp'] = time.time()

    req = urllib.request.Request(url, body_bytes, headers=headers, method='POST')
    print(f'[LLM] Model: {model}, Temperature: {temperature}, MaxTokens: {max_tokens}, Reasoning: {reasoning}')
    if reasoning:
        # Log which reasoning branch was matched
        model_lower = model.lower()
        provider = llm_config.get('provider', '')
        if 'o1' in model_lower or 'o3' in model_lower or 'o4' in model_lower or 'codex' in model_lower:
            print(f'[LLM] Reasoning branch: OpenAI (reasoning_effort=high)')
        elif provider == 'anthropic' or 'anthropic' in llm_config.get('api_type', ''):
            print(f'[LLM] Reasoning branch: Anthropic thinking')
        elif 'deepseek' in model_lower or 'reasoner' in model_lower:
            print(f'[LLM] Reasoning branch: DeepSeek (temp=0.6)')
        elif 'step' in model_lower:
            print(f'[LLM] Reasoning branch: Step (temp=0.6)')
        elif 'glm' in model_lower:
            print(f'[LLM] Reasoning branch: GLM (temp=0.6)')
        elif 'qwq' in model_lower or 'kimi' in model_lower or 'think' in model_lower or 'reasoning' in model_lower:
            print(f'[LLM] Reasoning branch: Generic (no special params)')
        else:
            print(f'[LLM] Reasoning enabled but no model matched — model="{model}"')
    print(f'[LLM] Headers: {dict((k, v[:20]+"..." if len(v)>20 else v) for k,v in headers.items())}')
    print(f'[LLM] Messages count: {len(api_messages)}')

    with _urllib_opener.open(req, timeout=300) as resp:
        byte_buffer = b''
        accumulated_finish_reason = None
        raw_sse_lines = []  # Capture raw SSE lines for debugging empty responses
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            byte_buffer += chunk
            while b'\n' in byte_buffer:
                line_bytes, byte_buffer = byte_buffer.split(b'\n', 1)
                try:
                    line = line_bytes.decode('utf-8').strip()
                except UnicodeDecodeError:
                    continue
                if not line.startswith('data: '):
                    continue
                data_str = line[6:]
                if data_str == '[DONE]':
                    # Store raw SSE data for debugging before returning
                    _last_llm_payload_debug['raw_sse_count'] = len(raw_sse_lines)
                    _last_llm_payload_debug['finish_reason'] = accumulated_finish_reason
                    _last_llm_payload_debug['raw_sse_tail'] = raw_sse_lines[-5:] if len(raw_sse_lines) > 5 else raw_sse_lines
                    return
                try:
                    data = json.loads(data_str)
                    # Store raw data for debugging (keep last 10 chunks)
                    raw_sse_lines.append(data_str[:500])
                    if len(raw_sse_lines) > 10:
                        raw_sse_lines = raw_sse_lines[-10:]
                    choices = data.get('choices', [])
                    if choices:
                        delta = choices[0].get('delta', {})
                        fr = choices[0].get('finish_reason')
                        if fr:
                            delta['_finish_reason'] = fr
                            accumulated_finish_reason = fr
                        # Pass through reasoning_content for DeepSeek/QwQ/Step/GLM/Kimi/reasoning models
                        # The delta dict may contain 'reasoning_content' field
                        if 'reasoning_content' in delta:
                            delta['_reasoning'] = True
                        yield delta
                except json.JSONDecodeError:
                    continue
        # Process any remaining partial data in buffer
        if byte_buffer.strip():
            line = byte_buffer.decode('utf-8', errors='replace').strip()
            if line.startswith('data: ') and line[6:] != '[DONE]':
                try:
                    data = json.loads(line[6:])
                    choices = data.get('choices', [])
                    if choices:
                        delta = choices[0].get('delta', {})
                        fr = choices[0].get('finish_reason')
                        if fr:
                            delta['_finish_reason'] = fr
                        if 'reasoning_content' in delta:
                            delta['_reasoning'] = True
                        yield delta
                except (json.JSONDecodeError, KeyError):
                    pass
        # Store raw SSE data for debugging (stream ended without [DONE])
        _last_llm_payload_debug['raw_sse_count'] = len(raw_sse_lines)
        _last_llm_payload_debug['finish_reason'] = accumulated_finish_reason
        _last_llm_payload_debug['raw_sse_tail'] = raw_sse_lines[-5:] if len(raw_sse_lines) > 5 else raw_sse_lines

# ==================== Context Window Management ====================
def _estimate_tokens(text):
    """Estimate token count. Uses tiktoken if available, otherwise heuristic.
    
    Chinese/CJK ~1.5 tokens per character, Latin ~0.25 tokens per character.
    Fallback is more accurate than len//4 for mixed-language content.
    """
    if not text:
        return 0
    try:
        import tiktoken
        _enc = tiktoken.get_encoding("cl100k_base")
        return len(_enc.encode(text))
    except Exception:
        pass
    # Heuristic: CJK chars cost ~1.5 tokens, other chars ~0.25 tokens
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3400' <= c <= '\u4dbf')
    return int(cjk * 1.5 + (len(text) - cjk) * 0.25)


def _get_context_budget(llm_config):
    """Get the context window budget for compression.
    
    Uses the model's max_context (input context window size) minus safety margins,
    falling back to max_tokens * 10 if max_context is not configured.
    """
    max_context = llm_config.get('max_context', 0)
    if max_context > 0:
        max_output = llm_config.get('max_tokens', 4096)
        return max(max_context - max_output - 4000, 8000)  # safety margin + minimum floor
    return llm_config.get('max_tokens', 4096) * 10


# ==================== AI-Powered History Summarization ====================
def _ai_summarize_messages(messages, llm_config):
    """Use the LLM to generate a concise summary of conversation messages.
    
    P2-2: Supports incremental summarization — if an existing summary is found
    in the messages, it's used as a base context and only new messages since
    the summary are summarized and merged. This avoids re-summarizing the
    entire conversation each time.
    
    Returns summary text string, or None on failure.
    This is used by _compress_context when llm_config is provided.
    """
    if not messages or not llm_config or len(messages) < 3:
        return None
    
    # P2-2: Find existing summary to use as base context
    existing_summary = None
    summary_end_idx = 0
    for i, msg in enumerate(messages):
        role = msg.get('role', '')
        content = (msg.get('content') or '')
        if role == 'user' and (content.startswith('[Previous Conversation Summary]') or
                                content.startswith('[Conversation Summary]') or
                                content.startswith('Earlier conversation summary')):
            # Extract the summary text (after the prefix line)
            for prefix in ('[Previous Conversation Summary]\n', '[Conversation Summary]\n', 'Earlier conversation summary:\n'):
                if content.startswith(prefix):
                    existing_summary = content[len(prefix):].strip()
                    break
            else:
                existing_summary = content.strip()
            summary_end_idx = i + 1
            break
    
    # Only summarize messages after the existing summary (incremental)
    if existing_summary and summary_end_idx < len(messages) - 2:
        messages_to_summarize = messages[summary_end_idx:]
        if len(messages_to_summarize) < 3:
            # Not enough new messages to justify re-summarization
            return existing_summary
    elif existing_summary:
        # Existing summary covers all messages — return as-is
        return existing_summary
    else:
        messages_to_summarize = messages
    
    # Build compact representation of messages for summarization
    compact = []
    for msg in messages_to_summarize:
        role = msg.get('role', '')
        content = (msg.get('content') or '')
        name = msg.get('name', '')
        
        # Skip existing summary messages to avoid re-summarizing summaries
        if role == 'user' and (content.startswith('[Previous Conversation Summary]') or
                                content.startswith('[Conversation Summary]') or
                                content.startswith('Earlier conversation summary')):
            continue
        
        if role == 'user':
            compact.append(f'[User]: {content[:400]}')
        elif role == 'assistant':
            tool_calls = msg.get('tool_calls')
            if tool_calls:
                tools = ', '.join(t.get('function', {}).get('name', '') for t in tool_calls)
                text_part = f' "{content[:200]}"' if content else ''
                compact.append(f'[Assistant]: Called [{tools}]{text_part}')
            else:
                compact.append(f'[Assistant]: {content[:300]}')
        elif role == 'tool':
            compact.append(f'[Tool/{name}]: {content[:200]}')
    
    if not compact:
        return existing_summary
    
    conversation_text = '\n'.join(compact)
    
    # Build prompt based on whether we have an existing summary
    if existing_summary:
        summary_prompt = (
            "You are updating a conversation summary. Below is the existing summary followed by "
            "NEW conversation that happened after it. Update the summary to incorporate the new information.\n\n"
            "Focus on:\n"
            "1. What the user asked for (goals and requirements)\n"
            "2. Key files modified (full file paths and what was changed)\n"
            "3. Important commands run and their results\n"
            "4. Errors encountered and how they were resolved\n"
            "5. Current state of work (what is done, what remains)\n\n"
            "Preserve all file paths, code snippets, function names, and technical details. "
            "Be concise but complete. Keep relevant older context and add new information.\n\n"
            f"=== EXISTING SUMMARY ===\n{existing_summary}\n\n"
            f"=== NEW CONVERSATION (to incorporate) ===\n{conversation_text}\n\n"
            "Provide the UPDATED summary:"
        )
    else:
        summary_prompt = (
            "Summarize the following conversation between a user and an AI coding assistant. "
            "Focus on:\n"
            "1. What the user asked for (goals and requirements)\n"
            "2. Key files modified (full file paths and what was changed)\n"
            "3. Important commands run and their results\n"
            "4. Errors encountered and how they were resolved\n"
            "5. Current state of work (what is done, what remains)\n\n"
            "Preserve all file paths, code snippets, function names, and technical details. "
            "Be concise but complete. This summary will replace the original conversation.\n\n"
            f"Conversation to summarize:\n{conversation_text}"
        )
    
    try:
        url, headers = _get_llm_endpoint(llm_config, llm_config.get('model'))
        headers = headers or {'Content-Type': 'application/json; charset=utf-8'}
        
        payload = {
            'model': llm_config.get('model'),
            'messages': [{'role': 'user', 'content': summary_prompt}],
            'temperature': 0.3,  # Low temperature for factual summarization
            'max_tokens': min(2000, llm_config.get('max_tokens', 4096)),
        }
        
        body_bytes = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        req = urllib.request.Request(url, body_bytes, headers=headers, method='POST')
        with _urllib_opener.open(req, timeout=60) as resp:
            resp_body = resp.read().decode()
            result = json.loads(resp_body)
            summary = result.get('choices', [{}])[0].get('message', {}).get('content', '').strip()
            if summary:
                mode = 'incremental' if existing_summary else 'full'
                print(f'[AI-SUMMARY] Generated {len(summary)} char {mode} summary from {len(messages_to_summarize)} messages')
            return summary
    except Exception as e:
        print(f'[AI-SUMMARY] Failed to generate summary: {e}')
        return existing_summary  # Return existing summary on failure instead of None


# ==================== Self-Correction Loop ====================
MAX_SELF_CORRECTION_RETRIES = 3

# Tools that should trigger self-correction on failure
_SELF_CORRECTION_TOOLS = frozenset({
    'write_file', 'edit_file', 'run_command', 'install_package',
})

# Error patterns to detect in tool results
_ERROR_PATTERNS = [
    'error:', 'error：', 'traceback (most recent call last)',
    'syntaxerror', 'typeerror', 'valueerror', 'nameerror',
    'importerror', 'modulenotfounderror', 'filenotfounderror',
    'keyerror', 'attributeerror', 'indexerror', 'runtimeerror',
    'permission denied', 'no such file or directory',
    'command not found', 'returned non-zero', 'non-zero exit', 'exit code',
    'segmentation fault', 'connectionrefusederror', 'connectionerror',
    'oserror', 'json.decoder.jsondecodeerror',
]


def _is_tool_result_error(tool_name, result_str):
    """Check if a tool result indicates a failure that should trigger self-correction."""
    if tool_name not in _SELF_CORRECTION_TOOLS:
        return False
    if not result_str:
        return False
    
    # Skip successful-looking results
    result_lower = result_str[:800].lower()
    
    # If result starts with 'Error' or 'error', it's definitely an error
    if result_lower.startswith('error'):
        return True
    
    # Check for error patterns
    for pattern in _ERROR_PATTERNS:
        if pattern in result_lower:
            return True
    
    return False


def _build_self_correction_hint(failed_tools):
    """Build a hint message for the LLM when self-correction is needed.
    
    Args:
        failed_tools: list of (tool_name, args, result_str) tuples
    """
    hint = "[Self-Correction Required] The following tool calls encountered errors:\n\n"
    for name, args, result in failed_tools:
        args_str = json.dumps(args, ensure_ascii=False)[:300] if args else 'N/A'
        # Extract the most relevant error info from the result
        error_excerpt = result[:500]
        hint += f"**{name}** (args: {args_str}):\n"
        hint += f"Error output: {error_excerpt}\n\n"
    
    hint += (
        "Please analyze these errors and retry with a corrected approach:\n"
        "1. Read the relevant file(s) to understand the current state before fixing\n"
        "2. Identify the root cause from the error message\n"
        "3. Apply a corrected approach (different edit, different command, fix imports, etc.)\n"
        "4. Re-run to verify the fix works\n"
    )
    return hint


def _has_tool_calls(msg):
    """Check if a message has tool calls."""
    return bool(msg and msg.get('tool_calls'))


def _check_self_correction(context, batch_results, self_corrections):
    """P2-5: Shared self-correction check used by both agent loops.

    Checks if any tool results indicate failures and, if so, builds a
    correction hint and appends it to context.

    Args:
        context: The conversation context list (modified in-place).
        batch_results: List of (tool_name, args, ok, result_str) tuples.
        self_corrections: Current self-correction counter.

    Returns:
        (updated_self_corrections, hint_or_None) tuple.
        If hint is not None, it has already been appended to context.
    """
    if self_corrections >= MAX_SELF_CORRECTION_RETRIES:
        return self_corrections, None

    failed = [(n, a, r) for n, a, ok, r in batch_results
              if not ok or _is_tool_result_error(n, r)]
    if not failed:
        return self_corrections, None

    self_corrections += 1
    hint = _build_self_correction_hint(failed)
    context.append({'role': 'user', 'content': hint, 'time': datetime.now().isoformat()})

    print(f'[SELF-CORRECT] #{self_corrections}: {len(failed)} tool(s) failed')
    return self_corrections, hint

def _compress_context(messages, max_tokens=None, llm_config=None):
    """Smart context compression with AI summarization and code-change preservation.
    
    Strategy:
    1. Preserve write_file/edit_file results as "KEY CODE CHANGES" 
    2. AI-powered summarization of older messages (when llm_config is provided)
    3. Differentiated compression limits by tool type
    4. Two-stage compression (gentle → aggressive)
    5. Informative size markers instead of silent truncation
    
    Args:
        messages: List of chat messages.
        max_tokens: Maximum token budget for the compressed context.
        llm_config: If provided, uses AI summarization for older messages.
                  Only effective when there are enough older messages to summarize.
    
    Returns (messages, was_compressed) tuple.
    """
    if not messages:
        return messages, False
    max_tokens = max_tokens or 60000
    total = sum(_estimate_tokens(m.get('content', '') or '') for m in messages)
    if total <= max_tokens:
        return messages, False

    was_compressed = True
    original_total = total

    # ── Stage 1: Extract key code changes (write_file/edit_file results) ──
    code_changes = []
    for msg in messages:
        if msg.get('role') == 'tool' and msg.get('name') in ('write_file', 'edit_file'):
            content = msg.get('content', '') or ''
            if content and 'Error' not in content[:20]:
                code_changes.append(content[:500])

    # ── Stage 2: Split into older/recent ──
    user_indices = [i for i, m in enumerate(messages) if m.get('role') == 'user']
    if len(user_indices) >= 2:
        split_idx = user_indices[-2]
    else:
        split_idx = max(0, len(messages) - 6)

    older = messages[:split_idx]
    recent = messages[split_idx:]

    # ── Stage 3: Build smart summary of older messages ──
    # Try AI-powered summarization first (only when llm_config is provided and enough messages)
    ai_summary = None
    if llm_config and len(older) >= 4:
        ai_summary = _ai_summarize_messages(older, llm_config)
    
    if ai_summary:
        # Use AI-generated summary — much better context preservation
        summary_content = f'[Previous Conversation Summary]\n{ai_summary}'
        if code_changes:
            summary_content += '\n\nKEY CODE CHANGES (preserved):\n' + '\n'.join(f'  - {c[:300]}' for c in code_changes[:5])
        summary_msg = {'role': 'user', 'content': summary_content}
        print(f'[CONTEXT] Using AI-generated summary ({len(ai_summary)} chars) for {len(older)} older messages')
    else:
        # Fallback to mechanical summary (original logic)
        summary_parts = []
        for msg in older:
            role = msg.get('role', '')
            content = msg.get('content') or ''
            if role == 'user':
                summary_parts.append(f'[User]: {content[:300]}')
            elif role == 'assistant':
                text = content[:300] if content else '(tool calls only)'
                summary_parts.append(f'[Assistant]: {text}')
            elif role == 'tool':
                name = msg.get('name', 'tool')
                # Differentiated limits by tool type
                if name in ('write_file', 'edit_file'):
                    summary_parts.append(f'[Tool {name}]: {_truncate(content, 200)}')
                elif name in ('read_file', 'grep_code', 'search_files', 'glob_files', 'find_definition', 'find_references'):
                    summary_parts.append(f'[Tool {name}]: {_truncate(content, 150)}')
                elif name in ('run_command', 'install_package'):
                    summary_parts.append(f'[Tool {name}]: {_truncate(content, 200)}')
                elif name in ('todo_write', 'todo_read'):
                    summary_parts.append(f'[Tool {name}]: {_truncate(content, 300)}')
                else:
                    summary_parts.append(f'[Tool {name}]: {_truncate(content, 100)}')
    
        summary = 'Earlier conversation summary:\n'
        if code_changes:
            summary += 'KEY CODE CHANGES (preserved from earlier):\n' + '\n'.join(f'  • {c}' for c in code_changes[:5]) + '\n\n'
        summary += '\n'.join(summary_parts[-15:])
        summary_msg = {'role': 'user', 'content': summary}

    # ── Stage 4: Gentle compression — differentiated tool limits ──
    TOOL_LIMITS_GENTLE = {
        'read_file': 5000, 'glob_files': 2000, 'grep_code': 3000,
        'search_files': 3000, 'file_structure': 3000,
        'find_definition': 3000, 'find_references': 2000,
        'run_command': 3000, 'web_fetch': 2000,
        'delegate_task': 3000,
        'parallel_tasks': 6000,
        'kill_port': 2000,
        'todo_write': 2000, 'todo_read': 2000,
    }
    TOOL_LIMITS_DEFAULT = 4000

    compressed_recent = []
    for msg in recent:
        # Shallow copy to avoid mutating original messages list
        msg = dict(msg)
        if msg.get('role') == 'tool':
            content = msg.get('content') or ''
            name = msg.get('name', '')
            limit = TOOL_LIMITS_GENTLE.get(name, TOOL_LIMITS_DEFAULT)
            if len(content) > limit:
                msg['content'] = content[:limit] + f'\n[compressed: {len(content)}→{limit} chars]'
        compressed_recent.append(msg)

    all_msgs = [summary_msg] + compressed_recent
    total2 = sum(_estimate_tokens(m.get('content', '') or '') for m in all_msgs)

    # ── Stage 5: Aggressive compression if still too large ──
    if total2 > max_tokens:
        for msg in all_msgs:
            if msg.get('role') == 'tool':
                content = msg.get('content', '')
                if len(content) > 1500:
                    msg['content'] = content[:1500] + f'\n[compressed: {len(content)}→1500 chars]'

    total3 = sum(_estimate_tokens(m.get('content', '') or '') for m in all_msgs)
    if total3 > max_tokens:
        user_indices2 = [i for i, m in enumerate(all_msgs) if m.get('role') == 'user']
        if len(user_indices2) >= 1:
            keep_from = user_indices2[-1]
            kept = all_msgs[keep_from:]
            for msg in kept:
                if msg.get('role') == 'tool':
                    content = msg.get('content', '')
                    if len(content) > 800:
                        msg['content'] = content[:800] + f'\n[compressed: {len(content)}→800 chars]'
            all_msgs = [summary_msg] + kept

    # ── Stage 6: Ultimate fallback ──
    total4 = sum(_estimate_tokens(m.get('content', '') or '') for m in all_msgs)
    if total4 > max_tokens:
        minimal = [summary_msg]
        for msg in all_msgs[-2:]:
            content = msg.get('content', '') or ''
            minimal.append(dict(msg, content=content[:200] + ('...' if len(content) > 200 else '')))
        all_msgs = minimal

    final_total = sum(_estimate_tokens(m.get('content', '') or '') for m in all_msgs)
    print(f'[CONTEXT] Compressed: {_estimate_tokens(str(original_total*4))}→{final_total} tokens '
          f'({len(messages)}→{len(all_msgs)} messages, saved code changes: {len(code_changes)})')
    return all_msgs, was_compressed

# ==================== Agent Loop ====================
MAX_AGENT_ITERATIONS = 100  # Increased from 15 for complex tasks
MAX_ITERATION_RETRIES = 10

# Valid tool names for fallback detection
_TOOL_NAMES = frozenset(
    f.get('function', {}).get('name', '')
    for f in AGENT_TOOLS
)

def _try_parse_tool_calls_from_content(content):
    """Try to parse tool calls from LLM text content.

    Some models (especially non-OpenAI-compatible ones) return tool calls as
    JSON text in the content field instead of using the proper tool_calls field.
    This function detects and converts them to the standard tool_calls format.

    Returns list of tool_call dicts (OpenAI format) or None if not detected.
    """
    if not content or not content.strip():
        return None

    import re
    parsed_calls = []

    # Pattern 1: Standalone JSON objects with "name" and "arguments"
    # Matches: {"name": "run_command", "arguments": {"command": "...", ...}}
    standalone_pattern = re.compile(
        r'\{\s*"name"\s*:\s*"(\w+)"\s*,\s*"arguments"\s*:\s*',
        re.DOTALL
    )
    matches = list(standalone_pattern.finditer(content))
    if matches:
        # Try to extract complete JSON objects for each match
        for match in matches:
            start = match.start()
            # Find the matching closing brace
            depth = 0
            end = start
            for i in range(start, len(content)):
                if content[i] == '{':
                    depth += 1
                elif content[i] == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            json_str = content[start:end]
            try:
                obj = json.loads(json_str)
                name = obj.get('name', '')
                arguments = obj.get('arguments', {})
                if name and name in _TOOL_NAMES:
                    parsed_calls.append({
                        'id': f'call_parsed_{name}',
                        'type': 'function',
                        'function': {
                            'name': name,
                            'arguments': json.dumps(arguments, ensure_ascii=False) if isinstance(arguments, dict) else str(arguments),
                        },
                    })
            except (json.JSONDecodeError, KeyError, TypeError):
                continue

    # Pattern 2: Code-fenced tool calls: ```json\n{"name":...}\n```
    if not parsed_calls:
        fenced_pattern = re.compile(
            r'```(?:json|tool_calls?)\s*\n(\{[^`]*\})\s*\n```',
            re.DOTALL
        )
        for m in fenced_pattern.finditer(content):
            try:
                obj = json.loads(m.group(1))
                name = obj.get('name', '')
                arguments = obj.get('arguments', {})
                if name and name in _TOOL_NAMES:
                    parsed_calls.append({
                        'id': f'call_parsed_{name}',
                        'type': 'function',
                        'function': {
                            'name': name,
                            'arguments': json.dumps(arguments, ensure_ascii=False) if isinstance(arguments, dict) else str(arguments),
                        },
                    })
            except (json.JSONDecodeError, KeyError, TypeError):
                continue

    # Pattern 3: Markdown code block with tool_call (without json/lang marker)
    if not parsed_calls:
        # {"name": "...", "arguments": {...}} on its own line
        line_pattern = re.compile(
            r'^\s*\{"name"\s*:\s*"(\w+)"\s*,\s*"arguments"\s*:\s*\{.*\}\s*\}\s*$',
            re.MULTILINE | re.DOTALL
        )
        for m in line_pattern.finditer(content):
            try:
                obj = json.loads(m.group(0).strip())
                name = obj.get('name', '')
                arguments = obj.get('arguments', {})
                if name and name in _TOOL_NAMES:
                    parsed_calls.append({
                        'id': f'call_parsed_{name}',
                        'type': 'function',
                        'function': {
                            'name': name,
                            'arguments': json.dumps(arguments, ensure_ascii=False) if isinstance(arguments, dict) else str(arguments),
                        },
                    })
            except (json.JSONDecodeError, KeyError, TypeError):
                continue

    if parsed_calls:
        print(f'[AGENT] Parsed {len(parsed_calls)} tool call(s) from content text (model doesn\'t use tool_calls field)')
        return parsed_calls
    return None


def run_agent_loop(user_message, llm_config, history=None, stream_callback=None):
    """Run the full agent loop: LLM -> tools -> LLM -> ... until final answer.

    Args:
        user_message: The user's message string.
        llm_config: LLM configuration dict.
        history: Existing chat history list (will be appended to).
        stream_callback: Optional callable(event_dict) for real-time streaming.

    Returns:
        dict with 'content' (final text), 'iterations', 'tool_calls_made', 'history'
    """
    if history is None:
        history = load_chat_history()

    # Reset todo list for each new conversation (prevent cross-session leakage)
    with _active_todos['lock']:
        _active_todos['todos'] = []

    user_msg = {'role': 'user', 'content': user_message, 'time': datetime.now().isoformat()}
    history.append(user_msg)

    # Compress context if needed (with AI summarization for history-level compression)
    context, _ = _compress_context(history, max_tokens=_get_context_budget(llm_config), llm_config=llm_config)

    def _emit(event):
        if stream_callback:
            stream_callback(event)

    final_content = ''
    total_iterations = 0
    all_tool_calls = []
    self_corrections = 0  # Track self-correction retries

    for iteration in range(MAX_AGENT_ITERATIONS):
        total_iterations = iteration + 1
        _emit({'type': 'thinking', 'content': f'Iteration {iteration + 1}: Calling LLM...'})

        # Call LLM with retries
        response = None
        for retry in range(MAX_ITERATION_RETRIES):
            try:
                response = _call_llm_api(context, llm_config)
                break
            except urllib.error.HTTPError as e:
                body = e.read().decode() if hasattr(e, 'read') else ''
                if retry < MAX_ITERATION_RETRIES - 1:
                    _emit({'type': 'thinking', 'content': f'LLM API error (retry {retry + 1}): {e.code} {body[:200]}'})
                    time.sleep(1 * (retry + 1))
                else:
                    raise Exception(f'LLM API error after {MAX_ITERATION_RETRIES} retries ({e.code}): {body[:500]}')
            except Exception as e:
                if retry < MAX_ITERATION_RETRIES - 1:
                    _emit({'type': 'thinking', 'content': f'Retry {retry + 1}: {str(e)[:200]}'})
                    time.sleep(1 * (retry + 1))
                else:
                    raise Exception(f'LLM request failed after {MAX_ITERATION_RETRIES} retries: {str(e)}')

        # Parse response
        choice = response.get('choices', [{}])[0]
        message = choice.get('message', {})
        content = message.get('content', '') or ''
        tool_calls_raw = message.get('tool_calls', [])

        # Stream text content
        if content:
            _emit({'type': 'text', 'content': content})
            final_content = content

        # If no tool calls, we're done
        if not tool_calls_raw:
            break

        # Add assistant message with tool_calls to context
        assistant_msg = {
            'role': 'assistant',
            'content': content or None,
            'tool_calls': tool_calls_raw,
            'time': datetime.now().isoformat(),
        }
        context.append(assistant_msg)

        # Try parallel execution for read-only tools
        parallel_results = _execute_tools_parallel(tool_calls_raw)

        if parallel_results is not None:
            # Parallel execution succeeded — emit all results
            _batch_results = []
            for idx, tool_name, ok, result_str, elapsed, tool_call_id in parallel_results:
                all_tool_calls.append({'name': tool_name})
                _emit({'type': 'tool_start', 'tool': tool_name})
                _emit({'type': 'tool_result', 'tool': tool_name, 'ok': ok,
                       'result': _truncate(result_str, 30000), 'elapsed': round(elapsed, 2)})
                context.append({'role': 'tool', 'tool_call_id': tool_call_id,
                                'name': tool_name, 'tool': tool_name, 'content': result_str,
                                'time': datetime.now().isoformat()})
                _batch_results.append((tool_name, {}, ok, result_str))
            context, _ = _compress_context(context, max_tokens=_get_context_budget(llm_config))
            
            # === Self-Correction Check (parallel batch) ===
            self_corrections, _hint = _check_self_correction(context, _batch_results, self_corrections)
            if _hint:
                _emit({'type': 'thinking', 'content': f'Self-correction #{self_corrections}: Detected errors, retrying...'})
        else:
            # Sequential execution (mixed read/write tools or single tool)
            _batch_results = []
            for tc in tool_calls_raw:
                func = tc.get('function', {})
                tool_name = func.get('name', '')
                raw_args = func.get('arguments', '{}')
                tool_args, _was_recovered = _parse_tool_args(raw_args, tool_name)

                tool_call_id = tc.get('id', f'call_{tool_name}')
                all_tool_calls.append({'name': tool_name, 'args': tool_args})

                # Skip execution if args are irrecoverably broken for write/edit tools
                if tool_args.get('_skip_broken_args'):
                    _skip_path = tool_args.get('path', '(unknown)')
                    result_str = f'Error: {tool_name} arguments were truncated by max_tokens (path: {_skip_path}). The model\'s response was cut off before the file content could be fully generated. Please try again with a smaller file or increase max_tokens.'
                    _emit({'type': 'tool_start', 'tool': tool_name, 'args': tool_args})
                    _emit({
                        'type': 'tool_result',
                        'tool': tool_name,
                        'ok': False,
                        'result': result_str,
                        'elapsed': 0,
                    })
                    _batch_results.append((tool_name, tool_args, False, result_str))
                    continue

                _emit({'type': 'tool_start', 'tool': tool_name, 'args': tool_args})

                ok, result_str, elapsed = execute_agent_tool_with_timeout(tool_name, tool_args)

                _emit({
                    'type': 'tool_result',
                    'tool': tool_name,
                    'ok': ok,
                    'result': _truncate(result_str, 30000),
                    'elapsed': round(elapsed, 2),
                })

                # Add tool result to context
                context.append({
                    'role': 'tool',
                    'tool_call_id': tool_call_id,
                    'name': tool_name,
                    'tool': tool_name,
                    'content': result_str,
                    'time': datetime.now().isoformat(),
                })
                _batch_results.append((tool_name, tool_args, ok, result_str))

                # Re-check context size and compress if needed
                context, _ = _compress_context(context, max_tokens=_get_context_budget(llm_config))
            
            # === Self-Correction Check (sequential batch) ===
            self_corrections, _hint = _check_self_correction(context, _batch_results, self_corrections)
            if _hint:
                _emit({'type': 'thinking', 'content': f'Self-correction #{self_corrections}: Detected errors, retrying...'})

    # Build final assistant message for history
    final_assistant = {
        'role': 'assistant',
        'content': final_content,
        'tool_calls_made': all_tool_calls,
        'iterations': total_iterations,
        'time': datetime.now().isoformat(),
    }
    history.append(final_assistant)

    return {
        'content': final_content,
        'iterations': total_iterations,
        'tool_calls_made': all_tool_calls,
        'history': history,
    }

def run_agent_loop_stream(user_message, llm_config, conv_id=None, is_retry=False):
    """Generator that runs the agent loop and yields SSE events.
    
    Args:
        user_message: The user's message text.
        llm_config: LLM configuration dict.
        conv_id: Optional conversation ID for persistence.
        is_retry: If True, this is a retry of a failed turn. The conversation
                  history already contains the user message and partial progress,
                  so we don't add the user message again.
    """
    # Load history from conversation if conv_id provided, otherwise from legacy chat_history
    if conv_id:
        conv = get_conversation(conv_id)
        history = list(conv.get('messages', [])) if conv else []
    else:
        history = load_chat_history()

    # Only add user message if this is NOT a retry
    # On retry, the history already has the user message from the failed run
    if not is_retry:
        user_msg = {'role': 'user', 'content': user_message, 'time': datetime.now().isoformat()}
        history.append(user_msg)

    # Compress context if needed (with AI summarization for history-level compression)
    context, _ = _compress_context(history, max_tokens=_get_context_budget(llm_config), llm_config=llm_config)

    # Trigger background AST index if stale or empty (runs in daemon thread)
    try:
        from utils import load_config
        cfg = load_config()
        ws = cfg.get('workspace', WORKSPACE)
        prj = cfg.get('project', None)
        project_root = os.path.join(ws, prj) if prj else ws
        if os.path.isdir(project_root) and (project_index.file_count == 0 or
                (time.time() - project_index.last_index_time > 300)):
            threading.Thread(target=project_index.index_project,
                           args=(project_root,), kwargs={'max_files': 1000, 'max_time': 10},
                           daemon=True).start()
    except Exception:
        pass

    # Reset todo list for each new conversation (prevent cross-session leakage)
    with _active_todos['lock']:
        _active_todos['todos'] = []

    # Pre-save history before starting the loop so retry can recover even if
    # the very first LLM call fails (before any tool execution).
    save_chat_history(history)
    if conv_id:
        save_conversation(conv_id, history)

    # Check and report .muside/ project knowledge loading status
    try:
        from utils import load_config as _load_cfg
        _cfg = _load_cfg()
        _ws_check = _cfg.get('workspace', WORKSPACE)
        _prj_check = _cfg.get('project', None)
        _pdir_check = os.path.join(_ws_check, _prj_check) if _prj_check else _ws_check
        if not os.path.isdir(_pdir_check):
            _pdir_check = _ws_check
        _muside_check = os.path.join(_pdir_check, '.muside')
        if not os.path.isdir(_muside_check):
            _muside_check = os.path.join(os.path.dirname(SERVER_DIR), '.muside')
        if os.path.isdir(_muside_check):
            _md_files = [f for f in ['rules.md', 'architecture.md', 'conventions.md'] if os.path.isfile(os.path.join(_muside_check, f))]
            if _md_files:
                yield f"data: {json.dumps({'type': 'thinking', 'content': f'\U0001f4c2 .muside/ loaded: {', '.join(_md_files)}'})}\n\n"
                log_write(f'[muside] SSE: .muside/ loaded from {_muside_check}: {_md_files}')
            else:
                yield f"data: {json.dumps({'type': 'thinking', 'content': '\u26a0\ufe0f .muside/ exists but has no content files'})}\n\n"
                log_write(f'[muside] SSE: .muside/ empty at {_muside_check}')
        else:
            yield f"data: {json.dumps({'type': 'thinking', 'content': '\u26a0\ufe0f .muside/ not found — no project knowledge loaded'})}\n\n"
            log_write(f'[muside] SSE: .muside/ not found, checked {_pdir_check} and {os.path.dirname(SERVER_DIR)}')
    except Exception as _e:
        log_write(f'[muside] SSE check error: {_e}')

    final_content = ''
    total_iterations = 0
    accumulated_text = ''
    tool_calls_in_progress = []
    loop_completed_normally = False
    self_corrections = 0  # Track self-correction retries
    # Buffer for streaming tool_calls assembly
    current_tool_calls = []
    current_tool_call_idx = {}
    current_args_buffer = {}

    for iteration in range(MAX_AGENT_ITERATIONS):
        total_iterations = iteration + 1

        # Check cancellation before each iteration
        if _active_task.get('cancelled'):
            yield f"data: {json.dumps({'type': 'error', 'content': 'Task cancelled by user.'})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'completed': False, 'iterations': total_iterations})}\n\n"
            return

        yield f"data: {json.dumps({'type': 'thinking', 'content': f'Iteration {iteration + 1}: Calling LLM...'})}\n\n"

        # Call LLM with streaming
        response_message = None
        finish_reason = None
        retry = 0
        context_retries = 0
        MAX_CONTEXT_RETRIES = 20
        # Tools level: 'full' (default) — used for debugging payload errors
        # Payload format errors ("Unterminated string") are now shown raw with debug info
        current_tools_level = 'full'
        while retry < MAX_ITERATION_RETRIES:
            try:
                current_tool_calls = []
                current_args_buffer = {}
                current_tool_call_idx = {}
                delta_content = ''
                delta_tool_calls = []
                saved_accumulated = accumulated_text  # save for rollback on failure
                # Pre-compute LLM URL for error reporting
                try:
                    current_llm_url, _ = _get_llm_endpoint(llm_config, llm_config.get('model', 'gpt-4o-mini'))
                except Exception:
                    current_llm_url = '(unknown)'

                finish_reason = None
                reasoning_text = ''  # accumulate reasoning/thinking content
                reasoning_ended = False
                for delta in _call_llm_stream_raw(context, llm_config, tools_level=current_tools_level):
                    # Check cancellation during LLM streaming
                    if _active_task.get('cancelled'):
                        yield f"data: {json.dumps({'type': 'error', 'content': 'Task cancelled by user.'})}\n\n"
                        yield f"data: {json.dumps({'type': 'done', 'completed': False, 'iterations': total_iterations})}\n\n"
                        return

                    # Capture finish_reason
                    fr = delta.get('_finish_reason')
                    if fr:
                        finish_reason = fr

                    # Handle reasoning_content (DeepSeek-R1, QwQ, Step, GLM, Kimi, etc.)
                    reasoning_chunk = delta.get('reasoning_content')
                    content_chunk = delta.get('content') or None

                    if reasoning_chunk:
                        reasoning_text += reasoning_chunk
                        yield f"data: {json.dumps({'type': 'reasoning', 'content': reasoning_chunk})}\n\n"
                        # Don't skip — also check for content below

                    # Signal reasoning_end when we transition from reasoning to content
                    if reasoning_text and not reasoning_ended and content_chunk:
                        yield f"data: {json.dumps({'type': 'reasoning_end'})}\n\n"
                        reasoning_ended = True

                    # Handle text content
                    if content_chunk:
                        delta_content += content_chunk
                        accumulated_text += content_chunk
                        yield f"data: {json.dumps({'type': 'text', 'content': content_chunk})}\n\n"

                    # Handle tool_calls (assembled from streaming deltas)
                    tc_delta = delta.get('tool_calls')
                    if tc_delta:
                        for tc_part in tc_delta:
                            idx = tc_part.get('index', 0)
                            if idx not in current_tool_call_idx:
                                current_tool_call_idx[idx] = len(current_tool_calls)
                                tc_entry = {
                                    'id': tc_part.get('id', f'call_{idx}'),
                                    'type': 'function',
                                    'function': {'name': '', 'arguments': ''},
                                }
                                current_tool_calls.append(tc_entry)
                                current_args_buffer[idx] = ''

                            tc_entry = current_tool_calls[current_tool_call_idx[idx]]
                            if tc_part.get('id'):
                                tc_entry['id'] = tc_part['id']
                            func_delta = tc_part.get('function', {})
                            if func_delta.get('name'):
                                tc_entry['function']['name'] += func_delta['name']
                            if func_delta.get('arguments'):
                                current_args_buffer[idx] += func_delta['arguments']

                # Finalize tool call arguments
                for idx, tc_entry in enumerate(current_tool_calls):
                    if idx in current_args_buffer:
                        tc_entry['function']['arguments'] = current_args_buffer[idx]

                # If reasoning was accumulated but reasoning_end not yet signaled
                if reasoning_text and not reasoning_ended:
                    yield f"data: {json.dumps({'type': 'reasoning_end'})}\n\n"

                # Build the complete response message
                response_message = {
                    'role': 'assistant',
                    'content': delta_content or None,
                }
                # Filter out empty/invalid tool calls (some models send blanks)
                valid_tool_calls = [tc for tc in current_tool_calls
                                    if tc.get('function', {}).get('name', '').strip()]
                if valid_tool_calls:
                    response_message['tool_calls'] = valid_tool_calls
                break  # success — exit retry loop

            except urllib.error.HTTPError as e:
                accumulated_text = saved_accumulated  # rollback on failure
                body = e.read().decode() if hasattr(e, 'read') else ''
                
                # Build detailed error with payload debugging info
                err_detail = f'URL: {current_llm_url}\nHTTP {e.code}: {body[:500]}'
                
                # For "Unterminated string" / payload format errors, dump raw payload for debugging
                is_payload_error = any(kw in body.lower() for kw in ['unterminated', 'input_invalid'])
                if is_payload_error and _last_llm_payload_debug:
                    _debug = _last_llm_payload_debug
                    _payload_body = _debug.get('body', b'')
                    _payload_size = _debug.get('size', 0)
                    # Parse error to find the character position
                    import re as _err_re
                    _char_match = _err_re.search(r'char (\d+)', body)
                    _char_pos = int(_char_match.group(1)) if _char_match else None
                    
                    # Extract system prompt from payload
                    _sys_content = ''
                    _payload_obj = _debug.get('payload', {})
                    for _msg in _payload_obj.get('messages', []):
                        if _msg.get('role') == 'system':
                            _sys_content = _msg.get('content', '')
                            break
                    
                    # Extract snippet around the error position
                    _snippet = ''
                    if _char_pos is not None and _char_pos < len(_payload_body):
                        _start = max(0, _char_pos - 100)
                        _end = min(len(_payload_body), _char_pos + 100)
                        _snippet = _payload_body[_start:_end].decode('utf-8', errors='replace')
                        _snippet = f'\n--- Payload around char {_char_pos} (showing {_start}-{_end}) ---\n...{_snippet}...\n--- End snippet ---'
                    
                    # Build comprehensive debug output
                    _debug_info = (
                        f'\n\n===== PAYLOAD DEBUG =====\n'
                        f'Payload size: {_payload_size} bytes ({_payload_size/1024:.1f} KB)\n'
                        f'Error position: char {_char_pos}\n'
                        f'Tools level: {current_tools_level}\n'
                        f'System prompt length: {len(_sys_content)} chars\n'
                        f'System prompt first 2000 chars:\n{_sys_content[:2000]}\n'
                        f'{_snippet}'
                        f'\n===== END DEBUG ====='
                    )
                    print(f'[LLM] PAYLOAD ERROR DEBUG:{_debug_info}')
                    err_detail += _debug_info
                
                # Detect context length exceeded errors (NOT payload format errors)
                is_context_error = (
                    e.code in (400, 413, 422) and
                    any(kw in body.lower() for kw in [
                        'context', 'token', 'max_length', 'maximum context',
                        'too many tokens', 'input too large', 'prompt is too long',
                        'request too large', 'exceeds the model', 'context_length',
                        'max_tokens', 'too long', '超出', '上下文',
                    ]) and
                    not is_payload_error  # payload errors should be shown raw, not auto-compressed
                )
                
                if is_context_error:
                    context_retries += 1
                    if context_retries >= MAX_CONTEXT_RETRIES:
                        yield f"data: {json.dumps({'type': 'error', 'content': f'Context still too large after {MAX_CONTEXT_RETRIES} compression attempts. Please start a new conversation.'})}\n\n"
                        yield f"data: {json.dumps({'type': 'done', 'completed': False, 'iterations': total_iterations})}\n\n"
                        return
                    # Aggressively compress context and retry (don't consume normal retry counter)
                    budget = llm_config.get('max_tokens', 4096) * 10
                    context, was_compressed = _compress_context(context, max_tokens=max(budget // 2, 4000))
                    yield f"data: {json.dumps({'type': 'thinking', 'content': f'Context too large, compressing and retrying ({context_retries}/{MAX_CONTEXT_RETRIES})...'})}\n\n"
                    print(f'[LLM] Context overflow detected (HTTP {e.code}), compressed to {sum(_estimate_tokens(m.get("content","") or "") for m in context)} tokens (budget: {budget // 2})')
                    time.sleep(0.5)
                    continue
                
                retry += 1
                if retry < MAX_ITERATION_RETRIES:
                    yield f"data: {json.dumps({'type': 'thinking', 'content': f'LLM API error (retry {retry}): {err_detail[:200]}'})}\n\n"
                    time.sleep(1 * retry)
                else:
                    yield f"data: {json.dumps({'type': 'error', 'content': f'LLM API error after {MAX_ITERATION_RETRIES} retries:\n{err_detail}'})}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'completed': False, 'iterations': total_iterations})}\n\n"
                    return
            except Exception as e:
                accumulated_text = saved_accumulated  # rollback on failure
                err_detail = f'URL: {current_llm_url}\nError: {str(e)}'
                
                # Also check for context errors in generic exceptions
                # NOTE: "unterminated" is NOT treated as context error — it's a payload format error
                err_lower = str(e).lower()
                is_context_error = any(kw in err_lower for kw in [
                    'context', 'token', 'max_length', 'too many tokens',
                    'prompt is too long', 'too long',
                ]) and 'unterminated' not in err_lower
                
                if is_context_error:
                    context_retries += 1
                    if context_retries >= MAX_CONTEXT_RETRIES:
                        yield f"data: {json.dumps({'type': 'error', 'content': f'Context still too large after {MAX_CONTEXT_RETRIES} compression attempts.'})}\n\n"
                        yield f"data: {json.dumps({'type': 'done', 'completed': False, 'iterations': total_iterations})}\n\n"
                        return
                    budget = llm_config.get('max_tokens', 4096) * 10
                    context, was_compressed = _compress_context(context, max_tokens=max(budget // 2, 4000))
                    yield f"data: {json.dumps({'type': 'thinking', 'content': f'Context error, compressing and retrying ({context_retries}/{MAX_CONTEXT_RETRIES})...'})}\n\n"
                    print(f'[LLM] Context overflow exception, compressed and retrying')
                    time.sleep(0.5)
                    continue
                
                retry += 1
                if retry < MAX_ITERATION_RETRIES:
                    yield f"data: {json.dumps({'type': 'thinking', 'content': f'Retry {retry}: {err_detail[:200]}'})}\n\n"
                    time.sleep(1 * retry)
                else:
                    yield f"data: {json.dumps({'type': 'error', 'content': f'LLM request failed after {MAX_ITERATION_RETRIES} retries:\n{err_detail}'})}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'completed': False, 'iterations': total_iterations})}\n\n"
                    return

        if response_message is None:
            # All retries exhausted without success
            yield f"data: {json.dumps({'type': 'error', 'content': 'All retries failed: LLM did not return a valid response.'})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'completed': False, 'iterations': total_iterations})}\n\n"
            return

        # Warn if model hit max_tokens (finish_reason == 'length')
        if finish_reason == 'length':
            if _has_tool_calls(response_message):
                # Model had tool calls but was truncated — validate arguments
                _raw_tc = response_message.get('tool_calls', [])
                _valid_tc = []
                _invalid_tc_names = []
                for _tc in _raw_tc:
                    _func = _tc.get('function', {})
                    _tc_name = _func.get('name', '')
                    _tc_args_str = _func.get('arguments', '')
                    if not _tc_name.strip():
                        continue
                    # Check if args are parseable or recoverable
                    _tc_args, _tc_recovered = _parse_tool_args(_tc_args_str, _tc_name)
                    if _tc_args and not _tc_args.get('_skip_broken_args'):
                        _valid_tc.append(_tc)
                    else:
                        _invalid_tc_names.append(_tc_name or '(unknown)')

                if _invalid_tc_names:
                    print(f'[LLM] Truncated tool_calls detected: {", ".join(_invalid_tc_names)} arguments incomplete/recoverable (finish_reason=length)')
                    # Keep valid/recoverable tool calls, discard truly broken ones
                    response_message['tool_calls'] = _valid_tc
                    if not _valid_tc:
                        # All tool calls broken — inject retry hint
                        _continue_hint = f'Your previous response was truncated by max_tokens. You were trying to call: {", ".join(_invalid_tc_names)}. Please continue and call those tools again.'
                        context.append({'role': 'user', 'content': _continue_hint})
                        _truncated_names = ', '.join(_invalid_tc_names)
                        yield f"data: {json.dumps({'type': 'thinking', 'content': f'Response truncated (max_tokens), tool calls [{_truncated_names}] were incomplete. Retrying...'})}\n\n"
                        # Don't count this as a completed iteration — retry
                        accumulated_text = ''  # reset for next iteration
                        continue
                # All tool calls valid or recoverable despite length truncation
                yield f"data: {json.dumps({'type': 'warning', 'content': 'Response was truncated (max_tokens reached) but tool calls are intact or recoverable. Consider increasing Max Tokens.'})}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'warning', 'content': 'Response was truncated (max_tokens reached). Consider increasing Max Tokens in settings for longer responses.'})}\n\n"

        content = response_message.get('content', '') or ''
        tool_calls_raw = response_message.get('tool_calls', [])

        # ── Fallback: parse tool calls from content if model doesn't use tool_calls field ──
        # Some models (e.g. non-OpenAI-compatible) return tool calls as JSON in content instead
        # of using the proper tool_calls field. Detect and parse them.
        if not tool_calls_raw and content.strip():
            _parsed_from_content = _try_parse_tool_calls_from_content(content)
            if _parsed_from_content:
                tool_calls_raw = _parsed_from_content
                content = ''
                # Remove the leaked tool call text from accumulated display
                for _tc in _parsed_from_content:
                    _tc_json = json.dumps(_tc, ensure_ascii=False)
                    accumulated_text = accumulated_text.replace(_tc_json, '').strip()
                # Clean up common wrapper patterns
                import re as _re
                accumulated_text = _re.sub(r'```json\s*\n?\s*```', '', accumulated_text).strip()
                accumulated_text = _re.sub(r'```tool_calls?\s*\n?\s*```', '', accumulated_text).strip()
                if not accumulated_text.strip():
                    accumulated_text = ''

        # Handle completely empty response (no content, no tool calls)
        if not content.strip() and not tool_calls_raw:
            # Build debug info from raw API response for diagnosis
            _empty_debug = []
            _empty_debug.append(f'finish_reason: {finish_reason}')
            if reasoning_text:
                _empty_debug.append(f'reasoning_text length: {len(reasoning_text)} chars')
                _empty_debug.append(f'reasoning_text (last 500): {reasoning_text[-500:]}')
            _debug = _last_llm_payload_debug
            _empty_debug.append(f'payload_size: {_debug.get("size", "?")} bytes')
            _empty_debug.append(f'payload_url: {_debug.get("url", "?")}')
            _empty_debug.append(f'sse_chunks_received: {_debug.get("raw_sse_count", "?")}')
            _empty_debug.append(f'sse_finish_reason: {_debug.get("finish_reason", "?")}')
            _raw_tail = _debug.get('raw_sse_tail', [])
            if _raw_tail:
                _empty_debug.append(f'last_sse_chunks: {json.dumps(_raw_tail, ensure_ascii=False)[:1000]}')
            # Print full debug to server log
            print(f'[LLM] EMPTY RESPONSE DEBUG:\n' + '\n'.join(f'  {l}' for l in _empty_debug))

            # Try auto-retry for empty responses (up to 3 times)
            empty_retries = getattr(run_agent_loop_stream, '_empty_retry', 0) + 1
            run_agent_loop_stream._empty_retry = empty_retries
            if empty_retries <= 3:
                _retry_hint = f'finish_reason={finish_reason}' + (f', reasoning={len(reasoning_text)} chars' if reasoning_text else '')
                yield f"data: {json.dumps({'type': 'thinking', 'content': f'Model returned empty response ({_retry_hint}), auto-retrying ({empty_retries}/3)...'})}\n\n"
                print(f'[LLM] Empty response detected (iteration {total_iterations}), retrying ({empty_retries}/3)')
                # Inject a continuation prompt so the model has something to respond to
                # Instead of blindly retrying the same context (which may cause the same empty response),
                # add a user message nudging the model to continue its work.
                _empty_nudge = 'It seems your previous response was empty. Please continue with your task — call the appropriate tools (e.g., write_file, edit_file) to proceed.'
                context.append({'role': 'user', 'content': _empty_nudge})
                accumulated_text = ''  # reset for next iteration
                time.sleep(1)
                continue  # retry the same iteration
            else:
                run_agent_loop_stream._empty_retry = 0
                _err_detail = 'Model returned empty response 3 times in a row.\n\nRaw debug info:\n' + '\n'.join(f'  {l}' for l in _empty_debug)
                yield f"data: {json.dumps({'type': 'error', 'content': _err_detail})}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'completed': False, 'iterations': total_iterations})}\n\n"
                return
        # Reset empty retry counter on successful response
        run_agent_loop_stream._empty_retry = 0

        if content:
            final_content = accumulated_text.strip() if accumulated_text.strip() else content

        # If no tool calls, we're done
        if not tool_calls_raw:
            # Don't save empty assistant message to history
            if not final_content.strip():
                _final_debug = []
                _final_debug.append(f'finish_reason: {finish_reason}')
                if reasoning_text:
                    _final_debug.append(f'reasoning_text length: {len(reasoning_text)} chars')
                    _final_debug.append(f'reasoning_text (last 500): {reasoning_text[-500:]}')
                _debug = _last_llm_payload_debug
                _final_debug.append(f'payload_size: {_debug.get("size", "?")} bytes')
                _final_debug.append(f'sse_chunks_received: {_debug.get("raw_sse_count", "?")}')
                _final_debug.append(f'sse_finish_reason: {_debug.get("finish_reason", "?")}')
                print(f'[LLM] EMPTY FINAL RESPONSE DEBUG:\n' + '\n'.join(f'  {l}' for l in _final_debug))
                _err_msg = 'Model returned an empty final response.\n\nRaw debug info:\n' + '\n'.join(f'  {l}' for l in _final_debug)
                yield f"data: {json.dumps({'type': 'error', 'content': _err_msg})}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'completed': False, 'iterations': total_iterations})}\n\n"
                return
            loop_completed_normally = True
            break

        # Add assistant message to context AND history for progressive save
        assistant_msg = {
            'role': 'assistant',
            'content': content or None,
            'tool_calls': tool_calls_raw,
            'time': datetime.now().isoformat(),
        }
        context.append(assistant_msg)
        history.append(assistant_msg)

        # Reset accumulated text for next iteration
        accumulated_text = ''

        # Check cancellation before tool execution
        if _active_task.get('cancelled'):
            yield f"data: {json.dumps({'type': 'error', 'content': 'Task cancelled by user.'})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'completed': False, 'iterations': total_iterations})}\n\n"
            return

        # Try parallel execution for read-only tools
        parallel_results = _execute_tools_parallel(tool_calls_raw)

        if parallel_results is not None:
            # Parallel execution
            _batch_results = []
            for idx, tool_name, ok, result_str, elapsed, tool_call_id in parallel_results:
                tool_calls_in_progress.append({'name': tool_name})
                yield f"data: {json.dumps({'type': 'tool_start', 'tool': tool_name})}\n\n"
                yield f"data: {json.dumps({'type': 'tool_result', 'tool': tool_name, 'ok': ok, 'result': _truncate(result_str, 30000), 'elapsed': round(elapsed, 2), 'max_iterations': MAX_AGENT_ITERATIONS})}\n\n"
                tool_msg = {'role': 'tool', 'tool_call_id': tool_call_id,
                            'name': tool_name, 'tool': tool_name, 'content': result_str,
                            'time': datetime.now().isoformat()}
                context.append(tool_msg)
                history.append(tool_msg)
                _batch_results.append((tool_name, {}, ok, result_str))
            context, _ = _compress_context(context, max_tokens=_get_context_budget(llm_config))
            
            # === Self-Correction Check (parallel batch) ===
            self_corrections, _hint = _check_self_correction(context, _batch_results, self_corrections)
            if _hint:
                yield f"data: {json.dumps({'type': 'thinking', 'content': f'Self-correction #{self_corrections}: Detected errors in {len(_batch_results)} tool(s), analyzing and retrying...'})}\n\n"
        else:
            # Sequential execution (mixed read/write tools or single tool)
            _batch_results = []
            for tc in tool_calls_raw:
                # Check cancellation before each tool
                if _active_task.get('cancelled'):
                    yield f"data: {json.dumps({'type': 'error', 'content': 'Task cancelled by user.'})}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'completed': False, 'iterations': total_iterations})}\n\n"
                    return

                func = tc.get('function', {})
                tool_name = func.get('name', '')
                raw_args = func.get('arguments', '{}')
                tool_args, _was_recovered = _parse_tool_args(raw_args, tool_name)

                tool_call_id = tc.get('id', f'call_{tool_name}')
                tool_calls_in_progress.append({'name': tool_name, 'args': tool_args})

                # Skip execution if args are irrecoverably broken for write/edit tools
                if tool_args.get('_skip_broken_args'):
                    _skip_path = tool_args.get('path', '(unknown)')
                    result_str = f'Error: {tool_name} arguments were truncated by max_tokens (path: {_skip_path}). The model\'s response was cut off before the file content could be fully generated. Please try again with a smaller file or increase max_tokens.'
                    yield f"data: {json.dumps({'type': 'tool_start', 'tool': tool_name, 'args': tool_args})}\n\n"
                    yield f"data: {json.dumps({'type': 'tool_result', 'tool': tool_name, 'ok': False, 'result': result_str, 'elapsed': 0, 'max_iterations': MAX_AGENT_ITERATIONS})}\n\n"
                    # Add tool result to context so model knows it failed (assistant_msg already added above)
                    tool_msg = {
                        'role': 'tool',
                        'tool_call_id': tool_call_id,
                        'name': tool_name,
                        'tool': tool_name,
                        'content': result_str,
                    }
                    context.append(tool_msg)
                    history.append(tool_msg)
                    _batch_results.append((tool_name, tool_args, False, result_str))
                    continue

                yield f"data: {json.dumps({'type': 'tool_start', 'tool': tool_name, 'args': tool_args})}\n\n"

                ok, result_str, elapsed = execute_agent_tool_with_timeout(tool_name, tool_args)

                yield f"data: {json.dumps({'type': 'tool_result', 'tool': tool_name, 'ok': ok, 'result': _truncate(result_str, 30000), 'elapsed': round(elapsed, 2), 'max_iterations': MAX_AGENT_ITERATIONS})}\n\n"

                tool_msg = {
                    'role': 'tool',
                    'tool_call_id': tool_call_id,
                    'name': tool_name,
                    'tool': tool_name,
                    'content': result_str,
                    'time': datetime.now().isoformat(),
                }
                context.append(tool_msg)
                history.append(tool_msg)
                _batch_results.append((tool_name, tool_args, ok, result_str))

                # Save after each tool so refresh mid-iteration preserves partial progress
                save_chat_history(history)
                if conv_id:
                    save_conversation(conv_id, history)

                # Compress context if needed
                context, _ = _compress_context(context, max_tokens=_get_context_budget(llm_config))
            
            # === Self-Correction Check (sequential batch) ===
            self_corrections, _hint = _check_self_correction(context, _batch_results, self_corrections)
            if _hint:
                yield f"data: {json.dumps({'type': 'thinking', 'content': f'Self-correction #{self_corrections}: Detected errors, analyzing and retrying...'})}\n\n"

        # Progressive save: persist history after each iteration so retry can resume
        save_chat_history(history)
        if conv_id:
            save_conversation(conv_id, history)

    # Build final assistant message for history
    final_assistant = {
        'role': 'assistant',
        'content': final_content,
        'tool_calls_made': tool_calls_in_progress,
        'iterations': total_iterations,
        'time': datetime.now().isoformat(),
    }
    history.append(final_assistant)
    save_chat_history(history)
    # Also save to conversation if conv_id was provided
    if conv_id:
        save_conversation(conv_id, history)

    if not loop_completed_normally:
        yield f"data: {json.dumps({'type': 'warning', 'content': f'Agent loop reached max iterations ({MAX_AGENT_ITERATIONS}). Task may be incomplete.'})}\n\n"

    yield f"data: {json.dumps({'type': 'done', 'iterations': total_iterations, 'tool_calls': len(tool_calls_in_progress), 'completed': loop_completed_normally})}\n\n"

# ==================== Chat Endpoints ====================
@bp.route('/api/chat/history', methods=['GET'])
def get_chat_history():
    history = load_chat_history()
    # Also return the most recent conv_id so the frontend can resume the conversation
    convs = load_conversations()
    latest_conv_id = convs[0]['id'] if convs else None
    return jsonify({'messages': history, 'conv_id': latest_conv_id})

@bp.route('/api/chat/clear', methods=['POST'])
def clear_chat_history():
    save_chat_history([])
    return jsonify({'ok': True})

# ==================== Conversations API ====================
@bp.route('/api/conversations', methods=['GET'])
def list_conversations():
    """List all conversations (summary, no messages)."""
    convs = load_conversations()
    result = []
    for c in convs:
        result.append({
            'id': c.get('id', ''),
            'title': c.get('title', 'New Chat'),
            'created_at': c.get('created_at', ''),
            'updated_at': c.get('updated_at', ''),
            'message_count': len(c.get('messages', [])),
        })
    return jsonify({'conversations': result})

@bp.route('/api/conversations/<conv_id>', methods=['GET'])
def get_conv(conv_id):
    """Get a single conversation with messages."""
    conv = get_conversation(conv_id)
    if not conv:
        return jsonify({'error': 'Conversation not found'}), 404
    return jsonify(conv)

@bp.route('/api/conversations/<conv_id>', methods=['DELETE'])
def delete_conv(conv_id):
    """Delete a conversation."""
    delete_conversation(conv_id)
    return jsonify({'ok': True})

@bp.route('/api/conversations/<conv_id>', methods=['PATCH'])
def update_conv(conv_id):
    """Update conversation title."""
    data = request.json or {}
    convs = load_conversations()
    for c in convs:
        if c.get('id') == conv_id:
            if 'title' in data:
                c['title'] = data['title']
            break
    else:
        return jsonify({'error': 'Conversation not found'}), 404
    save_conversations(convs)
    return jsonify({'ok': True})

@bp.route('/api/chat/send', methods=['POST'])
@handle_error
def send_chat_message():
    """Non-streaming agent endpoint. Returns complete result after agent loop finishes."""
    data = request.json
    message = data.get('message', '').strip()
    if not message:
        return jsonify({'error': 'Message required'}), 400

    # Allow frontend to specify which model to use by index
    model_index = data.get('model_index')
    if model_index is not None:
        all_config = load_llm_config()
        models = all_config.get('models', [])
        idx = int(model_index)
        if 0 <= idx < len(models):
            llm_config = dict(models[idx])
            llm_config['system_prompt'] = llm_config.get('system_prompt') or all_config.get('system_prompt', '')
        else:
            return jsonify({'error': f'Invalid model index: {idx}'}), 400
    else:
        llm_config = get_active_llm_config()

    try:
        events = []
        result = run_agent_loop(message, llm_config, stream_callback=lambda e: events.append(e))
        save_chat_history(result['history'])
        return jsonify({
            'response': {'role': 'assistant', 'content': result['content']},
            'iterations': result['iterations'],
            'tool_calls_made': result['tool_calls_made'],
            'events': events,
            'history': result['history'][-20:],
        })
    except Exception as e:
        return jsonify({'error': str(e), 'response': {'role': 'assistant', 'content': f'Error: {str(e)}'}}), 500

@bp.route('/api/chat/send/stream', methods=['POST'])
def send_chat_stream():
    """SSE streaming agent endpoint. Runs agent in background thread, broadcasts events."""
    data = request.json
    message = data.get('message', '').strip()
    conv_id = data.get('conv_id')  # optional conversation id
    is_retry = data.get('retry', False)  # if True, continue from failed state instead of restart
    if not message and not is_retry:
        return jsonify({'error': 'Message required'}), 400

    # Allow frontend to specify which model to use by index
    model_index = data.get('model_index')
    if model_index is not None:
        all_config = load_llm_config()
        models = all_config.get('models', [])
        idx = int(model_index)
        if 0 <= idx < len(models):
            llm_config = dict(models[idx])
            llm_config['system_prompt'] = llm_config.get('system_prompt') or all_config.get('system_prompt', '')
        else:
            return jsonify({'error': f'Invalid model index: {idx}'}), 400
    else:
        llm_config = get_active_llm_config()

    print(f'[CHAT] send_chat_stream called')
    print(f'[CHAT] LLM config: name={llm_config.get("name")}, api_type={llm_config.get("api_type")}, model={llm_config.get("model")}, api_base={llm_config.get("api_base")}, api_key={"***"+llm_config.get("api_key","")[-6:] if llm_config.get("api_key") else "EMPTY"}')

    # Set up the global active task state
    with _active_task['lock']:
        if _active_task['running']:
            return jsonify({'error': 'A task is already running'}), 409

        event_queue = queue.Queue()
        event_buffer = deque(maxlen=RING_BUFFER_SIZE)

        _active_task['running'] = True
        _active_task['cancelled'] = False
        _active_task['conv_id'] = conv_id
        _active_task['message'] = message
        _active_task['model_index'] = model_index
        _active_task['started_at'] = time.time()
        _active_task['event_queue'] = event_queue
        _active_task['event_buffer'] = event_buffer
        _active_task['subscribers'] = 1

    def _run_agent():
        """Background thread: runs the agent loop and puts events into queue + buffer."""
        my_thread_id = threading.current_thread().ident
        try:
            for sse_event in run_agent_loop_stream(message, llm_config, conv_id=conv_id, is_retry=is_retry):
                # Check cancellation before enqueueing
                with _active_task['lock']:
                    if _active_task.get('cancelled'):
                        cancel_event = f"data: {json.dumps({'type': 'cancelled', 'content': 'Task cancelled by user.'})}\n\n"
                        event_queue.put(cancel_event)
                        _active_task['event_buffer'].append(cancel_event)
                        break
                event_queue.put(sse_event)
                with _active_task['lock']:
                    _active_task['event_buffer'].append(sse_event)
        except Exception as e:
            err_event = f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"
            event_queue.put(err_event)
            with _active_task['lock']:
                _active_task['event_buffer'].append(err_event)
            done_event = f"data: {json.dumps({'type': 'done', 'completed': False, 'error': True})}\n\n"
            event_queue.put(done_event)
            with _active_task['lock']:
                _active_task['event_buffer'].append(done_event)
        finally:
            # Signal completion and mark task as no longer running
            # Only set running=False if we are still the active task thread
            # (a new task may have started while we were running)
            with _active_task['lock']:
                current_thread = _active_task.get('thread')
                if current_thread is not None and current_thread.ident == my_thread_id:
                    _active_task['running'] = False
                else:
                    # A new task has already started — don't interfere with it
                    print(f'[LLM] Old agent thread (id={my_thread_id}) finished after new task started, not resetting running state')
            event_queue.put(None)

    # Start the agent in a background thread
    agent_thread = threading.Thread(target=_run_agent, daemon=True)
    agent_thread.start()
    with _active_task['lock']:
        _active_task['thread'] = agent_thread

    def generate():
        """Read from the shared queue and yield SSE events to this client."""
        q = None
        my_event_queue = event_queue  # capture our queue ref for safe cleanup
        try:
            with _active_task['lock']:
                q = _active_task['event_queue']
            while True:
                try:
                    event = q.get(timeout=30)
                except queue.Empty:
                    yield "data: {\"type\":\"keepalive\"}\n\n"
                    continue
                if event is None:  # sentinel = done
                    break
                yield event
        except GeneratorExit:
            pass
        finally:
            with _active_task['lock']:
                _active_task['subscribers'] -= 1
                if _active_task['subscribers'] <= 0:
                    # Full cleanup when last subscriber disconnects
                    # But only if we're still the active task (a new task may have started)
                    current_queue = _active_task.get('event_queue')
                    if current_queue is my_event_queue:
                        # Safe to clean up — no new task has taken over
                        _active_task['running'] = False
                        _active_task['cancelled'] = False
                        _active_task['conv_id'] = None
                        _active_task['message'] = None
                        _active_task['started_at'] = None
                        _active_task['event_queue'] = None
                        _active_task['event_buffer'] = None
                        _active_task['thread'] = None
                    else:
                        # A new task has started — don't interfere with its state
                        print(f'[LLM] Old SSE subscriber disconnecting after new task started, not cleaning up state')

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@bp.route('/api/chat/task/stop', methods=['POST'])
def stop_task():
    """Request cancellation of the currently running AI task."""
    with _active_task['lock']:
        if not _active_task['running']:
            return jsonify({'error': 'No task is running'}), 404
        _active_task['cancelled'] = True

    # Also stop any running terminal process that was started by the agent
    # The agent loop will check _active_task['cancelled'] and break out
    # Force-stop any running processes in utils.running_processes
    from utils import running_processes, stop_process
    for pid, info in list(running_processes.items()):
        if info.get('running'):
            try:
                stop_process(pid)
            except Exception:
                pass

    # Force cleanup after a short delay as a safety net.
    # The normal cleanup path is: agent thread finishes → running=False,
    # then SSE subscriber disconnects → subscribers=0 → full cleanup.
    # But if the SSE client already disconnected before we got here,
    # or if the agent thread hangs, the task state may never get cleaned up.
    # This delayed cleanup ensures the task is always marked as stopped.
    def _force_cleanup():
        time.sleep(3)  # Give the normal cleanup path a chance first
        with _active_task['lock']:
            if _active_task['running'] and _active_task.get('cancelled'):
                print('[LLM] Force-cleaning up task state after stop request (normal cleanup did not run)')
                _active_task['running'] = False
                _active_task['cancelled'] = False
                _active_task['conv_id'] = None
                _active_task['message'] = None
                _active_task['started_at'] = None
                _active_task['event_queue'] = None
                _active_task['event_buffer'] = None
                _active_task['thread'] = None
                _active_task['subscribers'] = 0

    cleanup_thread = threading.Thread(target=_force_cleanup, daemon=True)
    cleanup_thread.start()

    return jsonify({'ok': True, 'message': 'Task cancellation requested'})


@bp.route('/api/chat/task/status', methods=['GET'])
def get_task_status():
    """Check if there is an active running task."""
    with _active_task['lock']:
        if _active_task['running']:
            return jsonify({
                'running': True,
                'conv_id': _active_task['conv_id'],
                'started_at': _active_task['started_at'],
                'elapsed': time.time() - _active_task['started_at'] if _active_task['started_at'] else 0,
            })
        return jsonify({'running': False})


@bp.route('/api/chat/task/stream', methods=['GET'])
def task_reconnect_stream():
    """Reconnect to a running task. First sends buffered events, then subscribes to live events."""
    with _active_task['lock']:
        if not _active_task['running']:
            return jsonify({'error': 'No active task'}), 404

        q = _active_task['event_queue']
        my_event_queue = q  # capture for safe cleanup
        # Snapshot the ring buffer for catch-up
        buffered = list(_active_task['event_buffer'])
        _active_task['subscribers'] += 1

    def generate():
        try:
            # First replay all buffered (historical) events
            for event in buffered:
                yield event

            # Then subscribe to live events
            while True:
                try:
                    event = q.get(timeout=30)
                except queue.Empty:
                    yield "data: {\"type\":\"keepalive\"}\n\n"
                    continue
                if event is None:  # sentinel = done
                    break
                # Only yield if it's not already in the buffer (i.e., it's a new event)
                yield event
        except GeneratorExit:
            pass
        finally:
            with _active_task['lock']:
                _active_task['subscribers'] -= 1
                if _active_task['subscribers'] <= 0:
                    # Full cleanup when last subscriber disconnects
                    # But only if we're still the active task (a new task may have started)
                    current_queue = _active_task.get('event_queue')
                    if current_queue is my_event_queue:
                        _active_task['running'] = False
                        _active_task['cancelled'] = False
                        _active_task['conv_id'] = None
                        _active_task['message'] = None
                        _active_task['started_at'] = None
                        _active_task['event_queue'] = None
                        _active_task['event_buffer'] = None
                        _active_task['thread'] = None

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@bp.route('/api/tools', methods=['GET'])
def list_agent_tools():
    """List all available agent tools with their schemas."""
    tools_info = []
    for t in AGENT_TOOLS:
        f = t.get('function', {})
        tools_info.append({
            'name': f.get('name', ''),
            'description': f.get('description', ''),
            'parameters': f.get('parameters', {}),
        })
    return jsonify({'tools': tools_info})

@bp.route('/api/llm/config', methods=['GET'])
@handle_error
def get_llm_config():
    cfg = load_llm_config()
    # Mask API keys in models
    for m in cfg.get('models', []):
        key = m.get('api_key', '')
        if key:
            m['api_key_masked'] = key[:8] + '...' + key[-4:] if len(key) > 12 else '***'
        else:
            m['api_key_masked'] = ''
    return jsonify(cfg)

@bp.route('/api/llm/config', methods=['POST'])
@handle_error
def update_llm_config():
    config = request.json
    save_llm_config(config)
    return jsonify({'ok': True})


@bp.route('/api/llm/test', methods=['POST'])
def test_llm_config():
    """Test a specific model configuration or the active one."""
    try:
        data = request.json or {}
        # If a model index is provided, test that specific model
        if data.get('model_index') is not None:
            all_config = load_llm_config()
            models = all_config.get('models', [])
            idx = int(data['model_index'])
            if 0 <= idx < len(models):
                llm_config = dict(models[idx])
                llm_config['system_prompt'] = all_config.get('system_prompt', '')
            else:
                return jsonify({'ok': False, 'error': f'Invalid model index: {idx}'})
        else:
            llm_config = get_active_llm_config()

        api_type = llm_config.get('api_type', 'openai')
        api_key = llm_config.get('api_key', '')
        api_base = (llm_config.get('api_base') or '').rstrip('/')
        model = llm_config.get('model', 'gpt-4o-mini')

        # Ollama local mode does not require an API key
        if not api_key and api_type != 'ollama':
            return jsonify({'ok': False, 'error': 'API key not configured'})

        # Build endpoint URL and headers based on api_type
        try:
            url, headers = _get_llm_endpoint(llm_config, model)
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)})

        payload = {
            'model': model,
            'messages': [{'role': 'user', 'content': 'Hi, reply with just "OK".'}],
            'max_tokens': 500,
            'stream': False,
        }

        body_bytes = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        req = urllib.request.Request(url, body_bytes, headers=headers, method='POST')
        with _urllib_opener.open(req, timeout=60) as resp:
            resp_body = resp.read().decode()
            try:
                result = json.loads(resp_body)
            except (json.JSONDecodeError, ValueError) as je:
                return jsonify({'ok': False, 'error': f'API returned non-JSON response (api_type={api_type}): {str(je)}'})

        model_used = model
        reply_content = ''
        try:
            model_used = result.get('model', model)
            usage = result.get('usage', {})
            tokens = usage.get('total_tokens', 0)
            # Extract actual reply content from the response
            choices = result.get('choices', [])
            if choices:
                msg = choices[0].get('message', {})
                reply_content = msg.get('content', '') or ''
        except Exception:
            tokens = 0

        # Warn if model returned empty content (may happen with reasoning models if max_tokens too low)
        if not reply_content:
            _empty_raw_debug = {
                'finish_reason': choices[0].get('finish_reason', '?') if choices else 'no_choices',
                'model': model_used,
                'tokens': tokens,
                'raw_response_first_1000': resp_body[:1000],
            }
            return jsonify({
                'ok': True,
                'model': model_used,
                'tokens': tokens,
                'reply': '',
                'warning': 'Model returned empty content. If using a reasoning model, the max_tokens setting may be too low.',
                'debug': _empty_raw_debug,
            })

        return jsonify({'ok': True, 'model': model_used, 'tokens': tokens, 'reply': reply_content[:200]})
    except urllib.error.HTTPError as e:
        body = ''
        try:
            body = e.read().decode()[:500]
        except Exception:
            pass
        # Try to extract JSON error message from the body
        try:
            err_data = json.loads(body)
            err_msg = err_data.get('error', {})
            if isinstance(err_msg, dict):
                err_msg = err_msg.get('message', body[:300])
            else:
                err_msg = str(err_msg) or body[:300]
        except Exception:
            err_msg = body[:300]
        return jsonify({'ok': False, 'error': f'HTTP {e.code}: {err_msg}'})
    except urllib.error.URLError as e:
        reason = str(e.reason)
        if 'refused' in reason.lower():
            hint = '. Check if the API server is running.'
        elif 'name or service not known' in reason.lower():
            hint = '. Check the API base URL.'
        else:
            hint = ''
        return jsonify({'ok': False, 'error': f'Connection failed: {reason}{hint}'})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})
