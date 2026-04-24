"""
MusIDE - Virtual Environment & Compiler API routes.
"""

import os
import json
import subprocess
from flask import Blueprint, jsonify, request
from utils import handle_error, load_config, save_config, WORKSPACE, shlex_quote, IS_WINDOWS, get_default_compiler

bp = Blueprint('venv', __name__)

# Common venv directory names to check directly (fast path)
_VENV_DIR_NAMES = ('.venv', 'venv', 'env', '.env')

# Directories to always skip during os.walk (Windows-heavy dirs, etc.)
_SKIP_DIRS = frozenset({
    '__pycache__', 'node_modules', '.git', '.hg', '.svn',
    'AppData', 'Application Data', 'Program Files', 'Program Files (x86)',
    'Windows', 'System32', 'ProgramData',
    # Common large dirs on Windows
    '.vscode', '.idea', 'dist', 'build', '.tox', '.mypy_cache',
    '.pytest_cache', '.ruff_cache', 'htmlcov', '.coverage',
})


def _get_effective_base(config=None):
    """Get the effective base directory for venv operations.
    When a project is open, returns the project directory.
    Otherwise returns the workspace root."""
    if config is None:
        config = load_config()
    base = config.get('workspace', WORKSPACE)
    project = config.get('project', None)
    if project:
        project_dir = os.path.join(base, project)
        if os.path.isdir(project_dir):
            return project_dir
    return base


def _is_venv_dir(path):
    """Check if a directory is a valid Python virtual environment."""
    try:
        return os.path.isfile(os.path.join(path, 'pyvenv.cfg'))
    except (OSError, ValueError):
        return False


def _paths_same(p1, p2):
    """Compare two paths in a platform-aware way (case-insensitive on Windows)."""
    if not p1 or not p2:
        return False
    if IS_WINDOWS:
        return os.path.normcase(os.path.normpath(p1)) == os.path.normcase(os.path.normpath(p2))
    return os.path.normpath(p1) == os.path.normpath(p2)


def _scan_venv_dirs(scan_base):
    """Scan for virtual environments in the given directory tree.
    
    Uses a two-phase approach:
    1. Fast path: directly check common venv names in the root
    2. Slow path: os.walk with error handling (Windows PermissionError safe)
    """
    venvs = []
    seen = set()

    def _add_venv(root, base):
        """Add a venv if valid and not already seen."""
        try:
            real = os.path.realpath(root)
            key = os.path.normcase(real)
            if key in seen:
                return
            if _is_venv_dir(root):
                seen.add(key)
                rel = os.path.relpath(root, base)
                return {
                    'path': rel,
                    'full_path': root,
                    'name': os.path.basename(root),
                }
        except (OSError, ValueError):
            pass
        return None

    # Phase 1: Fast path - check common venv names directly in scan_base
    for name in _VENV_DIR_NAMES:
        candidate = os.path.join(scan_base, name)
        try:
            result = _add_venv(candidate, scan_base)
            if result:
                venvs.append(result)
        except (OSError, ValueError):
            pass

    # Phase 2: Walk the tree (depth-limited, error-tolerant)
    try:
        for root, dirs, files in os.walk(scan_base, onerror=lambda _: None):
            # Skip known non-venv directories to speed up scanning
            skip = _SKIP_DIRS
            # Allow .venv even though it starts with '.'
            dirs[:] = [d for d in dirs
                       if (d in _VENV_DIR_NAMES or
                           (not d.startswith('.') and d not in skip))]

            # Limit scan depth
            rel_prefix = root[len(scan_base):]
            if rel_prefix and rel_prefix[0] == os.sep:
                rel_prefix = rel_prefix[1:]
            depth = rel_prefix.count(os.sep) if rel_prefix else 0
            if depth > 2:
                # Don't descend further
                dirs[:] = []
                continue

            # Check for pyvenv.cfg
            if 'pyvenv.cfg' in files:
                result = _add_venv(root, scan_base)
                if result:
                    venvs.append(result)
    except (OSError, ValueError):
        pass

    return venvs


@bp.route('/api/compilers', methods=['GET'])
@handle_error
def list_compilers():
    compilers = []
    checks = [
        ('python3', 'Python 3', 'python3 --version'),
        ('python', 'Python', 'python --version'),
        ('node', 'Node.js', 'node --version'),
        ('gcc', 'GCC C', 'gcc --version'),
        ('g++', 'G++ C++', 'g++ --version'),
        ('go', 'Go', 'go version'),
        ('rustc', 'Rust', 'rustc --version'),
        ('ruby', 'Ruby', 'ruby --version'),
        ('lua', 'Lua', 'lua -v'),
    ]
    # On Windows, don't check bash; add cmd and powershell checks
    if IS_WINDOWS:
        checks.append(('cmd', 'CMD', 'cmd /c ver'))
    else:
        checks.append(('bash', 'Bash', 'bash --version'))
    for cmd, name, version_cmd in checks:
        try:
            result = subprocess.run(version_cmd, shell=True, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                version = result.stdout.strip().split('\n')[0]
                compilers.append({'id': cmd, 'name': name, 'version': version})
        except:
            pass
    return jsonify({'compilers': compilers})


@bp.route('/api/venv/create', methods=['POST'])
@handle_error
def create_venv():
    data = request.json
    path = data.get('path', '')
    config = load_config()
    base = config.get('workspace', WORKSPACE)

    # When a project is open, create venv inside the project directory
    effective_base = _get_effective_base(config)

    if not path:
        path = '.venv'

    # Resolve path relative to the effective base (project dir), not CWD
    if not os.path.isabs(path):
        target = os.path.realpath(os.path.join(effective_base, path))
    else:
        target = os.path.realpath(path)

    # Check if venv already exists at target
    if _is_venv_dir(target):
        config['venv_path'] = target
        save_config(config)
        return jsonify({'ok': True, 'venv_path': target, 'already_exists': True})

    venv_python = get_default_compiler()
    # Run venv creation SYNCHRONOUSLY so the caller can activate immediately.
    # The async run_process() returns before venv is ready, causing activate to fail.
    try:
        result = subprocess.run(
            f'{venv_python} -m venv {shlex_quote(target)}',
            shell=True, cwd=effective_base,
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            stderr = (result.stderr or '').strip()
            return jsonify({'ok': False, 'error': f'创建虚拟环境失败: {stderr}'}), 500

        # Verify venv was created successfully
        if not _is_venv_dir(target):
            return jsonify({'ok': False, 'error': '虚拟环境创建完成但验证失败'}), 500

        config['venv_path'] = target
        save_config(config)
        return jsonify({'ok': True, 'venv_path': target})
    except subprocess.TimeoutExpired:
        return jsonify({'ok': False, 'error': '创建虚拟环境超时（120秒）'}), 500
    except Exception as e:
        return jsonify({'ok': False, 'error': f'创建虚拟环境异常: {e}'}), 500


@bp.route('/api/venv/list', methods=['GET'])
@handle_error
def list_venvs():
    config = load_config()
    base = config.get('workspace', WORKSPACE)

    # When a project is open, only scan the project directory
    scan_base = _get_effective_base(config)

    # Scan for venvs (fast + slow path, error-tolerant)
    found_venvs = _scan_venv_dirs(scan_base)

    # Mark active venv (case-insensitive on Windows)
    current_venv = config.get('venv_path', '')
    for v in found_venvs:
        v['active'] = _paths_same(v['full_path'], current_venv)

    # Check if current venv is stale
    project = config.get('project', None)
    cleared_stale = False
    if current_venv:
        stale = False
        if not _is_venv_dir(current_venv):
            stale = True
        elif project:
            project_dir = os.path.realpath(os.path.join(base, project))
            # Strict: venv MUST be inside the current project directory
            if not current_venv.startswith(project_dir + os.sep) and not _paths_same(current_venv, project_dir):
                stale = True

        if stale:
            config['venv_path'] = ''
            save_config(config)
            current_venv = ''
            cleared_stale = True

    # Re-read current venv after possible stale clear
    if not current_venv:
        current_venv = config.get('venv_path', '')

    return jsonify({
        'venvs': found_venvs,
        'current': current_venv,
        'cleared_stale': cleared_stale,
        'has_project': bool(project),
    })


@bp.route('/api/venv/activate', methods=['POST'])
@handle_error
def activate_venv():
    data = request.json
    path = data.get('path', '')
    config = load_config()
    base = config.get('workspace', WORKSPACE)

    if not path:
        return jsonify({'error': 'Path required'}), 400

    # Resolve relative paths from workspace root (matching list_venvs output format)
    if not os.path.isabs(path):
        target = os.path.realpath(os.path.join(base, path))
    else:
        target = os.path.realpath(path)

    # Security: Only allow activating venvs within the current project directory
    # When a project is open, the venv MUST be inside the project directory
    project = config.get('project', None)
    if project:
        project_dir = os.path.realpath(os.path.join(base, project))
        if not _paths_same(target, project_dir) and not target.startswith(project_dir + os.sep):
            return jsonify({'error': '只能激活当前项目目录内的虚拟环境'}), 400

    if _is_venv_dir(target):
        config['venv_path'] = target
        save_config(config)
        return jsonify({'ok': True, 'venv_path': target})
    return jsonify({'error': 'Invalid venv directory'}), 400


@bp.route('/api/venv/packages', methods=['GET'])
@handle_error
def list_packages():
    config = load_config()
    venv_path = config.get('venv_path', '')
    if venv_path and os.path.exists(venv_path):
        # Try multiple strategies to find pip in the venv
        pip_candidates = []
        if IS_WINDOWS:
            pip_candidates = [
                os.path.join(venv_path, 'Scripts', 'pip.exe'),
                os.path.join(venv_path, 'Scripts', 'pip3.exe'),
                os.path.join(venv_path, 'Scripts', 'python.exe') + ' -m pip',
            ]
        else:
            pip_candidates = [
                os.path.join(venv_path, 'bin', 'pip'),
                os.path.join(venv_path, 'bin', 'pip3'),
                os.path.join(venv_path, 'bin', 'python') + ' -m pip',
                os.path.join(venv_path, 'bin', 'python3') + ' -m pip',
            ]

        for pip_cmd in pip_candidates:
            # For "python -m pip" style, the file check doesn't apply
            is_m_pip = ' -m pip' in pip_cmd
            if not is_m_pip and not os.path.exists(pip_cmd):
                continue
            try:
                result = subprocess.run(
                    f'{pip_cmd} list --format=json', shell=True,
                    capture_output=True, text=True, timeout=30,
                    encoding='utf-8', errors='replace'
                )
                if result.returncode == 0:
                    try:
                        packages = json.loads(result.stdout)
                        return jsonify({'packages': packages})
                    except (json.JSONDecodeError, ValueError):
                        pass
            except Exception:
                continue

        # Last resort: try system python with venv's site-packages
        try:
            venv_python = os.path.join(venv_path, 'bin', 'python3') if not IS_WINDOWS else os.path.join(venv_path, 'Scripts', 'python.exe')
            if os.path.exists(venv_python):
                result = subprocess.run(
                    [venv_python, '-m', 'pip', 'list', '--format=json'],
                    capture_output=True, text=True, timeout=30,
                    encoding='utf-8', errors='replace'
                )
                if result.returncode == 0:
                    try:
                        packages = json.loads(result.stdout)
                        return jsonify({'packages': packages})
                    except (json.JSONDecodeError, ValueError):
                        pass
        except Exception:
            pass
    return jsonify({'packages': []})
