/**
 * TrackEditor - DAW风格音乐轨道编辑器
 * 完整的Web Audio API音频引擎 + Canvas渲染
 * 纯原生JavaScript，无框架依赖
 *
 * 任务ID: 3-a
 * 作者: Agent
 * 日期: 2026-03-04
 */
window.TrackEditor = (function () {
    'use strict';

    // ───────────────────── 常量与配置 ─────────────────────
    const MAX_TRACKS = 16;
    const TRACK_HEADER_WIDTH = 160;
    const TIMELINE_HEIGHT = 28;
    const TRANSPORT_HEIGHT = 44;
    const TRACK_LANE_HEIGHT = 80;
    const CLIP_MIN_WIDTH = 8;
    const CLIP_TRIM_EDGE = 6; // 边缘拖拽热区宽度
    const DEFAULT_BPM = 120;
    const DEFAULT_TIME_SIG_NUM = 4;
    const DEFAULT_TIME_SIG_DEN = 4;
    const PIXELS_PER_BEAT_BASE = 60; // 基础缩放下每拍像素

    // 轨道颜色预设调色板
    const TRACK_COLORS = [
        '#e85d5d', '#e8853d', '#e8c45d', '#6bc96b', '#5dc4b8',
        '#5d9de8', '#b87de8', '#e85db8', '#c9785d', '#8be85d',
        '#5de8b8', '#5d7de8', '#e8b85d', '#5de8e8', '#e85d7d', '#b8e85d'
    ];

    // ───────────────────── 工具函数 ─────────────────────
    function clamp(val, min, max) { return Math.max(min, Math.min(max, val)); }
    function isMobileView() { return window.innerWidth <= 768; }
    function uid() { return '_' + Math.random().toString(36).substr(2, 9); }
    function formatTime(sec) {
        if (!isFinite(sec) || sec < 0) sec = 0;
        const m = Math.floor(sec / 60);
        const s = Math.floor(sec % 60);
        const ms = Math.floor((sec % 1) * 1000);
        return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0') + ':' + String(ms).padStart(3, '0');
    }
    function beatsToSeconds(beats, bpm) { return (beats * 60) / bpm; }
    function secondsToBeats(sec, bpm) { return (sec * bpm) / 60; }
    function hexToRgba(hex, alpha) {
        const r = parseInt(hex.slice(1, 3), 16);
        const g = parseInt(hex.slice(3, 5), 16);
        const b = parseInt(hex.slice(5, 7), 16);
        return 'rgba(' + r + ',' + g + ',' + b + ',' + alpha + ')';
    }

    // ───────────────────── 状态 ─────────────────────
    let _container = null;      // #track-editor-container
    let _initialized = false;

    // 项目设置
    let _bpm = DEFAULT_BPM;
    let _timeSigNum = DEFAULT_TIME_SIG_NUM;
    let _timeSigDen = DEFAULT_TIME_SIG_DEN;

    // 轨道数据
    let _tracks = [];
    let _trackIdCounter = 0;

    // 回放状态
    let _audioCtx = null;
    let _masterGain = null;
    let _isPlaying = false;
    let _isPaused = false;
    let _isRecording = false;
    let _isLooping = false;
    let _playStartTime = 0;       // audioCtx.currentTime when play started
    let _playStartOffset = 0;     // 秒, 从项目何处开始播放
    let _currentPlayPosition = 0; // 秒, 当前播放头位置
    let _loopStart = 0;
    let _loopEnd = 0;
    let _activeSources = [];      // 当前正在播放的BufferSourceNode
    let _animFrameId = null;

    // 缩放与滚动
    let _zoom = 1.0;    // 1.0 = 默认
    let _scrollX = 0;   // 水平滚动像素
    let _scrollY = 0;   // 垂直滚动像素

    // 选中的剪辑
    let _selectedClipTrackId = null;
    let _selectedClipId = null;

    // 拖拽状态
    let _dragState = null; // { type: 'clip-move'|'clip-trim-left'|'clip-trim-right'|'seek'|'scroll-h', ... }

    // 混音器面板可见
    let _mixerVisible = false;

    // 钢琴卷帘
    let _pianoRollVisible = false;
    let _pianoRollTrackId = null;
    let _pianoRollNotes = {};  // trackId -> [ { id, start, duration, pitch } ]

    // VU表
    let _vuMeters = {};  // trackId -> { analyser, peak, rms }

    // 歌词面板
    let _lyricsVisible = false;
    let _lyrics = {};  // trackId -> [ { time: 0, text: '歌词行' }, ... ]

    // 音色参数 (每轨道)
    let _defaultTimbre = { waveform: 'sine', attack: 0.05, decay: 0.1, sustain: 0.7, release: 0.3, filterFreq: 2000, filterQ: 1, pitchShift: 0, swing: 0, humanize: 0 };

    // 全局节拍/律动设置
    let _swing = 0;        // 0-1 摇摆量
    let _humanize = 0;     // 0-0.1 人性化偏移
    let _quantizeValue = 0; // 0=off, 1=1/4, 2=1/8, 3=1/16

    // DOM元素缓存
    let _els = {};

    // ───────────────────── AudioContext 管理 ─────────────────────
    function getAudioContext() {
        if (!_audioCtx) {
            _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        }
        // 移动端: 恢复被挂起的AudioContext
        if (_audioCtx.state === 'suspended') {
            _audioCtx.resume();
        }
        return _audioCtx;
    }

    function ensureMasterGain() {
        const ctx = getAudioContext();
        if (!_masterGain) {
            _masterGain = ctx.createGain();
            _masterGain.gain.value = 0.8;
            _masterGain.connect(ctx.destination);
        }
        return _masterGain;
    }

    // ───────────────────── 轨道管理 ─────────────────────
    function createTrack(name) {
        if (_tracks.length >= MAX_TRACKS) return null;
        const ctx = getAudioContext();
        const master = ensureMasterGain();
        const id = 'trk_' + (++_trackIdCounter);
        const colorIdx = _tracks.length % TRACK_COLORS.length;

        // Web Audio节点
        const gainNode = ctx.createGain();
        gainNode.gain.value = 0.8;
        const panNode = ctx.createStereoPanner();
        panNode.pan.value = 0;
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 256;
        analyser.smoothingTimeConstant = 0.8;

        gainNode.connect(panNode);
        panNode.connect(analyser);
        analyser.connect(master);

        const track = {
            id: id,
            name: name || ('轨道 ' + _tracks.length + 1),
            color: TRACK_COLORS[colorIdx],
            volume: 0.8,
            pan: 0,
            mute: false,
            solo: false,
            armed: false,
            gainNode: gainNode,
            panNode: panNode,
            analyser: analyser,
            clips: [],
            audioBuffers: {},  // clipId -> AudioBuffer
            timbre: Object.assign({}, _defaultTimbre),  // 每轨道音色参数
        };
        _tracks.push(track);
        _vuMeters[id] = { analyser: analyser, peak: 0, rms: 0 };
        return track;
    }

    function removeTrackById(id) {
        const idx = _tracks.findIndex(t => t.id === id);
        if (idx < 0) return;
        const track = _tracks[idx];
        // 断开音频节点
        try { track.gainNode.disconnect(); } catch (e) { /* ok */ }
        try { track.panNode.disconnect(); } catch (e) { /* ok */ }
        try { track.analyser.disconnect(); } catch (e) { /* ok */ }
        _tracks.splice(idx, 1);
        delete _vuMeters[id];
        if (_selectedClipTrackId === id) {
            _selectedClipTrackId = null;
            _selectedClipId = null;
        }
    }

    function getTrackById(id) {
        return _tracks.find(t => t.id === id) || null;
    }

    // Solo/Mute 逻辑
    function isTrackAudible(track) {
        const hasSolo = _tracks.some(t => t.solo);
        if (hasSolo) {
            return track.solo && !track.mute;
        }
        return !track.mute;
    }

    function updateTrackAudioRouting() {
        _tracks.forEach(track => {
            const audible = isTrackAudible(track);
            track.gainNode.gain.setTargetAtTime(audible ? track.volume : 0, getAudioContext().currentTime, 0.02);
            track.panNode.pan.setTargetAtTime(track.pan, getAudioContext().currentTime, 0.02);
        });
    }

    // ───────────────────── 音频剪辑管理 ─────────────────────
    function addClipToTrack(trackId, clip) {
        const track = getTrackById(trackId);
        if (!track) return null;
        const c = {
            id: uid(),
            startTime: clip.startTime || 0,
            duration: clip.duration || 0,
            offset: clip.offset || 0,
            filePath: clip.filePath || '',
            name: clip.name || '音频剪辑',
            waveformPeaks: clip.waveformPeaks || null,
        };
        track.clips.push(c);
        return c;
    }

    function removeClipFromTrack(trackId, clipId) {
        const track = getTrackById(trackId);
        if (!track) return;
        const idx = track.clips.findIndex(c => c.id === clipId);
        if (idx >= 0) track.clips.splice(idx, 1);
        if (_selectedClipTrackId === trackId && _selectedClipId === clipId) {
            _selectedClipTrackId = null;
            _selectedClipId = null;
        }
    }

    // ───────────────────── 波形峰值提取 ─────────────────────
    function extractPeaks(audioBuffer, samplesPerPixel) {
        const ch0 = audioBuffer.getChannelData(0);
        const ch1 = audioBuffer.numberOfChannels > 1 ? audioBuffer.getChannelData(1) : ch0;
        const totalSamples = ch0.length;
        const peaksCount = Math.ceil(totalSamples / samplesPerPixel);
        const peaksL = new Float32Array(peaksCount);
        const peaksR = new Float32Array(peaksCount);

        for (let i = 0; i < peaksCount; i++) {
            let maxL = 0, maxR = 0;
            const start = i * samplesPerPixel;
            const end = Math.min(start + samplesPerPixel, totalSamples);
            for (let j = start; j < end; j++) {
                const vL = Math.abs(ch0[j]);
                const vR = Math.abs(ch1[j]);
                if (vL > maxL) maxL = vL;
                if (vR > maxR) maxR = vR;
            }
            peaksL[i] = maxL;
            peaksR[i] = maxR;
        }
        return { left: peaksL, right: peaksR, length: peaksCount };
    }

    // ───────────────────── 音频文件加载 ─────────────────────
    function loadAudioFileToTrack(trackId, file, startTime) {
        const track = getTrackById(trackId);
        if (!track) return Promise.reject(new Error('轨道不存在'));
        const ctx = getAudioContext();

        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = function (e) {
                ctx.decodeAudioData(e.target.result, function (buffer) {
                    // 提取波形峰值
                    const pixPerBeat = PIXELS_PER_BEAT_BASE * _zoom;
                    const secPerPix = (60 / _bpm) / pixPerBeat;
                    const samplesPerPix = Math.max(1, Math.round(secPerPix * buffer.sampleRate));
                    const peaks = extractPeaks(buffer, samplesPerPix);

                    const clip = addClipToTrack(trackId, {
                        startTime: startTime || 0,
                        duration: buffer.duration,
                        offset: 0,
                        filePath: file.name,
                        name: file.name,
                        waveformPeaks: peaks,
                    });

                    track.audioBuffers[clip.id] = buffer;
                    renderAll();
                    resolve(clip);
                }, function (err) {
                    reject(new Error('音频解码失败: ' + (err ? err.message : '未知错误')));
                });
            };
            reader.onerror = function () { reject(new Error('文件读取失败')); };
            reader.readAsArrayBuffer(file);
        });
    }

    // ───────────────────── 回放引擎 ─────────────────────
    function stopAllSources() {
        _activeSources.forEach(src => {
            try { src.stop(0); } catch (e) { /* already stopped */ }
            try { src.disconnect(); } catch (e) { /* ok */ }
        });
        _activeSources = [];
    }

    function startPlaybackFrom(offset) {
        const ctx = getAudioContext();
        ensureMasterGain();
        stopAllSources();
        updateTrackAudioRouting();

        _playStartTime = ctx.currentTime;
        _playStartOffset = offset || 0;
        _currentPlayPosition = _playStartOffset;

        // 为每个轨道的每个剪辑创建BufferSource
        _tracks.forEach(track => {
            if (!isTrackAudible(track)) return;
            track.clips.forEach(clip => {
                const buffer = track.audioBuffers[clip.id];
                if (!buffer) return;

                const clipEnd = clip.startTime + clip.duration;
                if (clipEnd <= _playStartOffset) return; // 剪辑已经播完
                if (clip.startTime > _playStartOffset + getDuration()) return; // 剪辑太远

                const source = ctx.createBufferSource();
                source.buffer = buffer;

                // 计算偏移
                let when = _playStartTime;
                let startOffset = clip.offset || 0;
                if (clip.startTime < _playStartOffset) {
                    // 剪辑已经开始, 从中间播放
                    when = _playStartTime;
                    startOffset = (_playStartOffset - clip.startTime);
                } else {
                    when = _playStartTime + (clip.startTime - _playStartOffset);
                    startOffset = clip.offset || 0;
                }

                source.connect(track.gainNode);
                source.start(when, startOffset);
                _activeSources.push(source);

                source.onended = function () {
                    const idx = _activeSources.indexOf(source);
                    if (idx >= 0) _activeSources.splice(idx, 1);
                };
            });
        });
    }

    function play() {
        if (_isPlaying && !_isPaused) return;
        getAudioContext(); // 确保在用户交互中创建
        ensureMasterGain();

        if (_isPaused) {
            // 恢复播放
            startPlaybackFrom(_currentPlayPosition);
            _isPaused = false;
        } else {
            startPlaybackFrom(_currentPlayPosition);
        }
        _isPlaying = true;
        _isPaused = false;
        startAnimationLoop();
        updateTransportUI();
    }

    function pause() {
        if (!_isPlaying || _isPaused) return;
        _currentPlayPosition = getCurrentTime();
        stopAllSources();
        _isPaused = true;
        stopAnimationLoop();
        updateTransportUI();
    }

    function stop() {
        stopAllSources();
        _isPlaying = false;
        _isPaused = false;
        _isRecording = false;
        _currentPlayPosition = 0;
        stopAnimationLoop();
        renderAll();
        updateTransportUI();
    }

    function record() {
        _isRecording = !_isRecording;
        if (_isRecording && !_isPlaying) {
            play();
        }
        updateTransportUI();
    }

    function seek(time) {
        _currentPlayPosition = Math.max(0, time);
        if (_isPlaying && !_isPaused) {
            startPlaybackFrom(_currentPlayPosition);
        } else {
            renderAll();
        }
        updateTransportUI();
    }

    function getCurrentTime() {
        if (_isPlaying && !_isPaused) {
            const ctx = getAudioContext();
            const elapsed = ctx.currentTime - _playStartTime + _playStartOffset;
            // 循环模式
            if (_isLooping && _loopEnd > _loopStart && elapsed >= _loopEnd) {
                const loopDuration = _loopEnd - _loopStart;
                const over = elapsed - _loopStart;
                _currentPlayPosition = _loopStart + (over % loopDuration);
                // 需要重新启动播放
                startPlaybackFrom(_currentPlayPosition);
                _playStartTime = ctx.currentTime;
                _playStartOffset = _currentPlayPosition;
            } else {
                _currentPlayPosition = elapsed;
            }
        }
        return _currentPlayPosition;
    }

    function getDuration() {
        let maxEnd = 0;
        _tracks.forEach(track => {
            track.clips.forEach(clip => {
                const end = clip.startTime + clip.duration;
                if (end > maxEnd) maxEnd = end;
            });
        });
        // 至少10秒
        return Math.max(10, maxEnd);
    }

    function setBPM(bpm) {
        _bpm = clamp(bpm, 20, 300);
        renderAll();
    }

    function setTimeSignature(num, den) {
        _timeSigNum = clamp(num, 1, 16);
        _timeSigDen = clamp(den, 1, 16);
        renderAll();
    }

    function setTrackVolume(trackId, vol) {
        const track = getTrackById(trackId);
        if (!track) return;
        track.volume = clamp(vol, 0, 1);
        track.gainNode.gain.setTargetAtTime(isTrackAudible(track) ? track.volume : 0, getAudioContext().currentTime, 0.02);
        renderTrackHeaders();
    }

    function setTrackPan(trackId, pan) {
        const track = getTrackById(trackId);
        if (!track) return;
        track.pan = clamp(pan, -1, 1);
        track.panNode.pan.setTargetAtTime(track.pan, getAudioContext().currentTime, 0.02);
        renderTrackHeaders();
    }

    function setTrackMute(trackId, bool) {
        const track = getTrackById(trackId);
        if (!track) return;
        track.mute = !!bool;
        updateTrackAudioRouting();
        renderTrackHeaders();
    }

    function setTrackSolo(trackId, bool) {
        const track = getTrackById(trackId);
        if (!track) return;
        track.solo = !!bool;
        updateTrackAudioRouting();
        renderTrackHeaders();
    }

    // ───────────────────── 动画循环 ─────────────────────
    function startAnimationLoop() {
        if (_animFrameId) return;
        function loop() {
            _animFrameId = requestAnimationFrame(loop);
            _currentPlayPosition = getCurrentTime();
            renderPlayhead();
            updateVUMeters();
            updateTimeDisplay();
        }
        loop();
    }

    function stopAnimationLoop() {
        if (_animFrameId) {
            cancelAnimationFrame(_animFrameId);
            _animFrameId = null;
        }
        updateVUMeters();
    }

    // ───────────────────── VU表更新 ─────────────────────
    function updateVUMeters() {
        _tracks.forEach(track => {
            const meter = _vuMeters[track.id];
            if (!meter || !meter.analyser) return;
            const data = new Float32Array(meter.analyser.fftSize);
            meter.analyser.getFloatTimeDomainData(data);
            let peak = 0, rms = 0;
            for (let i = 0; i < data.length; i++) {
                const v = Math.abs(data[i]);
                if (v > peak) peak = v;
                rms += data[i] * data[i];
            }
            rms = Math.sqrt(rms / data.length);
            meter.peak = peak;
            meter.rms = rms;
        });
        // 更新混音器面板的VU条
        if (_mixerVisible) renderMixerPanel();
    }

    // ───────────────────── 渲染系统 ─────────────────────
    function pixelsPerBeat() {
        return PIXELS_PER_BEAT_BASE * _zoom;
    }

    function timeToPixel(time) {
        const beats = secondsToBeats(time, _bpm);
        return beats * pixelsPerBeat() - _scrollX;
    }

    function pixelToTime(px) {
        const beats = (px + _scrollX) / pixelsPerBeat();
        return beatsToSeconds(beats, _bpm);
    }

    function renderAll() {
        renderTimeline();
        renderTrackLanes();
        renderTrackHeaders();
        renderPlayhead();
        updateTransportUI();
        updateTimeDisplay();
        if (_mixerVisible) renderMixerPanel();
        if (_pianoRollVisible) renderPianoRoll();
        if (_lyricsVisible) renderLyricsPanel();
    }

    // ───────────────────── DOM构建 ─────────────────────
    function buildUI() {
        if (!_container) return;
        _container.innerHTML = '';
        _container.className = 'te-container';

        // 传输控制栏
        const transport = document.createElement('div');
        transport.className = 'te-transport';
        transport.innerHTML = buildTransportHTML();
        _container.appendChild(transport);

        // 主区域: 轨道头 + 时间线 + 轨道内容
        const mainArea = document.createElement('div');
        mainArea.className = 'te-main-area';

        // 左侧轨道头
        const headerCol = document.createElement('div');
        headerCol.className = 'te-track-headers-col';
        headerCol.innerHTML = '<div class="te-header-spacer"></div><div class="te-track-headers"></div>';
        mainArea.appendChild(headerCol);

        // 右侧时间线+轨道内容
        const contentCol = document.createElement('div');
        contentCol.className = 'te-content-col';

        const timelineCanvas = document.createElement('canvas');
        timelineCanvas.className = 'te-timeline-canvas';
        contentCol.appendChild(timelineCanvas);

        const lanesContainer = document.createElement('div');
        lanesContainer.className = 'te-lanes-container';
        const lanesCanvas = document.createElement('canvas');
        lanesCanvas.className = 'te-lanes-canvas';
        lanesContainer.appendChild(lanesCanvas);
        contentCol.appendChild(lanesContainer);

        mainArea.appendChild(contentCol);
        _container.appendChild(mainArea);

        // 混音器面板 (可切换)
        const mixerPanel = document.createElement('div');
        mixerPanel.className = 'te-mixer-panel te-hidden';
        _container.appendChild(mixerPanel);

        // 钢琴卷帘 (可切换)
        const pianoRollPanel = document.createElement('div');
        pianoRollPanel.className = 'te-piano-roll-panel te-hidden';
        _container.appendChild(pianoRollPanel);

        // 歌词面板 (可切换)
        const lyricsPanel = document.createElement('div');
        lyricsPanel.className = 'te-lyrics-panel te-hidden';
        _container.appendChild(lyricsPanel);

        // 缓存元素
        _els = {
            transport,
            headerCol,
            headerSpacer: headerCol.querySelector('.te-header-spacer'),
            trackHeaders: headerCol.querySelector('.te-track-headers'),
            contentCol,
            timelineCanvas,
            lanesContainer,
            lanesCanvas,
            mixerPanel,
            pianoRollPanel,
            lyricsPanel,
        };

        // 绑定传输控制事件
        bindTransportEvents();
        // 绑定Canvas交互
        bindCanvasEvents();
        // 绑定窗口大小变化
        bindResizeEvent();
    }

    function buildTransportHTML() {
        return '<div class="te-transport-left">' +
            '<button class="te-btn te-btn-stop" title="停止">⏹</button>' +
            '<button class="te-btn te-btn-play" title="播放">▶</button>' +
            '<button class="te-btn te-btn-pause" title="暂停">⏸</button>' +
            '<button class="te-btn te-btn-record" title="录制">⏺</button>' +
            '<button class="te-btn te-btn-loop" title="循环">🔁</button>' +
            '</div>' +
            '<div class="te-transport-center">' +
            '<span class="te-time-display">00:00:000</span>' +
            '<span class="te-time-total">/ 00:10:000</span>' +
            '</div>' +
            '<div class="te-transport-right">' +
            '<button class="te-btn te-btn-zoom-in" title="放大">+</button>' +
            '<button class="te-btn te-btn-zoom-out" title="缩小">−</button>' +
            '<button class="te-btn te-btn-add-track" title="添加轨道">+</button>' +
            '<button class="te-btn te-btn-mixer" title="混音器">🎛</button>' +
            '<button class="te-btn te-btn-piano" title="钢琴卷帘">🎹</button>' +
            '<button class="te-btn te-btn-lyrics" title="歌词">📝</button>' +
            '<span class="te-bpm-display"><label>BPM</label><input type="number" class="te-bpm-input" value="' + _bpm + '" min="20" max="300"></span>' +
            '<span class="te-timesig-display"><input type="number" class="te-timesig-num" value="' + _timeSigNum + '" min="1" max="16">/<input type="number" class="te-timesig-den" value="' + _timeSigDen + '" min="1" max="16"></span>' +
            '</div>';
    }

    // ───────────────────── 传输控制事件 ─────────────────────
    function bindTransportEvents() {
        const t = _els.transport;
        if (!t) return;

        const playBtn = t.querySelector('.te-btn-play');
        const pauseBtn = t.querySelector('.te-btn-pause');
        const stopBtn = t.querySelector('.te-btn-stop');
        const recBtn = t.querySelector('.te-btn-record');
        const loopBtn = t.querySelector('.te-btn-loop');
        const zoomIn = t.querySelector('.te-btn-zoom-in');
        const zoomOut = t.querySelector('.te-btn-zoom-out');
        const addTrackBtn = t.querySelector('.te-btn-add-track');
        const mixerBtn = t.querySelector('.te-btn-mixer');
        const pianoBtn = t.querySelector('.te-btn-piano');
        const bpmInput = t.querySelector('.te-bpm-input');
        const tsNumInput = t.querySelector('.te-timesig-num');
        const tsDenInput = t.querySelector('.te-timesig-den');

        playBtn.addEventListener('click', function () { play(); });
        pauseBtn.addEventListener('click', function () { pause(); });
        stopBtn.addEventListener('click', function () { stop(); });
        recBtn.addEventListener('click', function () { record(); });
        loopBtn.addEventListener('click', function () {
            _isLooping = !_isLooping;
            if (_isLooping && _loopEnd <= _loopStart) {
                _loopStart = 0;
                _loopEnd = getDuration();
            }
            updateTransportUI();
        });
        zoomIn.addEventListener('click', function () {
            _zoom = clamp(_zoom * 1.3, 0.1, 10);
            renderAll();
        });
        zoomOut.addEventListener('click', function () {
            _zoom = clamp(_zoom / 1.3, 0.1, 10);
            renderAll();
        });
        addTrackBtn.addEventListener('click', function () {
            if (_tracks.length >= MAX_TRACKS) {
                if (window.showToast) window.showToast('最多支持' + MAX_TRACKS + '条轨道', 'warning');
                return;
            }
            const track = createTrack();
            if (track) {
                renderAll();
            }
        });
        mixerBtn.addEventListener('click', function () {
            _mixerVisible = !_mixerVisible;
            _els.mixerPanel.classList.toggle('te-hidden', !_mixerVisible);
            if (_mixerVisible) renderMixerPanel();
        });
        pianoBtn.addEventListener('click', function () {
            _pianoRollVisible = !_pianoRollVisible;
            _els.pianoRollPanel.classList.toggle('te-hidden', !_pianoRollVisible);
            if (_pianoRollVisible) {
                _pianoRollTrackId = _tracks.length > 0 ? _tracks[0].id : null;
                renderPianoRoll();
            }
        });
        const lyricsBtn = t.querySelector('.te-btn-lyrics');
        lyricsBtn.addEventListener('click', function () {
            _lyricsVisible = !_lyricsVisible;
            _els.lyricsPanel.classList.toggle('te-hidden', !_lyricsVisible);
            if (_lyricsVisible) renderLyricsPanel();
        });
        bpmInput.addEventListener('change', function () {
            setBPM(parseInt(this.value) || DEFAULT_BPM);
            bpmInput.value = _bpm;
        });
        tsNumInput.addEventListener('change', function () {
            setTimeSignature(parseInt(this.value) || 4, _timeSigDen);
            tsNumInput.value = _timeSigNum;
        });
        tsDenInput.addEventListener('change', function () {
            setTimeSignature(_timeSigNum, parseInt(this.value) || 4);
            tsDenInput.value = _timeSigDen;
        });
    }

    function updateTransportUI() {
        const t = _els.transport;
        if (!t) return;
        const playBtn = t.querySelector('.te-btn-play');
        const pauseBtn = t.querySelector('.te-btn-pause');
        const stopBtn = t.querySelector('.te-btn-stop');
        const recBtn = t.querySelector('.te-btn-record');
        const loopBtn = t.querySelector('.te-btn-loop');

        playBtn.classList.toggle('te-btn-active', _isPlaying && !_isPaused);
        pauseBtn.classList.toggle('te-btn-active', _isPaused);
        stopBtn.classList.toggle('te-btn-active', !_isPlaying);
        recBtn.classList.toggle('te-btn-active', _isRecording);
        loopBtn.classList.toggle('te-btn-active', _isLooping);
    }

    function updateTimeDisplay() {
        const t = _els.transport;
        if (!t) return;
        const timeDisp = t.querySelector('.te-time-display');
        const totalDisp = t.querySelector('.te-time-total');
        if (timeDisp) timeDisp.textContent = formatTime(getCurrentTime());
        if (totalDisp) totalDisp.textContent = '/ ' + formatTime(getDuration());
    }

    // ───────────────────── Canvas事件绑定 ─────────────────────
    function bindCanvasEvents() {
        const tlCanvas = _els.timelineCanvas;
        const lanesCanvas = _els.lanesCanvas;
        const lanesContainer = _els.lanesContainer;

        // 时间线点击定位
        tlCanvas.addEventListener('mousedown', onTimelineMouseDown);
        tlCanvas.addEventListener('touchstart', onTimelineTouchStart, { passive: false });

        // 轨道区域交互 (剪辑拖拽、选择等)
        lanesCanvas.addEventListener('mousedown', onLanesMouseDown);
        lanesCanvas.addEventListener('touchstart', onLanesTouchStart, { passive: false });

        // 全局鼠标/触摸移动和释放
        document.addEventListener('mousemove', onGlobalMouseMove);
        document.addEventListener('mouseup', onGlobalMouseUp);
        document.addEventListener('touchmove', onGlobalTouchMove, { passive: false });
        document.addEventListener('touchend', onGlobalTouchEnd);

        // 滚动
        lanesContainer.addEventListener('wheel', onLanesWheel, { passive: false });

        // Mobile pinch-to-zoom
        var pinchStartDist = 0;
        var pinchStartZoom = 1;
        lanesContainer.addEventListener('touchstart', function(e) {
            if (e.touches.length === 2) {
                e.preventDefault();
                var dx = e.touches[0].clientX - e.touches[1].clientX;
                var dy = e.touches[0].clientY - e.touches[1].clientY;
                pinchStartDist = Math.sqrt(dx * dx + dy * dy);
                pinchStartZoom = _zoom;
            }
        }, { passive: false });
        lanesContainer.addEventListener('touchmove', function(e) {
            if (e.touches.length === 2) {
                e.preventDefault();
                var dx = e.touches[0].clientX - e.touches[1].clientX;
                var dy = e.touches[0].clientY - e.touches[1].clientY;
                var dist = Math.sqrt(dx * dx + dy * dy);
                if (pinchStartDist > 0) {
                    _zoom = clamp(pinchStartZoom * (dist / pinchStartDist), 0.1, 10);
                    renderAll();
                }
            }
        }, { passive: false });
        lanesContainer.addEventListener('touchend', function() {
            pinchStartDist = 0;
        });
    }

    // 时间线鼠标点击 → 定位播放头
    function onTimelineMouseDown(e) {
        const rect = _els.timelineCanvas.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const time = pixelToTime(x);
        seek(time);
    }
    function onTimelineTouchStart(e) {
        e.preventDefault();
        const rect = _els.timelineCanvas.getBoundingClientRect();
        const x = e.touches[0].clientX - rect.left;
        const time = pixelToTime(x);
        seek(time);
    }

    // 轨道区域鼠标按下 → 检测剪辑交互
    function onLanesMouseDown(e) {
        const rect = _els.lanesCanvas.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        handleLanesPointerDown(x, y);
    }
    function onLanesTouchStart(e) {
        // Don't preventDefault immediately — allow native scrolling to work.
        // Only prevent and handle when user taps on a clip (drag interaction).
        if (isMobileView()) {
            // In mobile segment mode, let native scroll work unless tapping on a clip
            const rect = _els.lanesCanvas.getBoundingClientRect();
            const x = e.touches[0].clientX - rect.left;
            const y = e.touches[0].clientY - rect.top;
            const pixPerBeat = pixelsPerBeat();
            const containerW = _els.lanesContainer.clientWidth;
            const segmentBeats = containerW / pixPerBeat;
            const segmentDuration = (segmentBeats * 60) / _bpm;
            if (segmentDuration <= 0) return;
            const totalDuration = getDuration();
            const numSegments = Math.max(1, Math.ceil(totalDuration / segmentDuration));
            const segmentLabelHeight = 22;
            const segmentHeight = _tracks.length * TRACK_LANE_HEIGHT + segmentLabelHeight;
            const segIdx = Math.floor(y / segmentHeight);
            if (segIdx < 0 || segIdx >= numSegments) return;
            const segStart = segIdx * segmentDuration;
            const localY = y - segIdx * segmentHeight - segmentLabelHeight;
            const trackIdx = Math.floor(localY / TRACK_LANE_HEIGHT);
            if (trackIdx < 0 || trackIdx >= _tracks.length) return;
            const track = _tracks[trackIdx];
            const clickTime = segStart + (x / containerW) * segmentDuration;
            // Only prevent scroll if user tapped on an actual clip
            let clickedClip = null;
            for (let i = track.clips.length - 1; i >= 0; i--) {
                const clip = track.clips[i];
                if (clickTime >= clip.startTime && clickTime <= clip.startTime + clip.duration) {
                    clickedClip = clip;
                    break;
                }
            }
            if (clickedClip) {
                e.preventDefault();
                handleLanesPointerDown(x, y);
            }
            // Otherwise: don't prevent — allow native scrolling
        } else {
            // Desktop touch (touchscreen monitor): original behavior
            e.preventDefault();
            const rect = _els.lanesCanvas.getBoundingClientRect();
            const x = e.touches[0].clientX - rect.left;
            const y = e.touches[0].clientY - rect.top;
            handleLanesPointerDown(x, y);
        }
    }

    function handleLanesPointerDown(x, y) {
        if (isMobileView()) {
            // Mobile segment mode: calculate which segment and track was clicked
            const pixPerBeat = pixelsPerBeat();
            const containerW = _els.lanesContainer.clientWidth;
            const segmentBeats = containerW / pixPerBeat;
            const segmentDuration = (segmentBeats * 60) / _bpm;
            if (segmentDuration <= 0) return;
            const totalDuration = getDuration();
            const numSegments = Math.max(1, Math.ceil(totalDuration / segmentDuration));
            const segmentLabelHeight = 22;
            const segmentHeight = _tracks.length * TRACK_LANE_HEIGHT + segmentLabelHeight;
            const segIdx = Math.floor(y / segmentHeight);
            if (segIdx < 0 || segIdx >= numSegments) return;
            const segStart = segIdx * segmentDuration;
            const localY = y - segIdx * segmentHeight - segmentLabelHeight;
            const trackIdx = Math.floor(localY / TRACK_LANE_HEIGHT);
            if (trackIdx < 0 || trackIdx >= _tracks.length) {
                _selectedClipTrackId = null;
                _selectedClipId = null;
                renderTrackLanes();
                return;
            }
            const track = _tracks[trackIdx];
            const clickTime = segStart + (x / containerW) * segmentDuration;
            // Find clicked clip
            let clickedClip = null;
            for (let i = track.clips.length - 1; i >= 0; i--) {
                const clip = track.clips[i];
                if (clickTime >= clip.startTime && clickTime <= clip.startTime + clip.duration) {
                    clickedClip = clip;
                    break;
                }
            }
            if (clickedClip) {
                _selectedClipTrackId = track.id;
                _selectedClipId = clickedClip.id;
                // Set up drag state for mobile
                _dragState = {
                    type: 'clip-move',
                    trackId: track.id,
                    clipId: clickedClip.id,
                    origStart: clickedClip.startTime,
                    startX: x,
                    moved: false,
                    mobileSegIdx: segIdx,
                    mobileSegDuration: segmentDuration,
                };
            } else {
                _selectedClipTrackId = null;
                _selectedClipId = null;
            }
            renderTrackLanes();
            return;
        }
        // Desktop mode: original logic
        // 确定点击了哪个轨道
        const trackIdx = Math.floor(y / TRACK_LANE_HEIGHT);
        if (trackIdx < 0 || trackIdx >= _tracks.length) {
            _selectedClipTrackId = null;
            _selectedClipId = null;
            renderTrackLanes();
            return;
        }
        const track = _tracks[trackIdx];

        // 确定点击了哪个剪辑
        const clickTime = pixelToTime(x);
        let clickedClip = null;
        // 从后往前搜索（后添加的在上面）
        for (let i = track.clips.length - 1; i >= 0; i--) {
            const clip = track.clips[i];
            if (clickTime >= clip.startTime && clickTime <= clip.startTime + clip.duration) {
                clickedClip = clip;
                break;
            }
        }

        if (clickedClip) {
            _selectedClipTrackId = track.id;
            _selectedClipId = clickedClip.id;

            // 判断是剪辑左边缘、右边缘还是中间
            const clipStartPx = timeToPixel(clickedClip.startTime);
            const clipEndPx = timeToPixel(clickedClip.startTime + clickedClip.duration);

            if (x - clipStartPx < CLIP_TRIM_EDGE) {
                _dragState = {
                    type: 'clip-trim-left',
                    trackId: track.id,
                    clipId: clickedClip.id,
                    origStart: clickedClip.startTime,
                    origDuration: clickedClip.duration,
                    origOffset: clickedClip.offset,
                    startX: x,
                };
            } else if (clipEndPx - x < CLIP_TRIM_EDGE) {
                _dragState = {
                    type: 'clip-trim-right',
                    trackId: track.id,
                    clipId: clickedClip.id,
                    origStart: clickedClip.startTime,
                    origDuration: clickedClip.duration,
                    startX: x,
                };
            } else {
                _dragState = {
                    type: 'clip-move',
                    trackId: track.id,
                    clipId: clickedClip.id,
                    origStart: clickedClip.startTime,
                    startX: x,
                    moved: false,
                };
            }
        } else {
            _selectedClipTrackId = null;
            _selectedClipId = null;
        }
        renderTrackLanes();
    }

    function onGlobalMouseMove(e) {
        if (!_dragState) return;
        const rect = _els.lanesCanvas.getBoundingClientRect();
        const x = e.clientX - rect.left;
        handleDragMove(x);
    }
    function onGlobalTouchMove(e) {
        if (!_dragState) return;
        e.preventDefault();
        const rect = _els.lanesCanvas.getBoundingClientRect();
        const x = e.touches[0].clientX - rect.left;
        handleDragMove(x);
    }

    function handleDragMove(x) {
        const ds = _dragState;
        const track = getTrackById(ds.trackId);
        if (!track) return;
        const clip = track.clips.find(c => c.id === ds.clipId);
        if (!clip) return;

        const dx = x - ds.startX;
        const dTime = pixelToTime(x) - pixelToTime(ds.startX);

        if (ds.type === 'clip-move') {
            clip.startTime = Math.max(0, ds.origStart + dTime);
            ds.moved = true;
        } else if (ds.type === 'clip-trim-left') {
            const newStart = Math.max(0, ds.origStart + dTime);
            const diff = newStart - ds.origStart;
            clip.startTime = newStart;
            clip.duration = Math.max(0.01, ds.origDuration - diff);
            clip.offset = (ds.origOffset || 0) + diff;
        } else if (ds.type === 'clip-trim-right') {
            clip.duration = Math.max(0.01, ds.origDuration + dTime);
        }

        renderTrackLanes();
    }

    function onGlobalMouseUp() {
        _dragState = null;
    }
    function onGlobalTouchEnd() {
        _dragState = null;
    }

    // 滚轮缩放和滚动
    function onLanesWheel(e) {
        if (e.ctrlKey || e.metaKey) {
            // 缩放
            e.preventDefault();
            const factor = e.deltaY < 0 ? 1.1 : 0.9;
            _zoom = clamp(_zoom * factor, 0.1, 10);
            renderAll();
        } else if (e.shiftKey) {
            // 水平滚动
            e.preventDefault();
            _scrollX = Math.max(0, _scrollX + e.deltaY);
            renderAll();
        } else if (isMobileView()) {
            // Mobile segment mode: canvas is tall, let native scroll work
            // But also update _scrollY for playhead tracking
            // Don't preventDefault — let the container scroll natively
            requestAnimationFrame(function() {
                var cont = _els.lanesContainer;
                if (cont) {
                    _scrollY = cont.scrollTop;
                    renderAll();
                }
            });
        } else {
            // Desktop mode: manual vertical scroll via _scrollY
            e.preventDefault();
            _scrollY = Math.max(0, _scrollY + e.deltaY);
            renderAll();
        }
    }

    // ───────────────────── Canvas渲染: 时间线 ─────────────────────
    function renderTimeline() {
        const canvas = _els.timelineCanvas;
        if (!canvas) return;
        const container = _els.contentCol;
        const w = container ? container.clientWidth : 800;
        const h = TIMELINE_HEIGHT;
        canvas.width = w * (window.devicePixelRatio || 1);
        canvas.height = h * (window.devicePixelRatio || 1);
        canvas.style.width = w + 'px';
        canvas.style.height = h + 'px';

        const ctx = canvas.getContext('2d');
        const dpr = window.devicePixelRatio || 1;
        ctx.scale(dpr, dpr);

        // 背景
        const cs = getComputedStyle(_container);
        ctx.fillStyle = cs.getPropertyValue('--bg-secondary') || '#231e17';
        ctx.fillRect(0, 0, w, h);

        const ppb = pixelsPerBeat();
        const beatsPerMeasure = _timeSigNum;
        const totalBeats = secondsToBeats(getDuration() + 30, _bpm);

        // 绘制拍线和小节线
        for (let beat = 0; beat <= totalBeats; beat++) {
            const x = beat * ppb - _scrollX;
            if (x < -2 || x > w + 2) continue;

            const isMeasure = (beat % beatsPerMeasure === 0);
            ctx.strokeStyle = isMeasure
                ? (cs.getPropertyValue('--text-muted') || '#7d7068')
                : (cs.getPropertyValue('--border') || '#3a3228');
            ctx.lineWidth = isMeasure ? 1.5 : 0.5;
            ctx.beginPath();
            ctx.moveTo(x, 0);
            ctx.lineTo(x, h);
            ctx.stroke();

            // 小节号
            if (isMeasure) {
                const measureNum = Math.floor(beat / beatsPerMeasure) + 1;
                ctx.fillStyle = cs.getPropertyValue('--text-secondary') || '#b5a898';
                ctx.font = '10px ' + (cs.getPropertyValue('--font-mono') || 'monospace');
                ctx.fillText(String(measureNum), x + 3, 12);
            }
        }

        // 循环区域
        if (_isLooping && _loopEnd > _loopStart) {
            const lx1 = timeToPixel(_loopStart);
            const lx2 = timeToPixel(_loopEnd);
            ctx.fillStyle = hexToRgba('#e8853d', 0.15);
            ctx.fillRect(lx1, 0, lx2 - lx1, h);
        }
    }

    // ───────────────────── Canvas渲染: 轨道内容 ─────────────────────
    function renderTrackLanes() {
        const canvas = _els.lanesCanvas;
        const container = _els.lanesContainer;
        if (!canvas || !container) return;

        const w = container.clientWidth;
        const dpr = window.devicePixelRatio || 1;
        const mobile = isMobileView();

        if (mobile) {
            // Mobile segment mode: break timeline into screen-width segments, stack vertically
            const pixPerBeat = pixelsPerBeat();
            const segmentBeats = w / pixPerBeat; // beats that fit in one segment
            const segmentDuration = (segmentBeats * 60) / _bpm; // seconds per segment
            if (segmentDuration <= 0) return;
            const totalDuration = getDuration();
            const numSegments = Math.max(1, Math.ceil(totalDuration / segmentDuration));
            const numTracks = _tracks.length;
            const segmentHeight = numTracks * TRACK_LANE_HEIGHT;
            const segmentLabelHeight = 22;
            const totalH = numSegments * (segmentHeight + segmentLabelHeight);
            canvas.width = w * dpr;
            canvas.height = totalH * dpr;
            canvas.style.width = w + 'px';
            canvas.style.height = totalH + 'px';
            const ctx = canvas.getContext('2d');
            ctx.scale(dpr, dpr);
            const cs = getComputedStyle(_container);

            // Clear
            ctx.fillStyle = cs.getPropertyValue('--bg-secondary') || '#1a1510';
            ctx.fillRect(0, 0, w, totalH);

            for (let seg = 0; seg < numSegments; seg++) {
                const segStart = seg * segmentDuration;
                const segEnd = Math.min(segStart + segmentDuration, totalDuration);
                const yOffset = seg * (segmentHeight + segmentLabelHeight);

                // Segment time label
                ctx.fillStyle = 'rgba(232,133,61,0.15)';
                ctx.fillRect(0, yOffset, w, segmentLabelHeight);
                ctx.fillStyle = cs.getPropertyValue('--text-muted') || '#888';
                ctx.font = '10px ' + (cs.getPropertyValue('--font-mono') || 'monospace');
                const startStr = formatTime(segStart);
                const endStr = formatTime(segEnd);
                ctx.fillText(startStr + ' - ' + endStr, 4, yOffset + 14);

                // Draw tracks in this segment
                _tracks.forEach(function(track, i) {
                    const trackY = yOffset + segmentLabelHeight + i * TRACK_LANE_HEIGHT;
                    // Track background
                    ctx.fillStyle = i % 2 === 0
                        ? (cs.getPropertyValue('--bg-primary') || '#1a1510')
                        : (cs.getPropertyValue('--bg-tertiary') || '#16120d');
                    ctx.fillRect(0, trackY, w, TRACK_LANE_HEIGHT);
                    // Track name overlay with color accent
                    ctx.fillStyle = hexToRgba(track.color, 0.85);
                    ctx.fillRect(0, trackY, 6, TRACK_LANE_HEIGHT);
                    ctx.fillStyle = cs.getPropertyValue('--text-primary') || '#f5f0eb';
                    ctx.font = 'bold 11px ' + (cs.getPropertyValue('--font-sans') || 'sans-serif');
                    ctx.fillText(track.name, 10, trackY + 14);
                    // Volume indicator
                    ctx.font = '9px ' + (cs.getPropertyValue('--font-mono') || 'monospace');
                    ctx.fillStyle = cs.getPropertyValue('--text-muted') || '#7d7068';
                    ctx.fillText('Vol:' + Math.round(track.volume * 100) + '%', 10, trackY + 28);
                    if (track.mute) { ctx.fillText('[M]', 60, trackY + 28); }
                    if (track.solo) { ctx.fillText('[S]', 80, trackY + 28); }
                    // Track border
                    ctx.strokeStyle = cs.getPropertyValue('--border') || '#3a3228';
                    ctx.lineWidth = 0.5;
                    ctx.beginPath();
                    ctx.moveTo(0, trackY + TRACK_LANE_HEIGHT);
                    ctx.lineTo(w, trackY + TRACK_LANE_HEIGHT);
                    ctx.stroke();
                    // Mute overlay
                    if (track.mute || (!isTrackAudible(track))) {
                        ctx.fillStyle = 'rgba(0,0,0,0.4)';
                        ctx.fillRect(0, trackY, w, TRACK_LANE_HEIGHT);
                    }
                    // Draw clips in this segment
                    track.clips.forEach(function(clip) {
                        // Calculate clip position within this segment
                        const clipStartInSeg = clip.startTime - segStart;
                        const clipEndInSeg = clip.startTime + clip.duration - segStart;
                        if (clipEndInSeg <= 0 || clipStartInSeg >= segmentDuration) return;
                        const visibleStart = Math.max(0, clipStartInSeg);
                        const visibleEnd = Math.min(segmentDuration, clipEndInSeg);
                        const clipX = (visibleStart / segmentDuration) * w;
                        const clipW = ((visibleEnd - visibleStart) / segmentDuration) * w;
                        const isSelected = (_selectedClipTrackId === track.id && _selectedClipId === clip.id);
                        const clipY = trackY + 4;
                        const clipH = TRACK_LANE_HEIGHT - 8;
                        // Clip background
                        ctx.fillStyle = hexToRgba(track.color, isSelected ? 0.6 : 0.35);
                        ctx.fillRect(clipX, clipY, clipW, clipH);
                        // Selected border
                        if (isSelected) {
                            ctx.strokeStyle = track.color;
                            ctx.lineWidth = 2;
                            ctx.strokeRect(clipX, clipY, clipW, clipH);
                        }
                        // Waveform rendering for mobile segment
                        if (clip.waveformPeaks && clipW > 4) {
                            renderWaveformSegment(ctx, clip.waveformPeaks, clipX, clipY, clipW, clipH, track.color, visibleStart, visibleEnd, clip.startTime, clip.duration);
                        }
                        // Clip name
                        if (clipW > 30) {
                            ctx.fillStyle = cs.getPropertyValue('--text-primary') || '#f5f0eb';
                            ctx.font = '9px ' + (cs.getPropertyValue('--font-mono') || 'monospace');
                            ctx.save();
                            ctx.beginPath();
                            ctx.rect(clipX + 3, clipY, clipW - 6, clipH);
                            ctx.clip();
                            ctx.fillText(clip.name, clipX + 4, clipY + 12);
                            ctx.restore();
                        }
                        // Trim handles
                        if (isSelected) {
                            ctx.fillStyle = hexToRgba('#ffffff', 0.4);
                            ctx.fillRect(clipX, clipY, CLIP_TRIM_EDGE, clipH);
                            ctx.fillRect(clipX + clipW - CLIP_TRIM_EDGE, clipY, CLIP_TRIM_EDGE, clipH);
                        }
                    });
                });
            }
            // Playhead in mobile mode
            const currentTime = getCurrentTime();
            const currentSeg = Math.floor(currentTime / segmentDuration);
            if (currentSeg >= 0 && currentSeg < numSegments) {
                const segStart = currentSeg * segmentDuration;
                const px = ((currentTime - segStart) / segmentDuration) * w;
                const yOffset = currentSeg * (segmentHeight + segmentLabelHeight) + segmentLabelHeight;
                ctx.strokeStyle = '#e85d5d';
                ctx.lineWidth = 1.5;
                ctx.beginPath();
                ctx.moveTo(px, yOffset);
                ctx.lineTo(px, yOffset + segmentHeight);
                ctx.stroke();
                ctx.fillStyle = '#e85d5d';
                ctx.beginPath();
                ctx.moveTo(px - 5, yOffset);
                ctx.lineTo(px + 5, yOffset);
                ctx.lineTo(px, yOffset + 6);
                ctx.closePath();
                ctx.fill();
            }
        } else {
            // Desktop mode: original rendering
            const h = _tracks.length * TRACK_LANE_HEIGHT;
            canvas.width = w * dpr;
            canvas.height = h * dpr;
            canvas.style.width = w + 'px';
            canvas.style.height = h + 'px';
            container.scrollTop = _scrollY;

            const ctx = canvas.getContext('2d');
            ctx.scale(dpr, dpr);

            const cs = getComputedStyle(_container);

            // 逐轨道渲染
            _tracks.forEach(function (track, trackIdx) {
                const y = trackIdx * TRACK_LANE_HEIGHT;

                // 轨道背景
                ctx.fillStyle = trackIdx % 2 === 0
                    ? (cs.getPropertyValue('--bg-primary') || '#1a1510')
                    : (cs.getPropertyValue('--bg-tertiary') || '#16120d');
                ctx.fillRect(0, y, w, TRACK_LANE_HEIGHT);

                // 轨道分隔线
                ctx.strokeStyle = cs.getPropertyValue('--border') || '#3a3228';
                ctx.lineWidth = 0.5;
                ctx.beginPath();
                ctx.moveTo(0, y + TRACK_LANE_HEIGHT);
                ctx.lineTo(w, y + TRACK_LANE_HEIGHT);
                ctx.stroke();

                // 静音轨道半透明遮罩
                if (track.mute || (!isTrackAudible(track))) {
                    ctx.fillStyle = 'rgba(0,0,0,0.4)';
                    ctx.fillRect(0, y, w, TRACK_LANE_HEIGHT);
                }

                // 绘制剪辑
                track.clips.forEach(function (clip) {
                    renderClip(ctx, track, clip, y, w, cs);
                });
            });

            // 播放头
            renderPlayheadOnCanvas(ctx, w, h);
        }
    }

    // Waveform rendering helper for mobile segment mode (maps peaks to segment-local coordinates)
    function renderWaveformSegment(ctx, peaks, x, y, w, h, color, visibleStart, visibleEnd, clipStartTime, clipDuration) {
        const midY = y + h / 2;
        const halfH = h / 2 - 2;
        // Map visible portion of the clip to the segment-local pixel range
        const clipLocalStart = visibleStart - clipStartTime;
        const clipLocalEnd = visibleEnd - clipStartTime;
        const startFrac = clipLocalStart / clipDuration;
        const endFrac = clipLocalEnd / clipDuration;

        ctx.fillStyle = hexToRgba(color, 0.7);

        for (let i = 0; i < w; i++) {
            const frac = startFrac + (i / w) * (endFrac - startFrac);
            const idx = Math.floor(frac * peaks.length);
            const valL = (idx >= 0 && idx < peaks.left.length) ? peaks.left[idx] : 0;
            const valR = (idx >= 0 && idx < peaks.right.length) ? peaks.right[idx] : 0;
            const topH = valL * halfH;
            const botH = valR * halfH;
            ctx.globalAlpha = 0.8;
            ctx.fillRect(x + i, midY - topH, 1, topH);
            ctx.fillRect(x + i, midY, 1, botH);
        }
        ctx.globalAlpha = 1;
    }

    function renderClip(ctx, track, clip, trackY, canvasWidth, cs) {
        const x = timeToPixel(clip.startTime);
        const clipWidth = Math.max(CLIP_MIN_WIDTH, (clip.duration * pixelsPerBeat() * _bpm) / 60);
        if (x + clipWidth < 0 || x > canvasWidth) return;

        const isSelected = (_selectedClipTrackId === track.id && _selectedClipId === clip.id);
        const clipY = trackY + 4;
        const clipH = TRACK_LANE_HEIGHT - 8;

        // 剪辑背景
        ctx.fillStyle = hexToRgba(track.color, isSelected ? 0.6 : 0.35);
        ctx.fillRect(x, clipY, clipWidth, clipH);

        // 选中高亮边框
        if (isSelected) {
            ctx.strokeStyle = track.color;
            ctx.lineWidth = 2;
            ctx.strokeRect(x, clipY, clipWidth, clipH);
        }

        // 波形渲染
        if (clip.waveformPeaks) {
            renderWaveform(ctx, clip.waveformPeaks, x, clipY, clipWidth, clipH, track.color);
        }

        // 剪辑名称
        ctx.fillStyle = cs.getPropertyValue('--text-primary') || '#f5f0eb';
        ctx.font = '10px ' + (cs.getPropertyValue('--font-mono') || 'monospace');
        const nameMaxW = clipWidth - 8;
        if (nameMaxW > 10) {
            ctx.save();
            ctx.beginPath();
            ctx.rect(x + 4, clipY, clipWidth - 8, clipH);
            ctx.clip();
            ctx.fillText(clip.name, x + 4, clipY + 12);
            ctx.restore();
        }

        // 裁剪热区指示
        if (isSelected) {
            ctx.fillStyle = hexToRgba('#ffffff', 0.4);
            ctx.fillRect(x, clipY, CLIP_TRIM_EDGE, clipH);        // 左
            ctx.fillRect(x + clipWidth - CLIP_TRIM_EDGE, clipY, CLIP_TRIM_EDGE, clipH); // 右
        }
    }

    function renderWaveform(ctx, peaks, x, y, w, h, color) {
        const midY = y + h / 2;
        const halfH = h / 2 - 2;
        const step = peaks.length / w;

        ctx.fillStyle = hexToRgba(color, 0.7);

        for (let i = 0; i < w; i++) {
            const idx = Math.floor(i * step);
            // 左声道(上半部分)
            const valL = (idx < peaks.left.length) ? peaks.left[idx] : 0;
            const valR = (idx < peaks.right.length) ? peaks.right[idx] : 0;
            const topH = valL * halfH;
            const botH = valR * halfH;

            ctx.globalAlpha = 0.8;
            ctx.fillRect(x + i, midY - topH, 1, topH);    // 上半 = 左声道
            ctx.fillRect(x + i, midY, 1, botH);            // 下半 = 右声道
        }
        ctx.globalAlpha = 1;
    }

    // ───────────────────── 播放头渲染 ─────────────────────
    function renderPlayhead() {
        const canvas = _els.lanesCanvas;
        if (!canvas) return;
        // 重绘整个轨道区域（简化实现）
        renderTrackLanes();
    }

    function renderPlayheadOnCanvas(ctx, w, h) {
        const px = timeToPixel(getCurrentTime());
        if (px < 0 || px > w) return;

        ctx.strokeStyle = '#e85d5d';
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.moveTo(px, 0);
        ctx.lineTo(px, h);
        ctx.stroke();

        // 播放头顶部小三角
        ctx.fillStyle = '#e85d5d';
        ctx.beginPath();
        ctx.moveTo(px - 5, 0);
        ctx.lineTo(px + 5, 0);
        ctx.lineTo(px, 6);
        ctx.closePath();
        ctx.fill();
    }

    // ───────────────────── 轨道头渲染 ─────────────────────
    function renderTrackHeaders() {
        const container = _els.trackHeaders;
        if (!container) return;
        container.innerHTML = '';

        _tracks.forEach(function (track) {
            const header = document.createElement('div');
            header.className = 'te-track-header';
            header.style.borderLeftColor = track.color;

            header.innerHTML =
                '<div class="te-track-header-top">' +
                '<span class="te-track-color" style="background:' + track.color + '"></span>' +
                '<span class="te-track-name">' + escapeHtml(track.name) + '</span>' +
                '<button class="te-track-btn te-track-remove" title="删除轨道">✕</button>' +
                '</div>' +
                '<div class="te-track-controls">' +
                '<div class="te-track-vol-row">' +
                '<span class="te-track-label">音量</span>' +
                '<input type="range" class="te-vol-slider" min="0" max="100" value="' + Math.round(track.volume * 100) + '" data-track="' + track.id + '">' +
                '</div>' +
                '<div class="te-track-pan-row">' +
                '<span class="te-track-label">声像</span>' +
                '<input type="range" class="te-pan-slider" min="-100" max="100" value="' + Math.round(track.pan * 100) + '" data-track="' + track.id + '">' +
                '</div>' +
                '<div class="te-track-btns">' +
                '<button class="te-track-btn te-btn-mute' + (track.mute ? ' te-btn-active' : '') + '" data-track="' + track.id + '" title="静音">M</button>' +
                '<button class="te-track-btn te-btn-solo' + (track.solo ? ' te-btn-active' : '') + '" data-track="' + track.id + '" title="独奏">S</button>' +
                '<button class="te-track-btn te-btn-arm' + (track.armed ? ' te-btn-active' : '') + '" data-track="' + track.id + '" title="录制准备">⏺</button>' +
                '<button class="te-track-btn te-btn-load" data-track="' + track.id + '" title="加载音频">📂</button>' +
                '</div>' +
                '</div>';

            // 事件绑定
            const volSlider = header.querySelector('.te-vol-slider');
            const panSlider = header.querySelector('.te-pan-slider');
            const muteBtn = header.querySelector('.te-btn-mute');
            const soloBtn = header.querySelector('.te-btn-solo');
            const armBtn = header.querySelector('.te-btn-arm');
            const removeBtn = header.querySelector('.te-track-remove');
            const loadBtn = header.querySelector('.te-btn-load');
            const nameSpan = header.querySelector('.te-track-name');

            volSlider.addEventListener('input', function () {
                setTrackVolume(track.id, parseInt(this.value) / 100);
            });
            panSlider.addEventListener('input', function () {
                setTrackPan(track.id, parseInt(this.value) / 100);
            });
            muteBtn.addEventListener('click', function () {
                setTrackMute(track.id, !track.mute);
            });
            soloBtn.addEventListener('click', function () {
                setTrackSolo(track.id, !track.solo);
            });
            armBtn.addEventListener('click', function () {
                track.armed = !track.armed;
                renderTrackHeaders();
            });
            removeBtn.addEventListener('click', function () {
                removeTrackById(track.id);
                renderAll();
            });
            loadBtn.addEventListener('click', function () {
                // 创建文件选择器
                const input = document.createElement('input');
                input.type = 'file';
                input.accept = 'audio/*';
                input.addEventListener('change', function () {
                    if (this.files && this.files[0]) {
                        loadAudioFileToTrack(track.id, this.files[0], getCurrentTime())
                            .then(function () {
                                if (window.showToast) window.showToast('音频已加载到 ' + track.name, 'success');
                            })
                            .catch(function (err) {
                                if (window.showToast) window.showToast(err.message, 'error');
                            });
                    }
                });
                input.click();
            });

            // 双击重命名
            nameSpan.addEventListener('dblclick', function () {
                const newName = prompt('重命名轨道:', track.name);
                if (newName && newName.trim()) {
                    track.name = newName.trim();
                    renderTrackHeaders();
                }
            });

            container.appendChild(header);
        });
    }

    // ───────────────────── 混音器面板渲染 ─────────────────────
    function renderMixerPanel() {
        const panel = _els.mixerPanel;
        if (!panel || !_mixerVisible) return;
        panel.innerHTML = '';

        const channelsContainer = document.createElement('div');
        channelsContainer.className = 'te-mixer-channels';

        // 各轨道通道
        _tracks.forEach(function (track) {
            const meter = _vuMeters[track.id] || { peak: 0, rms: 0 };
            const ch = document.createElement('div');
            ch.className = 'te-mixer-channel';

            const peakDb = meter.peak > 0 ? 20 * Math.log10(meter.peak) : -60;
            const peakNorm = clamp((peakDb + 60) / 60, 0, 1);

            ch.innerHTML =
                '<div class="te-mixer-ch-name" style="color:' + track.color + '">' + escapeHtml(track.name) + '</div>' +
                '<div class="te-mixer-vu">' +
                '<div class="te-mixer-vu-fill" style="height:' + (peakNorm * 100) + '%;background:' + track.color + '"></div>' +
                '<div class="te-mixer-vu-peak" style="bottom:' + (peakNorm * 100) + '%;background:' + (peakNorm > 0.9 ? '#e85d5d' : track.color) + '"></div>' +
                '</div>' +
                '<input type="range" class="te-mixer-fader" min="0" max="100" value="' + Math.round(track.volume * 100) + '" data-track="' + track.id + '" orient="vertical">' +
                '<div class="te-mixer-pan-row">' +
                '<input type="range" class="te-mixer-pan" min="-100" max="100" value="' + Math.round(track.pan * 100) + '" data-track="' + track.id + '">' +
                '</div>' +
                '<div class="te-mixer-btns">' +
                '<button class="te-track-btn te-btn-mute' + (track.mute ? ' te-btn-active' : '') + '" data-track="' + track.id + '">M</button>' +
                '<button class="te-track-btn te-btn-solo' + (track.solo ? ' te-btn-active' : '') + '" data-track="' + track.id + '">S</button>' +
                '</div>';

            // 事件
            ch.querySelector('.te-mixer-fader').addEventListener('input', function () {
                setTrackVolume(track.id, parseInt(this.value) / 100);
            });
            ch.querySelector('.te-mixer-pan').addEventListener('input', function () {
                setTrackPan(track.id, parseInt(this.value) / 100);
            });
            ch.querySelector('.te-btn-mute').addEventListener('click', function () {
                setTrackMute(track.id, !track.mute);
            });
            ch.querySelector('.te-btn-solo').addEventListener('click', function () {
                setTrackSolo(track.id, !track.solo);
            });

            channelsContainer.appendChild(ch);
        });

        // 主通道
        const masterCh = document.createElement('div');
        masterCh.className = 'te-mixer-channel te-mixer-master';
        masterCh.innerHTML =
            '<div class="te-mixer-ch-name">主输出</div>' +
            '<div class="te-mixer-vu"><div class="te-mixer-vu-fill" style="height:60%;background:#e8853d"></div></div>' +
            '<input type="range" class="te-mixer-fader" min="0" max="100" value="' + Math.round((_masterGain ? _masterGain.gain.value : 0.8) * 100) + '" id="te-master-fader" orient="vertical">' +
            '<div class="te-mixer-ch-name" style="font-size:10px">主音量</div>';

        masterCh.querySelector('#te-master-fader').addEventListener('input', function () {
            if (_masterGain) _masterGain.gain.value = parseInt(this.value) / 100;
        });

        channelsContainer.appendChild(masterCh);
        panel.appendChild(channelsContainer);
    }

    // ───────────────────── 钢琴卷帘渲染 ─────────────────────
    function renderPianoRoll() {
        const panel = _els.pianoRollPanel;
        if (!panel || !_pianoRollVisible) return;
        panel.innerHTML = '';

        // 选择轨道
        const selector = document.createElement('div');
        selector.className = 'te-piano-selector';
        let selectHtml = '<select class="te-piano-track-select">';
        _tracks.forEach(function (t) {
            selectHtml += '<option value="' + t.id + '"' + (_pianoRollTrackId === t.id ? ' selected' : '') + '>' + escapeHtml(t.name) + '</option>';
        });
        selectHtml += '</select>';
        selector.innerHTML = selectHtml;
        panel.appendChild(selector);

        const selEl = selector.querySelector('.te-piano-track-select');
        selEl.addEventListener('change', function () {
            _pianoRollTrackId = this.value;
            renderPianoRoll();
        });

        if (!_pianoRollTrackId) return;

        // 钢琴卷帘Canvas
        const PIANO_KEY_WIDTH = 40;
        const NOTE_HEIGHT = 14;
        const TOTAL_NOTES = 36;  // 3个八度 C3-B5
        const BASE_PITCH = 48;   // C3 MIDI

        const canvas = document.createElement('canvas');
        canvas.className = 'te-piano-canvas';
        const containerW = panel.clientWidth || 600;
        const containerH = TOTAL_NOTES * NOTE_HEIGHT + 20;
        const dpr = window.devicePixelRatio || 1;
        canvas.width = containerW * dpr;
        canvas.height = containerH * dpr;
        canvas.style.width = containerW + 'px';
        canvas.style.height = containerH + 'px';
        panel.appendChild(canvas);

        const ctx = canvas.getContext('2d');
        ctx.scale(dpr, dpr);
        const cs = getComputedStyle(_container);

        // 背景
        ctx.fillStyle = cs.getPropertyValue('--bg-primary') || '#1a1510';
        ctx.fillRect(0, 0, containerW, containerH);

        // 钢琴键标签和网格
        const noteNames = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];
        const blackKeys = [1, 3, 6, 8, 10]; // C#, D#, F#, G#, A#

        for (let i = 0; i < TOTAL_NOTES; i++) {
            const midiNote = BASE_PITCH + (TOTAL_NOTES - 1 - i);
            const noteName = noteNames[midiNote % 12];
            const octave = Math.floor(midiNote / 12) - 1;
            const isBlack = blackKeys.indexOf(midiNote % 12) >= 0;
            const y = i * NOTE_HEIGHT;

            // 行背景
            ctx.fillStyle = isBlack
                ? (cs.getPropertyValue('--bg-tertiary') || '#16120d')
                : (cs.getPropertyValue('--bg-secondary') || '#231e17');
            ctx.fillRect(PIANO_KEY_WIDTH, y, containerW - PIANO_KEY_WIDTH, NOTE_HEIGHT);

            // 网格线
            ctx.strokeStyle = cs.getPropertyValue('--border') || '#3a3228';
            ctx.lineWidth = 0.3;
            ctx.beginPath();
            ctx.moveTo(PIANO_KEY_WIDTH, y);
            ctx.lineTo(containerW, y);
            ctx.stroke();

            // 钢琴键
            ctx.fillStyle = isBlack ? '#333' : '#eee';
            ctx.fillRect(0, y, PIANO_KEY_WIDTH - 1, NOTE_HEIGHT);
            ctx.fillStyle = isBlack ? '#ccc' : '#333';
            ctx.font = '8px sans-serif';
            if (noteName === 'C') {
                ctx.fillText('C' + octave, 4, y + NOTE_HEIGHT - 3);
            }
        }

        // 拍线
        const ppb = pixelsPerBeat();
        const gridStartX = PIANO_KEY_WIDTH;
        const totalBeats = secondsToBeats(getDuration() + 10, _bpm);
        for (let beat = 0; beat <= totalBeats; beat++) {
            const x = gridStartX + beat * ppb - _scrollX;
            if (x < gridStartX || x > containerW) continue;
            const isMeasure = (beat % _timeSigNum === 0);
            ctx.strokeStyle = isMeasure
                ? (cs.getPropertyValue('--text-muted') || '#7d7068')
                : (cs.getPropertyValue('--border') || '#3a3228');
            ctx.lineWidth = isMeasure ? 0.8 : 0.3;
            ctx.beginPath();
            ctx.moveTo(x, 0);
            ctx.lineTo(x, containerH);
            ctx.stroke();
        }

        // 渲染音符
        const track = getTrackById(_pianoRollTrackId);
        if (track) {
            const notes = _pianoRollNotes[_pianoRollTrackId] || [];
            notes.forEach(function (note) {
                const noteIdx = TOTAL_NOTES - 1 - (note.pitch - BASE_PITCH);
                if (noteIdx < 0 || noteIdx >= TOTAL_NOTES) return;
                const nx = gridStartX + secondsToBeats(note.start, _bpm) * ppb - _scrollX;
                const nw = Math.max(4, secondsToBeats(note.duration, _bpm) * ppb);
                const ny = noteIdx * NOTE_HEIGHT + 1;
                ctx.fillStyle = hexToRgba(track.color, 0.8);
                ctx.fillRect(nx, ny, nw, NOTE_HEIGHT - 2);
                ctx.strokeStyle = track.color;
                ctx.lineWidth = 0.5;
                ctx.strokeRect(nx, ny, nw, NOTE_HEIGHT - 2);
            });
        }

        // 钢琴卷帘点击添加/删除音符
        canvas.addEventListener('click', function (e) {
            const rect = canvas.getBoundingClientRect();
            const mx = e.clientX - rect.left;
            const my = e.clientY - rect.top;

            if (mx < PIANO_KEY_WIDTH) return;

            const noteIdx = Math.floor(my / NOTE_HEIGHT);
            const pitch = BASE_PITCH + (TOTAL_NOTES - 1 - noteIdx);
            const clickBeat = (mx - PIANO_KEY_WIDTH + _scrollX) / ppb;
            const clickTime = beatsToSeconds(clickBeat, _bpm);

            if (!_pianoRollTrackId) return;
            if (!_pianoRollNotes[_pianoRollTrackId]) _pianoRollNotes[_pianoRollTrackId] = [];

            const notes = _pianoRollNotes[_pianoRollTrackId];
            // 检查是否点击了已有音符
            const existingIdx = notes.findIndex(function (n) {
                return n.pitch === pitch && clickTime >= n.start && clickTime <= n.start + n.duration;
            });

            if (existingIdx >= 0) {
                notes.splice(existingIdx, 1);
            } else {
                notes.push({
                    id: uid(),
                    pitch: pitch,
                    start: clickTime,
                    duration: beatsToSeconds(1, _bpm), // 1拍
                });
            }
            renderPianoRoll();
        });
    }

    // ───────────────────── 窗口大小调整 ─────────────────────
    function bindResizeEvent() {
        let resizeTimer;
        window.addEventListener('resize', function () {
            clearTimeout(resizeTimer);
            resizeTimer = setTimeout(function () {
                renderAll();
            }, 100);
        });
    }

    // ───────────────────── 导入/导出状态 ─────────────────────
    function exportState() {
        const state = {
            bpm: _bpm,
            timeSigNum: _timeSigNum,
            timeSigDen: _timeSigDen,
            zoom: _zoom,
            loopStart: _loopStart,
            loopEnd: _loopEnd,
            isLooping: _isLooping,
            tracks: _tracks.map(function (t) {
                return {
                    id: t.id,
                    name: t.name,
                    color: t.color,
                    volume: t.volume,
                    pan: t.pan,
                    mute: t.mute,
                    solo: t.solo,
                    armed: t.armed,
                    clips: t.clips.map(function (c) {
                        return {
                            id: c.id,
                            startTime: c.startTime,
                            duration: c.duration,
                            offset: c.offset,
                            filePath: c.filePath,
                            name: c.name,
                        };
                    }),
                };
            }),
            pianoRollNotes: _pianoRollNotes,
        };
        return JSON.stringify(state);
    }

    function importState(json) {
        try {
            const state = typeof json === 'string' ? JSON.parse(json) : json;
            // 停止播放
            stop();

            // 清理旧轨道
            _tracks.forEach(function (t) {
                try { t.gainNode.disconnect(); } catch (e) { /* ok */ }
                try { t.panNode.disconnect(); } catch (e) { /* ok */ }
                try { t.analyser.disconnect(); } catch (e) { /* ok */ }
            });
            _tracks = [];
            _vuMeters = {};
            _trackIdCounter = 0;

            _bpm = state.bpm || DEFAULT_BPM;
            _timeSigNum = state.timeSigNum || 4;
            _timeSigDen = state.timeSigDen || 4;
            _zoom = state.zoom || 1;
            _loopStart = state.loopStart || 0;
            _loopEnd = state.loopEnd || 0;
            _isLooping = !!state.isLooping;
            _pianoRollNotes = state.pianoRollNotes || {};

            // 重建轨道
            const ctx = getAudioContext();
            const master = ensureMasterGain();

            (state.tracks || []).forEach(function (td) {
                const gainNode = ctx.createGain();
                gainNode.gain.value = td.volume || 0.8;
                const panNode = ctx.createStereoPanner();
                panNode.pan.value = td.pan || 0;
                const analyser = ctx.createAnalyser();
                analyser.fftSize = 256;
                analyser.smoothingTimeConstant = 0.8;

                gainNode.connect(panNode);
                panNode.connect(analyser);
                analyser.connect(master);

                const track = {
                    id: td.id || ('trk_' + (++_trackIdCounter)),
                    name: td.name || '轨道',
                    color: td.color || TRACK_COLORS[_tracks.length % TRACK_COLORS.length],
                    volume: td.volume || 0.8,
                    pan: td.pan || 0,
                    mute: !!td.mute,
                    solo: !!td.solo,
                    armed: !!td.armed,
                    gainNode: gainNode,
                    panNode: panNode,
                    analyser: analyser,
                    clips: (td.clips || []).map(function (c) {
                        return {
                            id: c.id || uid(),
                            startTime: c.startTime || 0,
                            duration: c.duration || 0,
                            offset: c.offset || 0,
                            filePath: c.filePath || '',
                            name: c.name || '音频剪辑',
                            waveformPeaks: null, // 需要重新加载音频
                        };
                    }),
                    audioBuffers: {},
                };
                _tracks.push(track);
                _vuMeters[track.id] = { analyser: analyser, peak: 0, rms: 0 };
                _trackIdCounter = Math.max(_trackIdCounter, parseInt(track.id.replace('trk_', '')) || 0);
            });

            // 更新传输控制UI
            const bpmInput = _els.transport ? _els.transport.querySelector('.te-bpm-input') : null;
            const tsNumInput = _els.transport ? _els.transport.querySelector('.te-timesig-num') : null;
            const tsDenInput = _els.transport ? _els.transport.querySelector('.te-timesig-den') : null;
            if (bpmInput) bpmInput.value = _bpm;
            if (tsNumInput) tsNumInput.value = _timeSigNum;
            if (tsDenInput) tsDenInput.value = _timeSigDen;

            renderAll();
            return true;
        } catch (e) {
            console.error('TrackEditor.importState error:', e);
            return false;
        }
    }

    // ───────────────────── 工具 ─────────────────────
    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    // ───────────────────── CSS样式注入 ─────────────────────
    function injectStyles() {
        if (document.getElementById('te-styles')) return;
        const style = document.createElement('style');
        style.id = 'te-styles';
        style.textContent = getStylesCSS();
        document.head.appendChild(style);
    }

    function getStylesCSS() {
        return `
/* TrackEditor - DAW风格音乐轨道编辑器样式 */
.te-container {
    display: flex;
    flex-direction: column;
    width: 100%;
    height: 100%;
    background: var(--bg-primary, #1a1510);
    color: var(--text-primary, #f5f0eb);
    font-family: var(--font-sans, -apple-system, BlinkMacSystemFont, sans-serif);
    overflow: hidden;
    user-select: none;
    -webkit-user-select: none;
}

/* 传输控制栏 */
.te-transport {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px 10px;
    background: var(--bg-secondary, #231e17);
    border-bottom: 1px solid var(--border, #3a3228);
    height: ${TRANSPORT_HEIGHT}px;
    flex-shrink: 0;
    flex-wrap: wrap;
}
.te-transport-left {
    display: flex;
    align-items: center;
    gap: 3px;
}
.te-transport-center {
    display: flex;
    align-items: center;
    gap: 4px;
    flex-shrink: 0;
}
.te-transport-right {
    display: flex;
    align-items: center;
    gap: 4px;
    margin-left: auto;
    flex-wrap: wrap;
}
.te-time-display {
    font-family: var(--font-mono, monospace);
    font-size: 13px;
    color: var(--accent, #e8853d);
    font-weight: 600;
    min-width: 90px;
}
.te-time-total {
    font-family: var(--font-mono, monospace);
    font-size: 11px;
    color: var(--text-muted, #7d7068);
}
.te-bpm-display {
    display: flex;
    align-items: center;
    gap: 3px;
    font-size: 11px;
    color: var(--text-secondary, #b5a898);
}
.te-bpm-display label {
    font-size: 10px;
    color: var(--text-muted, #7d7068);
}
.te-bpm-input {
    width: 48px;
    padding: 2px 4px;
    border: 1px solid var(--border, #3a3228);
    background: var(--bg-tertiary, #16120d);
    color: var(--text-primary, #f5f0eb);
    border-radius: 3px;
    font-size: 12px;
    font-family: var(--font-mono, monospace);
    text-align: center;
}
.te-timesig-display {
    display: flex;
    align-items: center;
    gap: 1px;
    font-size: 12px;
}
.te-timesig-display input {
    width: 28px;
    padding: 2px 3px;
    border: 1px solid var(--border, #3a3228);
    background: var(--bg-tertiary, #16120d);
    color: var(--text-primary, #f5f0eb);
    border-radius: 3px;
    font-size: 11px;
    font-family: var(--font-mono, monospace);
    text-align: center;
}

/* 按钮通用 */
.te-btn {
    width: 30px;
    height: 30px;
    border: 1px solid var(--border, #3a3228);
    background: var(--bg-surface, #2d2620);
    color: var(--text-primary, #f5f0eb);
    font-size: 14px;
    cursor: pointer;
    border-radius: 4px;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all 0.15s;
    flex-shrink: 0;
}
.te-btn:active {
    transform: scale(0.92);
    background: var(--bg-hover, #3a3228);
}
.te-btn-active {
    background: var(--accent, #e8853d) !important;
    color: var(--bg-primary, #1a1510) !important;
    border-color: var(--accent, #e8853d) !important;
}
.te-btn-record {
    color: #e85d5d;
}
.te-btn-record.te-btn-active {
    background: #e85d5d !important;
    color: #fff !important;
}

/* 主区域布局 */
.te-main-area {
    display: flex;
    flex: 1;
    overflow: hidden;
    min-height: 0;
}

/* 左侧轨道头列 */
.te-track-headers-col {
    width: ${TRACK_HEADER_WIDTH}px;
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    background: var(--bg-secondary, #231e17);
    border-right: 1px solid var(--border, #3a3228);
    overflow-y: auto;
}
.te-header-spacer {
    height: ${TIMELINE_HEIGHT}px;
    flex-shrink: 0;
    border-bottom: 1px solid var(--border, #3a3228);
}
.te-track-headers {
    flex: 1;
    overflow-y: auto;
}

/* 轨道头 */
.te-track-header {
    padding: 6px 8px;
    height: ${TRACK_LANE_HEIGHT}px;
    display: flex;
    flex-direction: column;
    gap: 3px;
    border-bottom: 1px solid var(--border, #3a3228);
    border-left: 3px solid var(--accent, #e8853d);
    box-sizing: border-box;
}
.te-track-header-top {
    display: flex;
    align-items: center;
    gap: 4px;
}
.te-track-color {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    flex-shrink: 0;
}
.te-track-name {
    flex: 1;
    font-size: 11px;
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    cursor: default;
}
.te-track-controls {
    display: flex;
    flex-direction: column;
    gap: 2px;
}
.te-track-vol-row, .te-track-pan-row {
    display: flex;
    align-items: center;
    gap: 4px;
}
.te-track-label {
    font-size: 9px;
    color: var(--text-muted, #7d7068);
    width: 24px;
    flex-shrink: 0;
}
.te-vol-slider, .te-pan-slider {
    flex: 1;
    height: 14px;
    -webkit-appearance: none;
    appearance: none;
    background: var(--bg-tertiary, #16120d);
    border-radius: 3px;
    outline: none;
}
.te-vol-slider::-webkit-slider-thumb,
.te-pan-slider::-webkit-slider-thumb {
    -webkit-appearance: none;
    width: 10px;
    height: 10px;
    background: var(--accent, #e8853d);
    border-radius: 50%;
    cursor: pointer;
}
.te-track-btns {
    display: flex;
    gap: 2px;
}
.te-track-btn {
    width: 22px;
    height: 18px;
    border: 1px solid var(--border, #3a3228);
    background: var(--bg-tertiary, #16120d);
    color: var(--text-secondary, #b5a898);
    font-size: 9px;
    cursor: pointer;
    border-radius: 2px;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    transition: all 0.1s;
    padding: 0;
}
.te-track-btn:active {
    transform: scale(0.9);
}
.te-track-btn.te-btn-active {
    background: var(--accent, #e8853d);
    color: #fff;
    border-color: var(--accent, #e8853d);
}
.te-track-btn.te-btn-mute.te-btn-active {
    background: var(--yellow, #e8c45d);
    color: var(--bg-primary, #1a1510);
    border-color: var(--yellow, #e8c45d);
}
.te-track-btn.te-btn-solo.te-btn-active {
    background: var(--green, #6bc96b);
    color: var(--bg-primary, #1a1510);
    border-color: var(--green, #6bc96b);
}
.te-track-btn.te-btn-arm.te-btn-active {
    background: #e85d5d;
    color: #fff;
    border-color: #e85d5d;
}
.te-track-remove {
    width: 16px;
    height: 16px;
    border: none;
    background: none;
    color: var(--text-muted, #7d7068);
    font-size: 11px;
    cursor: pointer;
    border-radius: 2px;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0;
    opacity: 0.5;
    transition: opacity 0.15s;
}
.te-track-remove:hover,
.te-track-remove:active {
    opacity: 1;
    color: var(--red, #e85d5d);
}

/* 右侧内容区 */
.te-content-col {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    min-width: 0;
}
.te-timeline-canvas {
    width: 100%;
    height: ${TIMELINE_HEIGHT}px;
    flex-shrink: 0;
    cursor: pointer;
}
.te-lanes-container {
    flex: 1;
    overflow: auto;
    position: relative;
}
.te-lanes-canvas {
    display: block;
}

/* 混音器面板 */
.te-mixer-panel {
    height: 240px;
    background: var(--bg-secondary, #231e17);
    border-top: 1px solid var(--border, #3a3228);
    overflow-x: auto;
    overflow-y: hidden;
    flex-shrink: 0;
}
.te-mixer-panel.te-hidden {
    display: none;
}
.te-mixer-channels {
    display: flex;
    align-items: stretch;
    height: 100%;
    padding: 4px;
    gap: 4px;
}
.te-mixer-channel {
    width: 70px;
    min-width: 70px;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 3px;
    padding: 4px 2px;
    background: var(--bg-tertiary, #16120d);
    border-radius: 4px;
    border: 1px solid var(--border, #3a3228);
}
.te-mixer-ch-name {
    font-size: 9px;
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 64px;
    text-align: center;
}
.te-mixer-vu {
    width: 16px;
    height: 80px;
    background: var(--bg-primary, #1a1510);
    border-radius: 2px;
    position: relative;
    overflow: hidden;
}
.te-mixer-vu-fill {
    position: absolute;
    bottom: 0;
    left: 0;
    right: 0;
    transition: height 0.05s;
    border-radius: 0 0 2px 2px;
}
.te-mixer-vu-peak {
    position: absolute;
    left: 0;
    right: 0;
    height: 2px;
    transition: bottom 0.05s;
}
.te-mixer-fader {
    writing-mode: vertical-lr;
    direction: rtl;
    width: 60px;
    height: 60px;
    -webkit-appearance: slider-vertical;
    appearance: slider-vertical;
    background: var(--bg-primary, #1a1510);
    border-radius: 3px;
    cursor: pointer;
}
.te-mixer-pan-row {
    width: 100%;
    padding: 0 2px;
}
.te-mixer-pan {
    width: 100%;
    height: 12px;
    -webkit-appearance: none;
    appearance: none;
    background: var(--bg-primary, #1a1510);
    border-radius: 2px;
}
.te-mixer-pan::-webkit-slider-thumb {
    -webkit-appearance: none;
    width: 8px;
    height: 8px;
    background: var(--accent, #e8853d);
    border-radius: 50%;
    cursor: pointer;
}
.te-mixer-btns {
    display: flex;
    gap: 2px;
}
.te-mixer-master {
    border-color: var(--accent, #e8853d);
    background: rgba(232, 133, 61, 0.1);
}

/* 钢琴卷帘面板 */
.te-piano-roll-panel {
    height: 300px;
    background: var(--bg-secondary, #231e17);
    border-top: 1px solid var(--border, #3a3228);
    overflow: auto;
    flex-shrink: 0;
}
.te-piano-roll-panel.te-hidden {
    display: none;
}
.te-piano-selector {
    padding: 4px 8px;
    border-bottom: 1px solid var(--border, #3a3228);
}
.te-piano-track-select {
    padding: 3px 8px;
    border: 1px solid var(--border, #3a3228);
    background: var(--bg-tertiary, #16120d);
    color: var(--text-primary, #f5f0eb);
    border-radius: 3px;
    font-size: 12px;
}
.te-piano-canvas {
    display: block;
    cursor: crosshair;
}
/* ─── 移动端响应式 ─── */
@media (max-width: 768px) {
    .te-container { font-size: 11px; }
    .te-transport { flex-wrap: wrap; gap: 4px; padding: 2px 4px; }
    .te-transport-left { order: 1; }
    .te-transport-center { order: 3; width: 100%; text-align: center; }
    .te-transport-right { order: 2; flex-wrap: wrap; gap: 2px; }
    .te-transport-right .te-btn { padding: 2px 4px; font-size: 10px; }
    .te-bpm-display, .te-timesig-display { display: none; }
    /* 移动端: 隐藏左侧轨道头列，改为在Canvas内渲染轨道名 */
    .te-track-headers-col { display: none !important; }
    /* 内容列占满宽度 */
    .te-content-col { width: 100% !important; }
    /* 时间线Canvas占满宽度 */
    .te-timeline-canvas { width: 100% !important; }
    /* 轨道区域占满宽度，垂直滚动查看分段 */
    .te-lanes-container { width: 100% !important; overflow-x: hidden !important; overflow-y: auto !important; -webkit-overflow-scrolling: touch; }
    .te-lanes-canvas { width: 100% !important; }
    .te-lyrics-panel, .te-piano-roll-panel, .te-mixer-panel { max-height: 40vh; }
    .te-main-area { min-height: 200px; }
}
@media (max-width: 480px) {
    .te-transport-left .te-btn { padding: 2px 3px; font-size: 9px; }
    .te-main-area { min-height: 160px; }
}
`;
    }

    // ───────────────────── 公共API ─────────────────────
    function init() {
        _container = document.getElementById('track-editor-container');
        if (!_container) {
            console.error('TrackEditor: 找不到 #track-editor-container');
            return;
        }
        injectStyles();
        getAudioContext(); // 在用户交互上下文中预创建
        ensureMasterGain();
        buildUI();

        // 创建默认轨道
        if (_tracks.length === 0) {
            createTrack('主旋律');
            createTrack('伴奏');
            createTrack('鼓组');
            createTrack('贝斯');
        }

        renderAll();
        _initialized = true;

        // 移动端: 确保AudioContext在首次触摸时恢复
        document.addEventListener('touchstart', function resumeAudio() {
            if (_audioCtx && _audioCtx.state === 'suspended') {
                _audioCtx.resume();
            }
            document.removeEventListener('touchstart', resumeAudio);
        }, { once: true });
    }

    function addTrack(name) {
        if (_tracks.length >= MAX_TRACKS) return null;
        const track = createTrack(name);
        if (track) renderAll();
        return track ? track.id : null;
    }

    function removeTrack(id) {
        removeTrackById(id);
        renderAll();
    }

    function getTracks() {
        return _tracks.map(function (t) {
            return {
                id: t.id,
                name: t.name,
                color: t.color,
                volume: t.volume,
                pan: t.pan,
                mute: t.mute,
                solo: t.solo,
                armed: t.armed,
                clips: t.clips.map(function (c) {
                    return {
                        id: c.id,
                        startTime: c.startTime,
                        duration: c.duration,
                        offset: c.offset,
                        filePath: c.filePath,
                        name: c.name,
                    };
                }),
            };
        });
    }

    function loadAudioFile(trackId, file, startTime) {
        return loadAudioFileToTrack(trackId, file, startTime);
    }

    function resize() {
        renderAll();
    }

    // ───────────────────── 歌词面板 ─────────────────────
    function renderLyricsPanel() {
        const panel = _els.lyricsPanel;
        if (!panel) return;
        const trackId = _tracks.length > 0 ? _tracks[0].id : null;
        const lyrics = trackId ? (_lyrics[trackId] || []) : [];
        let html = '<div style="padding:8px;font-size:12px;color:var(--text-primary);">';
        html += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">';
        html += '<span style="font-weight:600;">📝 歌词编辑</span>';
        if (_tracks.length > 0) {
            html += '<select id="te-lyrics-track-select" style="flex:1;padding:2px 6px;border-radius:4px;background:var(--bg-tertiary);color:var(--text-primary);border:1px solid var(--border);font-size:11px;">';
            _tracks.forEach(function(t) {
                html += '<option value="' + t.id + '"' + (t.id === trackId ? ' selected' : '') + '>' + t.name + '</option>';
            });
            html += '</select>';
        }
        html += '</div>';
        html += '<textarea id="te-lyrics-editor" style="width:100%;height:300px;background:var(--bg-primary);color:var(--text-primary);border:1px solid var(--border);border-radius:4px;padding:8px;font-size:12px;font-family:var(--font-mono);resize:vertical;" placeholder="每行格式: [MM:SS] 歌词文本&#10;例: [00:05] 第一句歌词&#10;[00:10] 第二句歌词">';
        lyrics.forEach(function(line) {
            const m = Math.floor(line.time / 60);
            const s = Math.floor(line.time % 60);
            html += '[' + String(m).padStart(2,'0') + ':' + String(s).padStart(2,'0') + '] ' + line.text + '\n';
        });
        html += '</textarea>';
        html += '<div style="display:flex;gap:6px;margin-top:6px;">';
        html += '<button id="te-lyrics-apply" style="flex:1;padding:4px;border:none;background:var(--accent);color:#fff;border-radius:4px;font-size:11px;cursor:pointer;">应用歌词</button>';
        html += '<button id="te-lyrics-clear" style="padding:4px 8px;border:1px solid var(--border);background:var(--bg-surface);color:var(--text-secondary);border-radius:4px;font-size:11px;cursor:pointer;">清空</button>';
        html += '</div></div>';
        panel.innerHTML = html;
        // Bind events
        const applyBtn = document.getElementById('te-lyrics-apply');
        const clearBtn = document.getElementById('te-lyrics-clear');
        const trackSelect = document.getElementById('te-lyrics-track-select');
        const editor = document.getElementById('te-lyrics-editor');
        if (applyBtn) applyBtn.addEventListener('click', function() {
            const tid = trackSelect ? trackSelect.value : trackId;
            if (!tid) return;
            const text = editor ? editor.value : '';
            const lines = text.split('\n');
            const parsed = [];
            lines.forEach(function(line) {
                const match = line.match(/\[(\d{2}):(\d{2})\]\s*(.+)/);
                if (match) {
                    parsed.push({ time: parseInt(match[1]) * 60 + parseInt(match[2]), text: match[3].trim() });
                }
            });
            parsed.sort(function(a, b) { return a.time - b.time; });
            _lyrics[tid] = parsed;
        });
        if (clearBtn) clearBtn.addEventListener('click', function() {
            if (editor) editor.value = '';
            const tid = trackSelect ? trackSelect.value : trackId;
            if (tid) _lyrics[tid] = [];
        });
        if (trackSelect) trackSelect.addEventListener('change', function() {
            const tid = this.value;
            const l = _lyrics[tid] || [];
            if (editor) {
                editor.value = '';
                l.forEach(function(line) {
                    const m = Math.floor(line.time / 60);
                    const s = Math.floor(line.time % 60);
                    editor.value += '[' + String(m).padStart(2,'0') + ':' + String(s).padStart(2,'0') + '] ' + line.text + '\n';
                });
            }
        });
    }

    // ───────────────────── 歌声合成 ─────────────────────
    function midiToFreq(midi) { return 440 * Math.pow(2, (midi - 69) / 12); }

    function synthesizeVocals(trackId, notes) {
        const track = getTrackById(trackId);
        if (!track) return 0;
        const ctx = getAudioContext();
        const master = ensureMasterGain();
        let count = 0;
        notes.forEach(function(note) {
            if (note.pitch == null || note.start_time == null || note.duration == null) return;
            const freq = midiToFreq(note.pitch + (track.timbre.pitchShift || 0));
            const swingOffset = _swing > 0 && secondsToBeats(note.start_time, _bpm) % 1 >= 0.5
                ? _swing * (60 / _bpm / 2) : 0;
            const humanOffset = _humanize > 0 ? (Math.random() - 0.5) * _humanize * 2 : 0;
            const startTime = Math.max(0, note.start_time + swingOffset + humanOffset);
            const timbre = track.timbre;
            // Oscillator
            const osc = ctx.createOscillator();
            osc.type = timbre.waveform || 'sine';
            osc.frequency.value = freq;
            // ADSR envelope
            const env = ctx.createGain();
            const attack = timbre.attack || 0.05;
            const decay = timbre.decay || 0.1;
            const sustain = timbre.sustain || 0.7;
            const release = timbre.release || 0.3;
            const dur = note.duration;
            env.gain.setValueAtTime(0, ctx.currentTime + startTime);
            env.gain.linearRampToValueAtTime(0.5, ctx.currentTime + startTime + attack);
            env.gain.linearRampToValueAtTime(0.5 * sustain, ctx.currentTime + startTime + attack + decay);
            env.gain.setValueAtTime(0.5 * sustain, ctx.currentTime + startTime + dur - release);
            env.gain.linearRampToValueAtTime(0, ctx.currentTime + startTime + dur);
            // Filter
            const filter = ctx.createBiquadFilter();
            filter.type = 'lowpass';
            filter.frequency.value = timbre.filterFreq || 2000;
            filter.Q.value = timbre.filterQ || 1;
            // Connect: osc → filter → env → track gain
            osc.connect(filter);
            filter.connect(env);
            env.connect(track.gainNode);
            osc.start(ctx.currentTime + startTime);
            osc.stop(ctx.currentTime + startTime + dur + 0.01);
            count++;
        });
        return count;
    }

    function playSynthNote(trackId, frequency, duration, startTime) {
        const track = getTrackById(trackId);
        if (!track) return;
        const ctx = getAudioContext();
        const timbre = track.timbre;
        const osc = ctx.createOscillator();
        osc.type = timbre.waveform || 'sine';
        osc.frequency.value = frequency;
        const env = ctx.createGain();
        env.gain.setValueAtTime(0, ctx.currentTime + startTime);
        env.gain.linearRampToValueAtTime(0.5, ctx.currentTime + startTime + (timbre.attack || 0.05));
        env.gain.linearRampToValueAtTime(0.35, ctx.currentTime + startTime + (timbre.attack || 0.05) + (timbre.decay || 0.1));
        env.gain.linearRampToValueAtTime(0, ctx.currentTime + startTime + duration);
        const filter = ctx.createBiquadFilter();
        filter.type = 'lowpass';
        filter.frequency.value = timbre.filterFreq || 2000;
        filter.Q.value = timbre.filterQ || 1;
        osc.connect(filter);
        filter.connect(env);
        env.connect(track.gainNode);
        osc.start(ctx.currentTime + startTime);
        osc.stop(ctx.currentTime + startTime + duration + 0.01);
    }

    // ───────────────────── 音色控制 ─────────────────────
    function setTrackTimbre(trackId, params) {
        const track = getTrackById(trackId);
        if (!track) return;
        if (params.waveform) track.timbre.waveform = params.waveform;
        if (params.attack !== undefined) track.timbre.attack = clamp(params.attack, 0, 2);
        if (params.decay !== undefined) track.timbre.decay = clamp(params.decay, 0, 2);
        if (params.sustain !== undefined) track.timbre.sustain = clamp(params.sustain, 0, 1);
        if (params.release !== undefined) track.timbre.release = clamp(params.release, 0, 2);
        if (params.filterFreq !== undefined) track.timbre.filterFreq = clamp(params.filterFreq, 20, 20000);
        if (params.filterQ !== undefined) track.timbre.filterQ = clamp(params.filterQ, 0.1, 30);
        if (params.pitchShift !== undefined) track.timbre.pitchShift = clamp(params.pitchShift, -24, 24);
        if (params.swing !== undefined) track.timbre.swing = clamp(params.swing, 0, 1);
        if (params.humanize !== undefined) track.timbre.humanize = clamp(params.humanize, 0, 0.1);
    }

    function getTrackTimbre(trackId) {
        const track = getTrackById(trackId);
        if (!track) return Object.assign({}, _defaultTimbre);
        return Object.assign({}, track.timbre);
    }

    // ───────────────────── 节拍/律动控制 ─────────────────────
    function setSwing(amount) {
        _swing = clamp(amount, 0, 1);
    }

    function setHumanize(amount) {
        _humanize = clamp(amount, 0, 0.1);
    }

    function quantizeNotes(trackId, gridValue) {
        const gridMap = { '1/4': 1, '1/8': 0.5, '1/16': 0.25, '1/32': 0.125 };
        const gridBeats = gridMap[gridValue];
        if (!gridBeats) return;
        const notes = _pianoRollNotes[trackId];
        if (!notes) return;
        const beatDur = 60 / _bpm;
        const gridSec = gridBeats * beatDur;
        notes.forEach(function(note) {
            note.start = Math.round(note.start / gridSec) * gridSec;
        });
        if (_pianoRollVisible) renderPianoRoll();
    }

    function getGrooveSettings() {
        return { swing: _swing, humanize: _humanize, quantizeValue: _quantizeValue };
    }

    // ───────────────────── 返回公共接口 ─────────────────────
    return {
        init: init,
        addTrack: addTrack,
        removeTrack: removeTrack,
        getTracks: getTracks,
        play: play,
        pause: pause,
        stop: stop,
        record: record,
        setBPM: setBPM,
        setTimeSignature: setTimeSignature,
        loadAudioFile: loadAudioFile,
        seek: seek,
        getCurrentTime: getCurrentTime,
        getDuration: getDuration,
        setTrackVolume: setTrackVolume,
        setTrackPan: setTrackPan,
        setTrackMute: setTrackMute,
        setTrackSolo: setTrackSolo,
        exportState: exportState,
        importState: importState,
        resize: resize,
        // 歌词
        getLyrics: function(trackId) { return _lyrics[trackId || ''] || []; },
        setLyrics: function(trackId, lyrics) { _lyrics[trackId] = lyrics; if (_lyricsVisible) renderLyricsPanel(); },
        // 歌声合成
        synthesizeVocals: synthesizeVocals,
        playSynthNote: playSynthNote,
        // 音色
        setTrackTimbre: setTrackTimbre,
        getTrackTimbre: getTrackTimbre,
        // 节拍/律动
        setSwing: setSwing,
        setHumanize: setHumanize,
        quantizeNotes: quantizeNotes,
        getGrooveSettings: getGrooveSettings,
        // BPM获取
        getBPM: function() { return _bpm; },
        // 录音
        recordAudio: function() {
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                if (window.showToast) window.showToast('浏览器不支持录音', 'error');
                return;
            }
            record();
        },
    };
})();
