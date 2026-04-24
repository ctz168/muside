"""
MusIDE - Git API routes.
"""

import os
import shlex
import subprocess
import time
import threading
from datetime import datetime
from flask import Blueprint, jsonify, request
from utils import handle_error, load_config, save_config, WORKSPACE, shlex_quote, IS_WINDOWS

bp = Blueprint('git', __name__)


def resolve_cwd():
    """Resolve the git working directory from the request (query args or JSON body).

    The frontend sends the *relative* path inside the workspace (e.g. 'myrepo').
    This function joins it with the configured workspace to get an absolute path.
    If no path is provided, the project directory is used (if a project is open).
    Falls back to workspace root only if no project is set.
    """
    try:
        config = load_config()
    except Exception:
        config = {}
    base = config.get('workspace', WORKSPACE)

    # Try query string first (GET requests)
    rel = request.args.get('path') or request.args.get('cwd', '')
    # Fall back to JSON body (POST requests)
    if not rel:
        try:
            data = request.json or {}
            rel = data.get('path') or data.get('cwd', '')
        except Exception:
            pass

    if rel and rel.strip():
        target = os.path.realpath(os.path.join(base, rel.strip()))
        # Security: must stay under workspace
        if target.startswith(os.path.realpath(base)):
            return target

    # No explicit path: use project directory if a project is open
    project = config.get('project', None)
    if project:
        project_dir = os.path.realpath(os.path.join(base, project))
        if os.path.isdir(project_dir) and project_dir.startswith(os.path.realpath(base)):
            return project_dir

    return base


def git_cmd(args, cwd=None, timeout=60):
    try:
        config = load_config()
    except Exception:
        config = {}
    base = cwd or config.get('workspace', WORKSPACE)
    cmd = f'git -C {shlex_quote(base)} {args}'
    try:
        # On Windows, shell=True with cmd.exe can have issues with certain characters.
        # Use shell=False with list args when possible.
        if IS_WINDOWS:
            # Use shlex.split to properly handle quoted arguments (e.g. -m "msg", --format="...")
            # Without this, args.split() would break quoted strings containing spaces
            full_cmd = ['git', '-C', base] + shlex.split(args)
            result = subprocess.run(
                full_cmd, shell=False, capture_output=True,
                text=True, timeout=timeout, encoding='utf-8', errors='replace'
            )
        else:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return {'ok': result.returncode == 0, 'stdout': result.stdout, 'stderr': result.stderr, 'code': result.returncode}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'stdout': '', 'stderr': 'Command timed out', 'code': -1}
    except Exception as e:
        return {'ok': False, 'stdout': '', 'stderr': str(e), 'code': -1}


@bp.route('/api/git/init', methods=['POST'])
def git_init():
    """Initialize a new git repository in the current directory."""
    # Step 1: resolve working directory
    try:
        cwd = resolve_cwd()
        print(f'[git/init] cwd={cwd}')
    except Exception as e:
        print(f'[git/init] resolve_cwd error: {e}')
        return jsonify({'error': f'路径解析失败: {str(e)}'}), 400

    # Prevent git init on workspace root
    try:
        config = load_config()
    except Exception:
        config = {}
    base = config.get('workspace', WORKSPACE)
    if os.path.realpath(cwd) == os.path.realpath(base):
        return jsonify({'error': '禁止在工作区根目录初始化 Git 仓库，请先打开一个项目'}), 400

    # Step 2: verify git is available
    try:
        git_ver = subprocess.run('git --version', shell=True, capture_output=True, text=True, timeout=10)
        if git_ver.returncode != 0:
            return jsonify({'error': f'Git 未安装或不可用: {(git_ver.stderr or git_ver.stdout).strip()}'}), 500
        print(f'[git/init] git version: {git_ver.stdout.strip()}')
    except Exception as e:
        return jsonify({'error': f'Git 检查失败: {str(e)}'}), 500

    # Step 3: ensure target directory exists
    if not os.path.isdir(cwd):
        print(f'[git/init] directory does not exist, creating: {cwd}')
        try:
            os.makedirs(cwd, exist_ok=True)
        except Exception as e:
            return jsonify({'error': f'无法创建目录 {cwd}: {str(e)}'}), 400

    # Step 4: check write permission
    test_file = os.path.join(cwd, '.git_write_test')
    try:
        with open(test_file, 'w', encoding='utf-8') as f:
            f.write('test')
        os.remove(test_file)
    except Exception as e:
        return jsonify({'error': f'目录没有写入权限: {str(e)}'}), 400

    # Step 5: run git init
    r = git_cmd('init', cwd=cwd)
    print(f'[git/init] result: ok={r["ok"]}, code={r["code"]}, stderr={r["stderr"][:200]}')

    if not r['ok']:
        stderr = r['stderr'].strip()
        if 'already' in stderr.lower() or 'reinitialized' in stderr.lower():
            return jsonify({'ok': True, 'path': cwd, 'note': 'Git 仓库已存在'})
        return jsonify({'error': f'Git 初始化失败: {stderr or "未知错误"}'}), 500

    # Step 6: set safe defaults for mobile/termux environments
    git_cmd('config user.name "MusIDE"', cwd=cwd)
    git_cmd('config user.email "muside@local"', cwd=cwd)

    print(f'[git/init] success, cwd={cwd}')
    return jsonify({'ok': True, 'path': cwd})


@bp.route('/api/git/status', methods=['GET'])
@handle_error
def git_status():
    cwd = resolve_cwd()

    # Check if this is a git repo at all
    check = git_cmd('rev-parse --is-inside-work-tree', cwd=cwd)
    if not check['ok']:
        return jsonify({
            'branch': '',
            'changed': [],
            'staged': [],
            'untracked': [],
            'not_a_repo': True,
            'error': 'Not a git repository',
        })

    r = git_cmd('status --porcelain -b', cwd=cwd)
    if not r['ok']:
        return jsonify({'error': r['stderr']}), 500
    lines = r['stdout'].strip().split('\n') if r['stdout'].strip() else []
    branch = ''
    changed = []
    staged = []
    untracked = []
    for line in lines:
        if line.startswith('##'):
            branch = line[2:].strip().split('...')[0]
            continue
        if len(line) >= 2:
            status = line[:2]
            filepath = line[3:]
            if status[0] == '?' and status[1] == '?':
                untracked.append({'path': filepath, 'status': 'untracked'})
            elif status[0] != ' ':
                staged.append({'path': filepath, 'status': 'staged', 'change': status[0]})
            else:
                changed.append({'path': filepath, 'status': 'modified', 'change': status[1]})
    return jsonify({
        'branch': branch,
        'changed': changed,
        'staged': staged,
        'untracked': untracked,
    })


@bp.route('/api/git/log', methods=['GET'])
@handle_error
def git_log():
    count = request.args.get('count', 20)
    offset = request.args.get('offset', 0)
    try:
        count = int(count)
        offset = int(offset)
    except (ValueError, TypeError):
        count = 20
        offset = 0
    cwd = resolve_cwd()
    r = git_cmd(f'log -n {count} --skip {offset} --format="%H|%an|%ae|%at|%s"', cwd=cwd)
    if not r['ok']:
        return jsonify({'commits': [], 'error': r['stderr']})
    commits = []
    for line in r['stdout'].strip().split('\n'):
        if line and '|' in line:
            parts = line.split('|', 4)
            if len(parts) == 5:
                commits.append({
                    'hash': parts[0][:8],
                    'full_hash': parts[0],
                    'author': parts[1],
                    'email': parts[2],
                    'date': datetime.fromtimestamp(int(parts[3])).isoformat(),
                    'message': parts[4],
                })
    return jsonify({'commits': commits, 'offset': offset, 'count': len(commits)})


@bp.route('/api/git/branch', methods=['GET'])
@handle_error
def git_branch():
    cwd = resolve_cwd()
    r = git_cmd('branch -a', cwd=cwd)
    if not r['ok']:
        return jsonify({'branches': [], 'error': r['stderr']})
    branches = []
    current = ''
    for line in r['stdout'].strip().split('\n'):
        if line:
            active = line.startswith('*')
            name = line.lstrip('* ').strip()
            if active:
                current = name
            branches.append({'name': name, 'active': active})
    return jsonify({'branches': branches, 'current': current})


@bp.route('/api/git/checkout', methods=['POST'])
@handle_error
def git_checkout():
    data = request.json
    branch = data.get('branch', '')
    if not branch:
        return jsonify({'error': 'Branch name required'}), 400
    cwd = resolve_cwd()
    r = git_cmd(f'checkout {shlex_quote(branch)}', cwd=cwd)
    if not r['ok']:
        return jsonify({'error': r['stderr']}), 500
    return jsonify({'ok': True})


@bp.route('/api/git/add', methods=['POST'])
@handle_error
def git_add():
    try:
        data = request.json or {}
    except:
        data = {}
    paths = data.get('paths', [])
    cwd = resolve_cwd()
    if not paths:
        r = git_cmd('add -A', cwd=cwd)
    else:
        files = ' '.join(shlex_quote(p) for p in paths)
        r = git_cmd(f'add {files}', cwd=cwd)
    return jsonify({'ok': r['ok'], 'stderr': r['stderr']})


@bp.route('/api/git/commit', methods=['POST'])
@handle_error
def git_commit():
    try:
        data = request.json or {}
    except:
        data = {}
    message = data.get('message', '')
    if not message:
        return jsonify({'error': 'Commit message required'}), 400
    cwd = resolve_cwd()
    r = git_cmd(f'commit -m {shlex_quote(message)}', cwd=cwd)
    if not r['ok']:
        # git often puts the error in stdout (e.g. "nothing to commit"),
        # fallback to stderr, then to a generic message
        err_msg = (r['stderr'] or r['stdout'] or '').strip()
        if not err_msg:
            err_msg = f'Commit failed (exit code {r.get("code", "?")})'
        return jsonify({'error': err_msg}), 500
    return jsonify({'ok': True, 'stdout': r.get('stdout', '')})


@bp.route('/api/git/delete-file', methods=['POST'])
@handle_error
def git_delete_file():
    """Delete a file from the working tree using git rm (or plain rm for untracked).

    Accepts:
        - filepath: file path as reported by git status (relative to repo root)
    """
    data = request.json or {}
    filepath = data.get('filepath', '')
    if not filepath:
        return jsonify({'error': 'File path required'}), 400

    cwd = resolve_cwd()

    # Determine if the file is tracked by git
    check = git_cmd(f'ls-files --error-unmatch -- {shlex_quote(filepath)}', cwd=cwd)
    if check['ok']:
        # File is tracked — use git rm
        r = git_cmd(f'rm -f -- {shlex_quote(filepath)}', cwd=cwd)
    else:
        # File is untracked — just delete from filesystem
        import os
        full = os.path.join(cwd, filepath)
        if os.path.exists(full):
            if os.path.isdir(full):
                import shutil
                shutil.rmtree(full)
            else:
                os.remove(full)
            r = {'ok': True, 'stdout': '', 'stderr': ''}
        else:
            return jsonify({'error': f'File not found: {filepath}'}), 404

    if not r['ok']:
        err_msg = (r['stderr'] or r['stdout'] or '').strip()
        if not err_msg:
            err_msg = 'Delete failed'
        return jsonify({'error': err_msg}), 500
    return jsonify({'ok': True})


import re


def _strip_token_from_url(url):
    """Remove token from a git URL like https://ghp_xxx@github.com/user/repo.git
    Returns the clean URL: https://github.com/user/repo.git
    """
    if '@' not in url:
        return url
    # Handle https://token@host/path
    m = re.match(r'^(https?://)[^/@]+@(.+)$', url)
    if m:
        return m.group(1) + m.group(2)
    return url


def _inject_token_to_url(url, token):
    """Inject a GitHub token into a clean HTTPS git URL.
    https://github.com/user/repo.git -> https://token@github.com/user/repo.git
    """
    clean = _strip_token_from_url(url)
    return clean.replace('https://', f'https://{token}@')


def _setup_git_auth(cwd, token):
    """Configure git url.insteadOf so all https://github.com operations
    automatically use the token, without modifying the stored remote URL.
    This avoids URL parsing issues in Termux/mobile git versions."""
    if not token or not cwd:
        return
    try:
        # Set insteadOf so git replaces https://github.com/ with token-auth URL
        auth_url = f'https://{token}@github.com/'
        git_cmd(f'config url.{shlex_quote(auth_url)}.insteadOf https://github.com/', cwd=cwd, timeout=10)
        print(f'[git] configured url.insteadOf for auth in {cwd}')
    except Exception as e:
        print(f'[git] Warning: failed to configure insteadOf: {e}')


@bp.route('/api/git/push', methods=['POST'])
@handle_error
def git_push():
    data = request.json
    remote = data.get('remote', 'origin')
    branch = data.get('branch', '')
    set_upstream = data.get('set_upstream', False)

    cwd = resolve_cwd()

    # Ensure git auth is configured (uses url.insteadOf, no URL mangling)
    try:
        config = load_config()
        token = config.get('github_token', '')
        if token:
            _setup_git_auth(cwd, token)
    except Exception as e:
        print(f'[git/push] Warning: auth setup failed: {e}')

    cmd = f'push {remote} {branch}'
    if set_upstream:
        cmd = f'push -u {remote} {branch}'
    r = git_cmd(cmd, cwd=cwd, timeout=120)

    return jsonify({'ok': r['ok'], 'stdout': r['stdout'], 'stderr': r['stderr']})


@bp.route('/api/git/pull', methods=['POST'])
@handle_error
def git_pull():
    data = request.json
    remote = data.get('remote', 'origin')
    branch = data.get('branch', '')
    cwd = resolve_cwd()

    # Ensure git auth is configured (uses url.insteadOf, no URL mangling)
    try:
        config = load_config()
        token = config.get('github_token', '')
        if token:
            _setup_git_auth(cwd, token)
    except Exception as e:
        print(f'[git/pull] Warning: auth setup failed: {e}')

    r = git_cmd(f'pull {remote} {branch}', cwd=cwd, timeout=120)

    return jsonify({'ok': r['ok'], 'stdout': r['stdout'], 'stderr': r['stderr']})


@bp.route('/api/git/clone', methods=['POST'])
@handle_error
def git_clone():
    data = request.json
    url = data.get('url', '')
    path = data.get('path', '')
    config = load_config()
    base = config.get('workspace', WORKSPACE)

    if not url:
        return jsonify({'error': 'URL required'}), 400

    # Remember if the URL has a token embedded
    clean_url = _strip_token_from_url(url)

    if path:
        target = os.path.join(base, path)
    else:
        # Extract repo name from URL
        name = url.rstrip('/').split('/')[-1]
        if name.endswith('.git'):
            name = name[:-4]
        target = os.path.join(base, name)

    r = git_cmd(f'clone {shlex_quote(url)} {shlex_quote(target)}', cwd=base, timeout=300)
    if r['ok']:
        # If clone URL had a token, strip it from the stored remote URL
        # and set up url.insteadOf for future git operations
        if '@' in url.split('://')[-1].split('/')[0]:
            git_cmd(f'remote set-url origin {shlex_quote(clean_url)}', cwd=target, timeout=10)
            print(f'[git/clone] stripped token from remote URL in {target}')
            # Configure insteadOf for future push/pull
            token = config.get('github_token', '')
            if not token:
                # Try to extract token from the original clone URL
                m = re.match(r'^https://([^/@]+)@', url)
                if m:
                    token = m.group(1)
            if token:
                _setup_git_auth(target, token)

        return jsonify({'ok': True, 'path': os.path.relpath(target, base)})
    return jsonify({'error': r['stderr']}), 500


@bp.route('/api/git/remote', methods=['GET'])
@handle_error
def git_remote():
    cwd = resolve_cwd()
    r = git_cmd('remote -v', cwd=cwd)
    if not r['ok']:
        return jsonify({'remotes': []})
    remotes = []
    for line in r['stdout'].strip().split('\n'):
        if line:
            parts = line.split('\t')
            if len(parts) == 2:
                name, url = parts
                url = url.split(' ')[0]
                remotes.append({'name': name.strip(), 'url': url})
    return jsonify({'remotes': remotes})


@bp.route('/api/git/diff', methods=['GET'])
@handle_error
def git_diff():
    staged = request.args.get('staged', 'false').lower() == 'true'
    filepath = request.args.get('file', request.args.get('filepath', ''))
    cwd = resolve_cwd()
    cmd = 'diff --cached' if staged else 'diff'
    if filepath:
        cmd += f' -- {shlex_quote(filepath)}'
    r = git_cmd(cmd, cwd=cwd)
    return jsonify({'ok': r['ok'], 'diff': r['stdout'], 'stderr': r['stderr']})


@bp.route('/api/git/stash', methods=['POST'])
@handle_error
def git_stash():
    data = request.json
    action = data.get('action', 'push')
    cwd = resolve_cwd()
    r = git_cmd(f'stash {action}', cwd=cwd)
    return jsonify({'ok': r['ok'], 'stdout': r['stdout'], 'stderr': r['stderr']})


@bp.route('/api/git/reset', methods=['POST'])
@handle_error
def git_reset():
    data = request.json
    mode = data.get('mode', 'soft')
    cwd = resolve_cwd()
    r = git_cmd(f'reset {mode} HEAD', cwd=cwd)
    return jsonify({'ok': r['ok'], 'stderr': r['stderr']})


@bp.route('/api/git/restore', methods=['POST'])
@handle_error
def git_restore():
    """Restore a file to its state in HEAD (discard working/staged changes).

    Accepts:
        - filepath: file path(s) to restore. If omitted, restores all files.
        - staged: if True, unstage the file (git restore --staged); default False.
    """
    data = request.json or {}
    filepath = data.get('filepath', '')
    staged = data.get('staged', False)
    cwd = resolve_cwd()

    cmd = 'restore'
    if staged:
        cmd += ' --staged'
    if filepath:
        cmd += f' -- {shlex_quote(filepath)}'

    r = git_cmd(cmd, cwd=cwd)
    return jsonify({'ok': r['ok'], 'stdout': r['stdout'], 'stderr': r['stderr']})


@bp.route('/api/git/commit-diff', methods=['GET'])
@handle_error
def git_commit_diff():
    """Get file list and/or diff for a specific commit.

    Query params:
        ref  — commit hash (required)
        file — optional; if provided, returns diff for that specific file only
    Returns:
        { files: [{path, status, additions, deletions}], diff: "..." }
        If ?file= is set, returns { file, diff } for just that file.
    """
    ref = request.args.get('ref', '').strip()
    filepath = request.args.get('file', '').strip()

    if not ref:
        return jsonify({'error': 'Commit ref required'}), 400

    cwd = resolve_cwd()

    if filepath:
        # Get diff for a specific file in this commit
        r = git_cmd(
            f'show {shlex_quote(ref)} -- {shlex_quote(filepath)}',
            cwd=cwd, timeout=30
        )
        return jsonify({
            'file': filepath,
            'diff': r['stdout'] if r['ok'] else '',
            'error': r['stderr'] if not r['ok'] else None,
        })
    else:
        # Get file list with stat (names + change summary)
        r_stat = git_cmd(
            f'show {shlex_quote(ref)} --stat --format=""',
            cwd=cwd, timeout=30
        )
        files = []
        if r_stat['ok'] and r_stat['stdout'].strip():
            for line in r_stat['stdout'].strip().split('\n'):
                line = line.strip()
                if not line:
                    continue
                # Parse lines like " path/to/file | 10 ++++++++---"
                # or " 3 files changed, 10 insertions(+), 5 deletions(-)"
                if '|' not in line or 'files changed' in line:
                    continue
                parts = line.rsplit('|', 1)
                if len(parts) == 2:
                    fpath = parts[0].strip()
                    stat_str = parts[1].strip()
                    # Parse additions/deletions from stat
                    additions = 0
                    deletions = 0
                    import re as _re
                    nums = _re.findall(r'(\d+)', stat_str)
                    if len(nums) >= 1:
                        additions = int(nums[0])
                    if len(nums) >= 2:
                        deletions = int(nums[1])
                    files.append({
                        'path': fpath,
                        'additions': additions,
                        'deletions': deletions,
                    })

        return jsonify({
            'ref': ref,
            'files': files,
        })


@bp.route('/api/git/checkout-commit', methods=['POST'])
@handle_error
def git_checkout_commit():
    """Checkout a specific commit hash (detached HEAD) or a file at a commit.

    If mode='reset-hard', performs git reset --hard <ref> to truly rollback.
    If mode='preview' (default), performs git checkout <ref> (detached HEAD, view-only).
    If filepath is provided, restores that file from the commit.
    """
    data = request.json or {}
    ref = data.get('ref', '')
    filepath = data.get('filepath', '')
    mode = data.get('mode', 'preview')  # 'preview' or 'reset-hard'
    cwd = resolve_cwd()

    if not ref:
        return jsonify({'error': 'Commit ref required'}), 400

    if filepath:
        # Restore a specific file from a commit: git checkout <ref> -- <file>
        cmd = f'checkout {shlex_quote(ref)} -- {shlex_quote(filepath)}'
    elif mode == 'reset-hard':
        # True rollback: move branch pointer to this commit
        cmd = f'reset --hard {shlex_quote(ref)}'
    else:
        # Preview mode (detached HEAD): git checkout <ref>
        cmd = f'checkout {shlex_quote(ref)}'

    r = git_cmd(cmd, cwd=cwd, timeout=120)
    if not r['ok']:
        return jsonify({'error': r['stderr'] or 'Operation failed'}), 500
    return jsonify({'ok': True, 'stdout': r['stdout'], 'stderr': r['stderr'], 'mode': mode})


# ==================== GitHub OAuth Device Flow ====================

# In-memory store for pending device code polls
_github_oauth_pending = {}  # device_code -> { thread, stop_event, result }

GITHUB_CLIENT_ID = 'Ov23lire87hKJVZJFw0d'  # MusIDE GitHub OAuth App
GITHUB_SCOPES = 'repo,read:org,gist'


def _make_github_request(url, data):
    """Make a GitHub API request and return the JSON response."""
    try:
        import urllib.request
        import json as _json
        req = urllib.request.Request(
            url,
            data=_json.dumps(data).encode('utf-8'),
            headers={
                'Accept': 'application/json',
                'Content-Type': 'application/json',
            }
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return _json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        return {'error': str(e)}


@bp.route('/api/git/github/auth/start', methods=['POST'])
@handle_error
def github_auth_start():
    """Step 1: Request a device code from GitHub.

    Returns the user_code and verification_uri for the user to authorize.
    """
    data = request.json or {}
    client_id = data.get('client_id', '') or GITHUB_CLIENT_ID
    scopes = data.get('scopes', '') or GITHUB_SCOPES

    resp = _make_github_request('https://github.com/login/device/code', {
        'client_id': client_id,
        'scope': scopes,
    })

    if 'error' in resp:
        error_detail = resp.get('error_description', resp.get('error', 'Failed to start GitHub auth'))
        http_code = 400
        # If the OAuth App doesn't exist (404), tell the frontend to fall back to token
        if 'Not Found' in str(resp.get('error', '')) or '404' in str(resp.get('error', '')):
            error_detail = 'GitHub OAuth 应用未配置，请使用 Token 方式登录'
            http_code = 404
        return jsonify({'error': error_detail, 'oauth_unavailable': True}), http_code

    device_code = resp.get('device_code', '')
    user_code = resp.get('user_code', '')
    verification_uri = resp.get('verification_uri', '')
    expires_in = resp.get('expires_in', 900)
    interval = resp.get('interval', 5)

    # Start background polling for the token
    stop_event = threading.Event()
    result_holder = {'token': None, 'done': False, 'error': None}

    def _poll():
        poll_interval = max(interval, 5)
        deadline = time.time() + expires_in
        while not stop_event.is_set() and time.time() < deadline:
            stop_event.wait(poll_interval)
            if stop_event.is_set():
                break
            token_resp = _make_github_request('https://github.com/login/oauth/access_token', {
                'client_id': client_id,
                'device_code': device_code,
                'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
            })
            if 'access_token' in token_resp:
                result_holder['token'] = token_resp['access_token']
                result_holder['done'] = True
                # Auto-save the token to config
                try:
                    config = load_config()
                    config['github_token'] = token_resp['access_token']
                    config['github_auth_method'] = 'oauth'
                    save_config(config)
                    print(f'[GitHub OAuth] Token saved successfully (len={len(token_resp["access_token"])})')
                except Exception as e:
                    print(f'[GitHub OAuth] Failed to save token: {e}')
                break
            error = token_resp.get('error', '')
            if error == 'authorization_pending':
                continue
            elif error == 'slow_down':
                poll_interval += 5
                continue
            elif error == 'expired_token':
                result_holder['error'] = '授权已过期，请重新尝试'
                result_holder['done'] = True
                break
            elif error == 'access_denied':
                result_holder['error'] = '用户拒绝了授权'
                result_holder['done'] = True
                break
            else:
                result_holder['error'] = token_resp.get('error_description', error or 'Unknown error')
                result_holder['done'] = True
                break
        if not result_holder['done']:
            result_holder['error'] = '授权超时'
            result_holder['done'] = True
        # Cleanup
        _github_oauth_pending.pop(device_code, None)

    t = threading.Thread(target=_poll, daemon=True)
    t.start()
    _github_oauth_pending[device_code] = {
        'thread': t,
        'stop_event': stop_event,
        'result': result_holder,
    }

    return jsonify({
        'ok': True,
        'device_code': device_code,
        'user_code': user_code,
        'verification_uri': verification_uri,
        'expires_in': expires_in,
        'interval': interval,
    })


@bp.route('/api/git/github/auth/poll', methods=['POST'])
@handle_error
def github_auth_poll():
    """Step 2: Poll for the OAuth token result.

    Returns { done: false } while waiting, or { done: true, token: '...' } when complete.
    """
    data = request.json or {}
    device_code = data.get('device_code', '')

    entry = _github_oauth_pending.get(device_code)
    if not entry:
        return jsonify({'error': '无效的设备代码，请重新开始授权'}), 400

    result = entry['result']
    resp = {'done': result['done']}
    if result['done']:
        if result['token']:
            resp['token'] = result['token']
            resp['success'] = True
        else:
            resp['error'] = result['error']
            resp['success'] = False

    return jsonify(resp)


@bp.route('/api/git/github/auth/status', methods=['GET'])
@handle_error
def github_auth_status():
    """Check current GitHub authentication status."""
    config = load_config()
    token = config.get('github_token', '')
    auth_method = config.get('github_auth_method', '')

    if not token:
        return jsonify({
            'authenticated': False,
            'method': '',
        })

    # Try to verify the token by fetching user info
    try:
        import urllib.request
        import json as _json
        req = urllib.request.Request(
            'https://api.github.com/user',
            headers={
                'Authorization': f'token {token}',
                'Accept': 'application/json',
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            user_data = _json.loads(resp.read().decode('utf-8'))
        return jsonify({
            'authenticated': True,
            'method': auth_method or 'token',
            'username': user_data.get('login', ''),
            'avatar_url': user_data.get('avatar_url', ''),
        })
    except Exception as e:
        error_msg = str(e)
        if '401' in error_msg:
            return jsonify({
                'authenticated': False,
                'method': auth_method,
                'error': 'Token 已失效',
            })
        return jsonify({
            'authenticated': bool(token),
            'method': auth_method or 'token',
            'error': f'验证失败: {error_msg}',
        })


@bp.route('/api/git/github/user/repos', methods=['GET'])
@handle_error
def github_user_public_repos():
    """List a user's public GitHub repositories (no auth needed).

    Query params:
      - username: (required) GitHub username
      - per_page: 1-100, default 100
      - sort: 'updated' (default), 'created', 'pushed', 'full_name'
    """
    username = request.args.get('username', '').strip()
    if not username:
        return jsonify({'repos': [], 'error': '请输入 GitHub 用户名'}), 400

    per_page = min(int(request.args.get('per_page', 100)), 100)
    sort = request.args.get('sort', 'updated')

    try:
        import urllib.request
        import json as _json
        req = urllib.request.Request(
            f'https://api.github.com/users/{username}/repos?per_page={per_page}&sort={sort}&type=owner',
            headers={
                'Accept': 'application/vnd.github+json',
                'User-Agent': 'MusIDE',
            }
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            repos = _json.loads(resp.read().decode('utf-8'))
        result = []
        for r in repos:
            result.append({
                'full_name': r.get('full_name', ''),
                'name': r.get('name', ''),
                'owner': (r.get('owner') or {}).get('login', ''),
                'html_url': r.get('html_url', ''),
                'clone_url': r.get('clone_url', ''),
                'ssh_url': r.get('ssh_url', ''),
                'private': r.get('private', False),
                'description': (r.get('description') or '')[:120],
                'updated_at': r.get('updated_at', ''),
            })
        result.sort(key=lambda x: x['updated_at'], reverse=True)
        return jsonify({'repos': result})
    except Exception as e:
        error_msg = str(e)
        if '404' in error_msg:
            return jsonify({'repos': [], 'error': f'用户 {username} 不存在或无公开仓库'}), 404
        return jsonify({'repos': [], 'error': f'获取仓库列表失败: {error_msg}'}), 500


@bp.route('/api/git/github/repos', methods=['GET'])
@handle_error
def github_list_repos():
    """List user's GitHub repositories (requires saved token).

    Query params:
      - type: 'all' (default), 'owner', 'member', 'public', 'private'
      - sort: 'updated' (default), 'created', 'pushed', 'full_name'
      - per_page: 1-100, default 30
      - page: default 1
    """
    config = load_config()
    token = config.get('github_token', '')
    if not token:
        return jsonify({'repos': [], 'error': '未登录，请先授权'}), 401

    params = {
        'type': request.args.get('type', 'owner'),
        'sort': request.args.get('sort', 'updated'),
        'per_page': min(int(request.args.get('per_page', 100)), 100),
        'page': int(request.args.get('page', 1)),
    }
    qs = '&'.join(f'{k}={v}' for k, v in params.items())

    try:
        import urllib.request
        import json as _json
        req = urllib.request.Request(
            f'https://api.github.com/user/repos?{qs}',
            headers={
                'Authorization': f'token {token}',
                'Accept': 'application/vnd.github+json',
            }
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            repos = _json.loads(resp.read().decode('utf-8'))
        # Return simplified list
        result = []
        for r in repos:
            result.append({
                'full_name': r.get('full_name', ''),
                'name': r.get('name', ''),
                'owner': (r.get('owner') or {}).get('login', ''),
                'html_url': r.get('html_url', ''),
                'clone_url': r.get('clone_url', ''),
                'ssh_url': r.get('ssh_url', ''),
                'private': r.get('private', False),
                'description': (r.get('description') or '')[:120],
                'updated_at': r.get('updated_at', ''),
            })
        # Sort by updated_at desc
        result.sort(key=lambda x: x['updated_at'], reverse=True)
        return jsonify({'repos': result})
    except Exception as e:
        error_msg = str(e)
        if '401' in error_msg:
            return jsonify({'repos': [], 'error': 'Token 已失效，请重新授权'}), 401
        return jsonify({'repos': [], 'error': f'获取仓库列表失败: {error_msg}'}), 500
