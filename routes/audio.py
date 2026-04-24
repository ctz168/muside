"""
MusIDE - Audio library, stem separation, lyrics transcription, and project persistence API.

Audio files are saved to ~/.muside/audio_library/
Separated stems are saved to ~/.muside/audio_library/stems/
Lyrics transcription results are saved alongside audio files as .lyrics.json
Project state (tracks, clips, BPM, settings) is saved to ~/.muside/project.json
Analysis jobs are tracked in memory with progress updates.
"""

import os
import json
import time
import uuid
import threading
import traceback
from datetime import datetime
from flask import Blueprint, jsonify, request, send_from_directory
from utils import handle_error, CONFIG_DIR

bp = Blueprint('audio', __name__)

# ── Audio Library ──
AUDIO_LIBRARY_DIR = os.path.join(CONFIG_DIR, 'audio_library')
STEMS_DIR = os.path.join(AUDIO_LIBRARY_DIR, 'stems')
os.makedirs(AUDIO_LIBRARY_DIR, exist_ok=True)
os.makedirs(STEMS_DIR, exist_ok=True)

# ── Project State ──
PROJECT_FILE = os.path.join(CONFIG_DIR, 'project.json')

# ── Analysis Job Tracking ──
_analysis_jobs = {}  # job_id -> { status, progress, message, result, error }
_analysis_lock = threading.Lock()


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
    original_name = file.filename
    prefix = uuid.uuid4().hex[:6]
    safe_name = prefix + '_' + original_name.replace('/', '_').replace('\\', '_')

    # Sanitize: only allow audio-related extensions
    allowed_exts = {'.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a', '.wma',
                    '.webm', '.opus', '.aiff', '.mid', '.midi'}
    ext = os.path.splitext(safe_name)[1].lower()
    if ext not in allowed_exts:
        pass  # Allow it anyway

    save_path = os.path.join(AUDIO_LIBRARY_DIR, safe_name)

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
                # Check if stems exist for this file
                base = os.path.splitext(fname)[0]
                stems_dir = os.path.join(STEMS_DIR, base)
                has_stems = os.path.isdir(stems_dir) and len(os.listdir(stems_dir)) > 0
                # Check if lyrics exist
                lyrics_path = os.path.join(AUDIO_LIBRARY_DIR, base + '.lyrics.json')
                has_lyrics = os.path.exists(lyrics_path)
                files.append({
                    'filename': fname,
                    'path': '/api/audio/file/' + fname,
                    'size': stat.st_size,
                    'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    'has_stems': has_stems,
                    'has_lyrics': has_lyrics,
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

    safe_name = filename.replace('/', '_').replace('\\', '_').replace('..', '_')
    fpath = os.path.join(AUDIO_LIBRARY_DIR, safe_name)
    if not os.path.realpath(fpath).startswith(os.path.realpath(AUDIO_LIBRARY_DIR)):
        return jsonify({'error': 'Access denied'}), 403

    if os.path.exists(fpath):
        os.remove(fpath)
        # Also remove stems and lyrics
        base = os.path.splitext(safe_name)[0]
        stems_dir = os.path.join(STEMS_DIR, base)
        if os.path.isdir(stems_dir):
            import shutil
            shutil.rmtree(stems_dir, ignore_errors=True)
        lyrics_path = os.path.join(AUDIO_LIBRARY_DIR, base + '.lyrics.json')
        if os.path.exists(lyrics_path):
            os.remove(lyrics_path)
        return jsonify({'ok': True})
    return jsonify({'error': 'File not found'}), 404


# ── Stem Separation (Demucs) ──

def _run_stem_separation(job_id, audio_path, model='htdemucs'):
    """Run Demucs stem separation in a background thread."""
    from utils import log_write
    try:
        with _analysis_lock:
            _analysis_jobs[job_id]['status'] = 'separating'
            _analysis_jobs[job_id]['message'] = 'Loading Demucs model...'

        import torch
        from demucs.pretrained import get_model
        from demucs.apply import apply_model
        import torchaudio
        import numpy as np

        # Load the model
        log_write(f'[MusIDE] Loading Demucs model: {model}')
        with _analysis_lock:
            _analysis_jobs[job_id]['progress'] = 5
            _analysis_jobs[job_id]['message'] = f'Loading {model} model...'

        demucs_model = get_model(model)
        demucs_model.eval()

        # Load audio
        with _analysis_lock:
            _analysis_jobs[job_id]['progress'] = 10
            _analysis_jobs[job_id]['message'] = 'Reading audio file...'

        wav, sr = torchaudio.load(audio_path)
        # Resample if needed
        if sr != demucs_model.samplerate:
            wav = torchaudio.functional.resample(wav, sr, demucs_model.samplerate)
            sr = demucs_model.samplerate

        # Ensure stereo
        if wav.shape[0] == 1:
            wav = wav.repeat(2, 1)
        elif wav.shape[0] > 2:
            wav = wav[:2, :]

        # Normalize
        ref = wav.mean(0)
        wav_input = (wav - ref.mean()) / ref.std()

        # Run separation
        with _analysis_lock:
            _analysis_jobs[job_id]['progress'] = 20
            _analysis_jobs[job_id]['message'] = 'Separating stems (this may take a while)...'

        with torch.no_grad():
            # Apply model - split into chunks for progress tracking
            sources = apply_model(demucs_model, wav_input[None], progress=False)[0]

        # sources shape: [n_sources, n_channels, n_samples]
        sources = sources * ref.std() + ref.mean()

        # Get source names
        source_names = demucs_model.sources  # e.g. ['drums', 'bass', 'other', 'vocals']

        # Save each stem
        base_name = os.path.splitext(os.path.basename(audio_path))[0]
        stems_dir = os.path.join(STEMS_DIR, base_name)
        os.makedirs(stems_dir, exist_ok=True)

        stems_info = []
        for i, name in enumerate(source_names):
            with _analysis_lock:
                progress = 50 + int((i / len(source_names)) * 40)
                _analysis_jobs[job_id]['progress'] = progress
                _analysis_jobs[job_id]['message'] = f'Saving stem: {name}...'

            stem_wav = sources[i]
            # Clamp to prevent clipping
            stem_wav = torch.clamp(stem_wav, -1.0, 1.0)
            stem_filename = f'{name}.wav'
            stem_path = os.path.join(stems_dir, stem_filename)
            torchaudio.save(stem_path, stem_wav, int(sr))

            stems_info.append({
                'name': name,
                'filename': stem_filename,
                'path': f'/api/audio/stems/{base_name}/{stem_filename}',
            })

        # Save stems metadata
        meta_path = os.path.join(stems_dir, 'meta.json')
        with open(meta_path, 'w', encoding='utf-8') as f:
            json.dump({
                'source_file': os.path.basename(audio_path),
                'model': model,
                'stems': stems_info,
                'created_at': datetime.now().isoformat(),
            }, f, indent=2, ensure_ascii=False)

        with _analysis_lock:
            _analysis_jobs[job_id]['status'] = 'separated'
            _analysis_jobs[job_id]['progress'] = 90
            _analysis_jobs[job_id]['message'] = 'Stem separation complete!'
            _analysis_jobs[job_id]['result'] = {
                'stems_dir': base_name,
                'stems': stems_info,
                'source_file': os.path.basename(audio_path),
            }

        log_write(f'[MusIDE] Stem separation complete: {base_name} -> {[s["name"] for s in stems_info]}')

    except Exception as e:
        tb = traceback.format_exc()
        log_write(f'[MusIDE] Stem separation error: {e}\n{tb}')
        with _analysis_lock:
            _analysis_jobs[job_id]['status'] = 'error'
            _analysis_jobs[job_id]['error'] = str(e)
            _analysis_jobs[job_id]['message'] = f'Separation failed: {e}'


def _run_lyrics_transcription(job_id, audio_path, language=None):
    """Run Whisper lyrics transcription in a background thread."""
    from utils import log_write
    try:
        with _analysis_lock:
            _analysis_jobs[job_id]['status'] = 'transcribing'
            _analysis_jobs[job_id]['message'] = 'Loading Whisper model...'

        import whisper

        # Use 'base' model for speed, can be configured
        model_size = 'base'
        with _analysis_lock:
            _analysis_jobs[job_id]['progress'] = 5
            _analysis_jobs[job_id]['message'] = f'Loading Whisper {model_size} model...'

        whisper_model = whisper.load_model(model_size)

        with _analysis_lock:
            _analysis_jobs[job_id]['progress'] = 15
            _analysis_jobs[job_id]['message'] = 'Transcribing audio...'

        # Transcribe with word-level timestamps
        transcribe_opts = {
            'word_timestamps': True,
            'fp16': False,  # CPU mode
        }
        if language:
            transcribe_opts['language'] = language

        result = whisper_model.transcribe(audio_path, **transcribe_opts)

        # Build lyrics with timestamps
        lyrics_lines = []
        for seg in result.get('segments', []):
            lyrics_lines.append({
                'time': round(seg['start'], 2),
                'end': round(seg['end'], 2),
                'text': seg['text'].strip(),
            })

        # Save lyrics
        base_name = os.path.splitext(os.path.basename(audio_path))[0]
        lyrics_path = os.path.join(AUDIO_LIBRARY_DIR, base_name + '.lyrics.json')
        lyrics_data = {
            'source_file': os.path.basename(audio_path),
            'language': result.get('language', 'unknown'),
            'model': model_size,
            'lyrics': lyrics_lines,
            'full_text': result.get('text', '').strip(),
            'created_at': datetime.now().isoformat(),
        }
        with open(lyrics_path, 'w', encoding='utf-8') as f:
            json.dump(lyrics_data, f, indent=2, ensure_ascii=False)

        with _analysis_lock:
            _analysis_jobs[job_id]['status'] = 'transcribed'
            _analysis_jobs[job_id]['progress'] = 100
            _analysis_jobs[job_id]['message'] = 'Lyrics transcription complete!'
            _analysis_jobs[job_id]['result'] = {
                'lyrics_path': base_name + '.lyrics.json',
                'language': result.get('language', 'unknown'),
                'lyrics': lyrics_lines,
                'full_text': result.get('text', '').strip(),
            }

        log_write(f'[MusIDE] Lyrics transcription complete: {base_name}, language={result.get("language")}, {len(lyrics_lines)} lines')

    except Exception as e:
        tb = traceback.format_exc()
        log_write(f'[MusIDE] Lyrics transcription error: {e}\n{tb}')
        with _analysis_lock:
            _analysis_jobs[job_id]['status'] = 'error'
            _analysis_jobs[job_id]['error'] = str(e)
            _analysis_jobs[job_id]['message'] = f'Transcription failed: {e}'


@bp.route('/api/audio/analyze', methods=['POST'])
@handle_error
def analyze_audio():
    """Start audio analysis: stem separation + lyrics transcription.
    
    Expects JSON: {
        filename: string (required) - filename in audio_library,
        separate: boolean (default true) - whether to run stem separation,
        transcribe: boolean (default true) - whether to run lyrics transcription,
        model: string (default 'htdemucs') - demucs model name,
        language: string|null (default null) - language hint for whisper
    }
    Returns: { ok: true, job_id: string, ... }
    """
    data = request.json or {}
    filename = data.get('filename', '')
    if not filename:
        return jsonify({'error': 'filename required'}), 400

    # Security check
    safe_name = filename.replace('/', '_').replace('\\', '_').replace('..', '_')
    audio_path = os.path.join(AUDIO_LIBRARY_DIR, safe_name)
    if not os.path.realpath(audio_path).startswith(os.path.realpath(AUDIO_LIBRARY_DIR)):
        return jsonify({'error': 'Access denied'}), 403
    if not os.path.exists(audio_path):
        return jsonify({'error': 'File not found'}), 404

    do_separate = data.get('separate', True)
    do_transcribe = data.get('transcribe', True)
    model = data.get('model', 'htdemucs')
    language = data.get('language', None)

    # Check existing results
    base_name = os.path.splitext(safe_name)[0]
    existing_stems = os.path.isdir(os.path.join(STEMS_DIR, base_name))
    existing_lyrics = os.path.exists(os.path.join(AUDIO_LIBRARY_DIR, base_name + '.lyrics.json'))

    results = {}
    job_id_separate = None
    job_id_transcribe = None

    # Stem separation
    if do_separate and not existing_stems:
        job_id_separate = uuid.uuid4().hex[:8]
        with _analysis_lock:
            _analysis_jobs[job_id_separate] = {
                'status': 'queued',
                'progress': 0,
                'message': 'Queued for stem separation...',
                'result': None,
                'error': None,
                'type': 'separation',
                'filename': safe_name,
                'started_at': datetime.now().isoformat(),
            }
        t = threading.Thread(
            target=_run_stem_separation,
            args=(job_id_separate, audio_path, model),
            daemon=True,
        )
        t.start()
        results['separation_job_id'] = job_id_separate
    elif do_separate and existing_stems:
        # Load existing stems
        meta_path = os.path.join(STEMS_DIR, base_name, 'meta.json')
        if os.path.exists(meta_path):
            with open(meta_path, 'r', encoding='utf-8') as f:
                meta = json.load(f)
            results['existing_stems'] = meta.get('stems', [])

    # Lyrics transcription
    if do_transcribe and not existing_lyrics:
        job_id_transcribe = uuid.uuid4().hex[:8]
        with _analysis_lock:
            _analysis_jobs[job_id_transcribe] = {
                'status': 'queued',
                'progress': 0,
                'message': 'Queued for lyrics transcription...',
                'result': None,
                'error': None,
                'type': 'transcription',
                'filename': safe_name,
                'started_at': datetime.now().isoformat(),
            }
        t = threading.Thread(
            target=_run_lyrics_transcription,
            args=(job_id_transcribe, audio_path, language),
            daemon=True,
        )
        t.start()
        results['transcription_job_id'] = job_id_transcribe
    elif do_transcribe and existing_lyrics:
        # Load existing lyrics
        lyrics_path = os.path.join(AUDIO_LIBRARY_DIR, base_name + '.lyrics.json')
        with open(lyrics_path, 'r', encoding='utf-8') as f:
            lyrics_data = json.load(f)
        results['existing_lyrics'] = lyrics_data.get('lyrics', [])
        results['lyrics_language'] = lyrics_data.get('language', 'unknown')

    return jsonify({
        'ok': True,
        'filename': safe_name,
        **results,
    })


@bp.route('/api/audio/analyze_status', methods=['GET'])
@handle_error
def analyze_status():
    """Check status of an analysis job.
    
    Query params: job_id (required)
    Returns: { status, progress, message, result?, error? }
    """
    job_id = request.args.get('job_id', '')
    if not job_id:
        # Return all active jobs
        with _analysis_lock:
            jobs = {}
            for jid, job in _analysis_jobs.items():
                jobs[jid] = {
                    'status': job['status'],
                    'progress': job['progress'],
                    'message': job['message'],
                    'type': job.get('type', 'unknown'),
                    'error': job.get('error'),
                }
            return jsonify({'ok': True, 'jobs': jobs})

    with _analysis_lock:
        job = _analysis_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    resp = {
        'ok': True,
        'job_id': job_id,
        'status': job['status'],
        'progress': job['progress'],
        'message': job['message'],
        'type': job.get('type', 'unknown'),
    }
    if job.get('result'):
        resp['result'] = job['result']
    if job.get('error'):
        resp['error'] = job['error']
    return jsonify(resp)


@bp.route('/api/audio/stems/<path:stems_dir>/<path:filename>', methods=['GET'])
@handle_error
def serve_stem(stems_dir, filename):
    """Serve a separated stem audio file."""
    # Security check
    safe_dir = stems_dir.replace('/', '_').replace('\\', '_').replace('..', '_')
    safe_file = filename.replace('/', '_').replace('\\', '_').replace('..', '_')
    stems_path = os.path.join(STEMS_DIR, safe_dir)
    if not os.path.realpath(stems_path).startswith(os.path.realpath(STEMS_DIR)):
        return jsonify({'error': 'Access denied'}), 403
    return send_from_directory(stems_path, safe_file)


@bp.route('/api/audio/lyrics/<path:filename>', methods=['GET'])
@handle_error
def get_lyrics(filename):
    """Get lyrics transcription for an audio file."""
    safe_name = filename.replace('/', '_').replace('\\', '_').replace('..', '_')
    base_name = os.path.splitext(safe_name)[0]
    lyrics_path = os.path.join(AUDIO_LIBRARY_DIR, base_name + '.lyrics.json')

    if not os.path.exists(lyrics_path):
        return jsonify({'ok': True, 'lyrics': [], 'language': None})

    with open(lyrics_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return jsonify({
        'ok': True,
        'lyrics': data.get('lyrics', []),
        'language': data.get('language', 'unknown'),
        'full_text': data.get('full_text', ''),
    })


# ── Project State ──

@bp.route('/api/project/save', methods=['POST'])
@handle_error
def save_project():
    """Save the current project state (tracks, clips, BPM, settings, lyrics, etc.)."""
    data = request.json or {}
    os.makedirs(CONFIG_DIR, exist_ok=True)

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
