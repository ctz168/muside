"""
MusIDE - IDE Update API routes.

Checks for two types of updates:
1. APK update — from ctz168/muside GitHub Releases
2. Code update — from ctz168/ide GitHub repository
"""

import os
import re
import sys
import json
import subprocess
import threading
import time
import tempfile
import shutil
import urllib.request
import urllib.error
import tarfile
from datetime import datetime
from flask import Blueprint, jsonify, request
from utils import handle_error, load_config, save_chat_history, WORKSPACE, SERVER_DIR, PORT, HOST, CONFIG_DIR, log_write
from routes.git import git_cmd

bp = Blueprint('update', __name__)

# GitHub repos
IDE_REPO = 'ctz168/ide'          # Code (server, routes, static)
APK_REPO = 'ctz168/muside'     # APK releases

# GitHub API URLs
IDE_COMMITS_URL = f'https://api.github.com/repos/{IDE_REPO}/commits/main'
APK_RELEASES_URL = f'https://api.github.com/repos/{APK_REPO}/releases/latest'


def _fetch_github_json(url, timeout=15):
    """Helper to fetch JSON from GitHub API."""
    req = urllib.request.Request(url, headers={
        'User-Agent': 'MusIDE-Server',
        'Accept': 'application/vnd.github.v3+json',
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _parse_version(version_str):
    """Parse version string like '3.0.40' or '3.0.40-build.72' into comparable tuple.

    Returns (major, minor, patch, build) tuple. Build defaults to 0 if absent.
    Returns None if parsing fails.
    """
    if not version_str:
        return None
    cleaned = version_str.lstrip('v')
    m = re.match(r'(\d+)\.(\d+)\.(\d+)(?:-build\.?(\d+))?', cleaned)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4) or 0))
    return None


def _get_current_version():
    """Read the current app version. Priority: version.txt > git describe."""
    # 1. version.txt (written by CI build when bundled in APK)
    vtxt = os.path.join(SERVER_DIR, 'version.txt')
    if os.path.exists(vtxt):
        try:
            with open(vtxt, 'r', encoding='utf-8') as f:
                v = f.read().strip()
                if v:
                    return v
        except Exception:
            pass
    # 2. git describe (when running from git clone)
    try:
        r = git_cmd('describe --tags --abbrev=0', cwd=SERVER_DIR)
        if r['ok'] and r['stdout'].strip():
            return r['stdout'].strip().lstrip('v')
    except Exception:
        pass
    return '0.0.0'


def _get_local_commit():
    """Get local commit SHA. Try git, then commit.txt fallback."""
    try:
        r = git_cmd('rev-parse HEAD', cwd=SERVER_DIR)
        if r['ok'] and r['stdout'].strip():
            return r['stdout'].strip()[:40]
    except Exception:
        pass
    # Fallback: read commit.txt (written by CI build when bundled in APK)
    ctxt = os.path.join(SERVER_DIR, 'commit.txt')
    if os.path.exists(ctxt):
        try:
            with open(ctxt, 'r', encoding='utf-8') as f:
                sha = f.read().strip()[:40]
                if sha and len(sha) >= 7:
                    return sha
        except Exception:
            pass
    return ''


@bp.route('/api/update/check', methods=['POST'])
@handle_error
def update_check():
    """Check for code updates only from ctz168/ide repository.
    
    Note: APK updates are intentionally disabled to avoid requiring
    app termination for installation (self-killing paradox).
    """
    try:
        # Get current version
        current_version = _get_current_version()

        # Get local commit SHA
        local_sha = _get_local_commit()

        # === Code Update Check (from ctz168/ide commits) ===
        remote_sha = ''
        remote_message = ''
        code_update = False
        try:
            commit_data = _fetch_github_json(IDE_COMMITS_URL)
            remote_sha = commit_data.get('sha', '')
            remote_message = commit_data.get('commit', {}).get('message', '')

            if local_sha and remote_sha and local_sha != remote_sha:
                code_update = True
        except Exception as e:
            # If GitHub API fails, assume no update
            log_write(f'[UPDATE] GitHub check failed: {e}')
            pass

        return jsonify({
            'update_available': code_update,
            'apk_update': False,  # Disabled
            'code_update': code_update,
            'current_version': current_version,
            'new_version': '',  # Not applicable for code updates
            'latest_tag': '',
            'release_name': '',
            'release_body': '',
            'release_date': '',
            'release_url': '',
            'apk_url': '',
            'apk_size': 0,
            'apk_size_human': '',
            'local_sha': local_sha[:8] if local_sha else 'unknown',
            'remote_sha': remote_sha[:8] if remote_sha else 'unknown',
            'remote_message': remote_message.split('\n')[0] if remote_message else '',
            'commits_behind': 1 if code_update else 0,
        })
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return jsonify({'error': 'No releases found', 'update_available': False, 'current_version': _get_current_version()})
        return jsonify({'error': f'GitHub API error: {e.code}', 'update_available': False, 'current_version': _get_current_version()})
    except Exception as e:
        return jsonify({'error': str(e), 'update_available': False, 'current_version': _get_current_version()})


def _do_update_and_restart():
    """Run git pull / tarball download in a subprocess, then os.execv to reload.

    This function is called in a daemon thread.  It runs the actual update in a
    separate subprocess so the running server can exit cleanly before the new
    code replaces files on disk.  After the files are updated it re-executes
    muside_server.py which picks up the new code.

    Returns nothing — the caller should respond to the HTTP request immediately
    and this function runs in the background.
    """
    import subprocess as _sp

    log_write('[UPDATE] Background update started — giving current process time to finish HTTP response...')

    # ── Step 1: Give the Flask response a few seconds to reach the client ──
    time.sleep(3)

    # ── Step 2: Update code in a subprocess ──
    update_ok = False

    if os.path.exists(os.path.join(SERVER_DIR, '.git')):
        # Git path — run fetch + reset in a subprocess script
        update_script = f'''
import os, sys, subprocess, shutil
SERVER_DIR = {repr(SERVER_DIR)}
os.chdir(SERVER_DIR)

# Ensure remote points to ctz168/ide
r = subprocess.run(['git', 'remote', 'get-url', 'origin'], capture_output=True, text=True)
if r.returncode == 0:
    url = r.stdout.strip()
    if 'ctz168/ide' not in url:
        subprocess.run(['git', 'remote', 'set-url', 'origin', 'https://github.com/ctz168/ide.git'])

# Fetch latest
r = subprocess.run(['git', 'fetch', 'origin', 'main'], capture_output=True, text=True, timeout=120)
if r.returncode != 0:
    print('FAIL: git fetch failed')
    print(r.stderr)
    sys.exit(1)

# Reset to remote
r = subprocess.run(['git', 'reset', '--hard', 'origin/main'], capture_output=True, text=True)
if r.returncode != 0:
    print('FAIL: git reset failed')
    print(r.stderr)
    sys.exit(2)

# Clean __pycache__
for root, dirs, _ in os.walk(SERVER_DIR):
    if '__pycache__' in dirs:
        shutil.rmtree(os.path.join(root, '__pycache__'))

print('OK')
'''
        result = _sp.run(
            [sys.executable, '-c', update_script],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode == 0 and 'OK' in result.stdout:
            log_write('[UPDATE] Git pull succeeded in subprocess')
            update_ok = True
        else:
            log_write(f'[UPDATE] Git pull subprocess failed (rc={result.returncode}): {result.stderr}')
    else:
        # No .git — download tarball in a subprocess script
        update_script = f'''
import os, sys, tempfile, shutil, tarfile, urllib.request
SERVER_DIR = {repr(SERVER_DIR)}
tarball_url = 'https://github.com/ctz168/ide/archive/refs/heads/main.tar.gz'

req = urllib.request.Request(tarball_url, headers={{'User-Agent': 'MusIDE-Server'}})
with urllib.request.urlopen(req, timeout=120) as resp:
    tarball_data = resp.read()

tmpdir = tempfile.mkdtemp(prefix='muside_update_')
try:
    tarball_path = os.path.join(tmpdir, 'main.tar.gz')
    with open(tarball_path, 'wb') as f:
        f.write(tarball_data)
    with tarfile.open(tarball_path, 'r:gz') as tar:
        tar.extractall(tmpdir)

    extracted_dir = None
    for entry in os.listdir(tmpdir):
        full = os.path.join(tmpdir, entry)
        if os.path.isdir(full) and entry.endswith('-main'):
            extracted_dir = full
            break
    if not extracted_dir:
        print('FAIL: no extracted dir found')
        sys.exit(1)

    for fname in ['muside_server.py', 'utils.py', 'requirements.txt']:
        src = os.path.join(extracted_dir, fname)
        dst = os.path.join(SERVER_DIR, fname)
        if os.path.exists(src):
            shutil.copy2(src, dst)

    for dirname in ['routes', 'static']:
        src = os.path.join(extracted_dir, dirname)
        dst = os.path.join(SERVER_DIR, dirname)
        if os.path.exists(src):
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst)

    # Clean __pycache__
    for root, dirs, _ in os.walk(SERVER_DIR):
        if '__pycache__' in dirs:
            shutil.rmtree(os.path.join(root, '__pycache__'))
    print('OK')
finally:
    shutil.rmtree(tmpdir, ignore_errors=True)
'''
        result = _sp.run(
            [sys.executable, '-c', update_script],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode == 0 and 'OK' in result.stdout:
            log_write('[UPDATE] Tarball download succeeded in subprocess')
            update_ok = True
        else:
            log_write(f'[UPDATE] Tarball download subprocess failed (rc={result.returncode}): {result.stderr}')

    # ── Step 3: Re-exec muside_server.py ONLY if update succeeded ──
    if not update_ok:
        log_write('[UPDATE] Update failed, NOT restarting server.')
        return

    log_write('[UPDATE] Files updated, re-executing muside_server.py...')
    server_script = os.path.join(SERVER_DIR, 'muside_server.py')

    # Ensure working directory is correct before exec
    os.chdir(SERVER_DIR)

    # Flush logs before exec
    sys.stdout.flush()
    sys.stderr.flush()

    os.execv(sys.executable, [sys.executable, server_script])


@bp.route('/api/update/apply', methods=['POST'])
@handle_error
def update_apply():
    """Download latest code from GitHub, then restart the server with new code.

    Since the server cannot replace its own running modules, this works in two phases:
      1. Return success to the client immediately (so the UI can show "updating").
      2. Spawn a background thread that waits a few seconds, then runs git pull /
         tarball download in a *subprocess*, and finally uses os.execv to replace
         the current process with a fresh one running the updated code.
    """
    # Launch update + restart in a daemon thread
    t = threading.Thread(target=_do_update_and_restart, daemon=True)
    t.start()

    return jsonify({
        'ok': True,
        'method': 'bg_update',
        'message': '代码正在后台更新，服务器将自动重启。',
    })
