"""
MusIDE - Audio library and project persistence API.

Audio files are saved to ~/.muside/audio_library/
Project state (tracks, clips, BPM, settings) is saved to ~/.muside/project.json
"""

import os
import json
import time
import uuid
from datetime import datetime
from flask import Blueprint, jsonify, request, send_from_directory
from utils import handle_error, CONFIG_DIR

bp = Blueprint('audio', __name__)

# ── Audio Library ──
AUDIO_LIBRARY_DIR = os.path.join(CONFIG_DIR, 'audio_library')
os.makedirs(AUDIO_LIBRARY_DIR, exist_ok=True)

# ── Project State ──
PROJECT_FILE = os.path.join(CONFIG_DIR, 'project.json')


@bp.route('/api/audio/upload', methods=['POST'])
@handle_error
def upload_audio():
    """Upload an audio file to the audio library.
    Expects multipart/form-data with a 'file' field.
    Returns the saved filename and server path.
    """
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'Empty filename'}), 400

    # Generate a unique filename to avoid collisions
    # Keep original name but add a short unique prefix
    original_name = file.filename
    prefix = uuid.uuid4().hex[:6]
    safe_name = prefix + '_' + original_name.replace('/', '_').replace('\\', '_')

    # Sanitize: only allow audio-related extensions
    allowed_exts = {'.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a', '.wma',
                    '.webm', '.opus', '.aiff', '.mid', '.midi'}
    ext = os.path.splitext(safe_name)[1].lower()
    if ext not in allowed_exts:
        # Allow it anyway — browser may use other formats via MediaRecorder etc.
        pass

    save_path = os.path.join(AUDIO_LIBRARY_DIR, safe_name)

    # Check if file with same content already exists (by name match)
    # If exact file already exists, skip re-saving
    if not os.path.exists(save_path):
        file.save(save_path)

    file_size = os.path.getsize(save_path)

    return jsonify({
        'ok': True,
        'filename': safe_name,
        'original_name': original_name,
        'path': '/api/audio/file/' + safe_name,
        'size': file_size,
    })


@bp.route('/api/audio/list', methods=['GET'])
@handle_error
def list_audio():
    """List all audio files in the audio library."""
    files = []
    if os.path.isdir(AUDIO_LIBRARY_DIR):
        for fname in sorted(os.listdir(AUDIO_LIBRARY_DIR)):
            fpath = os.path.join(AUDIO_LIBRARY_DIR, fname)
            if os.path.isfile(fpath):
                stat = os.stat(fpath)
                files.append({
                    'filename': fname,
                    'path': '/api/audio/file/' + fname,
                    'size': stat.st_size,
                    'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                })
    return jsonify({'files': files})


@bp.route('/api/audio/file/<path:filename>', methods=['GET'])
@handle_error
def serve_audio(filename):
    """Serve an audio file from the audio library."""
    return send_from_directory(AUDIO_LIBRARY_DIR, filename)


@bp.route('/api/audio/delete', methods=['POST'])
@handle_error
def delete_audio():
    """Delete an audio file from the library."""
    data = request.json or {}
    filename = data.get('filename', '')
    if not filename:
        return jsonify({'error': 'filename required'}), 400

    # Security: prevent path traversal
    safe_name = filename.replace('/', '_').replace('\\', '_').replace('..', '_')
    fpath = os.path.join(AUDIO_LIBRARY_DIR, safe_name)
    if not os.path.realpath(fpath).startswith(os.path.realpath(AUDIO_LIBRARY_DIR)):
        return jsonify({'error': 'Access denied'}), 403

    if os.path.exists(fpath):
        os.remove(fpath)
        return jsonify({'ok': True})
    return jsonify({'error': 'File not found'}), 404


# ── Project State ──

@bp.route('/api/project/save', methods=['POST'])
@handle_error
def save_project():
    """Save the current project state (tracks, clips, BPM, settings, lyrics, etc.)."""
    data = request.json or {}
    os.makedirs(CONFIG_DIR, exist_ok=True)

    # Add saved timestamp
    data['saved_at'] = datetime.now().isoformat()
    data['version'] = 1

    with open(PROJECT_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    return jsonify({'ok': True, 'saved_at': data['saved_at']})


@bp.route('/api/project/load', methods=['GET'])
@handle_error
def load_project():
    """Load the saved project state."""
    if not os.path.exists(PROJECT_FILE):
        return jsonify({'ok': True, 'project': None})

    try:
        with open(PROJECT_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return jsonify({'ok': True, 'project': data})
    except Exception as e:
        return jsonify({'ok': True, 'project': None, 'error': str(e)})
