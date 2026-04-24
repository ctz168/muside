/**
 * TerminalManager - Code execution and output display for MusIDE
 * Works with Flask backend on port 1239
 */
const TerminalManager = (() => {
    'use strict';

    // ── State ──────────────────────────────────────────────────────
    let currentProcId = null;
    let isRunning = false;
    let eventSource = null;         // SSE connection
    let pollTimer = null;           // polling fallback timer
    let pollSince = 0;              // last line index seen

    // SessionStorage key for persisting running process ID across page refreshes
    const PROC_ID_STORAGE_KEY = 'muside_running_proc_id';
    let compilers = [];             // cached compiler list
    let panelHeight = Math.floor(window.innerHeight * 0.85);  // default 85% of screen height
    let isDragging = false;         // resize drag state
    let currentCmdBlock = null;     // current command block container
    let dragStartY = 0;            // touch/mouse Y at drag start
    let dragStartHeight = 0;        // panel height at drag start
    let onProcessComplete = null;   // callback after streamed process finishes
    let _userScrolling = false;     // user is manually scrolling output
    let _autoScrollPaused = false;  // auto-scroll is paused due to user scroll
    let _scrollPauseTimer = null;   // timer to resume auto-scroll after user stops
    let _pendingScroll = false;     // debounce flag for scroll-to-bottom
    let _appendBatchTimer = null;   // debounce timer for batch DOM updates
    let _pendingAppends = [];       // batched append operations
    let _trimScheduled = false;     // flag to avoid redundant trim calls

    // ── Constants ──────────────────────────────────────────────────
    const MIN_PANEL_HEIGHT = 120;
    const MAX_PANEL_HEIGHT = window.innerHeight ? Math.floor(window.innerHeight * 0.9) : 600;
    const POLL_INTERVAL = 500;      // ms between poll requests
    const MAX_OUTPUT_LINES = 5000;  // max lines before trimming

    // ── Platform Detection ────────────────────────────────────────
    let platformInfo = {
        is_windows: false,
        is_termux: false,
        shell_prompt: '$',
        default_shell: 'bash',
        default_compiler: 'python3',
    };

    async function loadPlatformInfo() {
        try {
            const resp = await fetch('/api/platform/info');
            if (resp.ok) {
                const data = await resp.json();
                Object.assign(platformInfo, data);
                // Update shell prompt in UI
                const promptEl = document.getElementById('shell-prompt');
                if (promptEl) promptEl.textContent = data.shell_prompt || '$';
            }
        } catch (e) {
            console.warn('[TerminalManager] Failed to load platform info:', e);
        }
    }

    // ── Helpers ────────────────────────────────────────────────────

    /**
     * Escape HTML entities for safe insertion into output
     */
    function escapeHTML(str) {
        const div = document.createElement('div');
        div.textContent = str || '';
        return div.innerHTML;
    }

    /**
     * Get current timestamp string for log lines
     */
    function timestamp() {
        const now = new Date();
        return now.toLocaleTimeString();
    }

    /**
     * Trim old output lines if we exceed the maximum
     */
    function trimOutput() {
        const container = document.getElementById('output-content');
        if (!container) return;

        const lines = container.querySelectorAll('.output-line');
        if (lines.length > MAX_OUTPUT_LINES) {
            const removeCount = lines.length - MAX_OUTPUT_LINES;
            // Batch remove: collect into a DocumentFragment then clear
            const parent = lines[0].parentNode;
            if (parent) {
                // Use a range for efficient batch removal
                const range = document.createRange();
                range.setStartBefore(lines[0]);
                range.setEndBefore(lines[removeCount]);
                range.deleteContents();
            }
        }
    }

    /**
     * Smart scroll-to-bottom: only auto-scroll if the user hasn't scrolled up.
     * On mobile, respects touch gestures and doesn't fight pull-to-refresh.
     */
    function smartScrollToBottom() {
        if (_pendingScroll) return;
        _pendingScroll = true;
        requestAnimationFrame(() => {
            _pendingScroll = false;
            if (_autoScrollPaused) return;
            const container = document.getElementById('output-content');
            if (!container) return;
            container.scrollTop = container.scrollHeight;
        });
    }

    /**
     * Detect user scroll on the output container.
     * If the user scrolls up, pause auto-scroll.
     * If the user scrolls to the bottom, resume auto-scroll.
     */
    function initScrollDetection() {
        const container = document.getElementById('output-content');
        if (!container) return;

        // Detect when user scrolls up (away from bottom)
        container.addEventListener('scroll', () => {
            const threshold = 50; // pixels from bottom
            const atBottom = (container.scrollHeight - container.scrollTop - container.clientHeight) < threshold;

            if (atBottom) {
                // User scrolled back to bottom — resume auto-scroll
                _autoScrollPaused = false;
                clearTimeout(_scrollPauseTimer);
            } else {
                // User scrolled up — pause auto-scroll
                _autoScrollPaused = true;
                clearTimeout(_scrollPauseTimer);
                // Resume auto-scroll after 5 seconds of inactivity
                _scrollPauseTimer = setTimeout(() => {
                    _autoScrollPaused = false;
                    smartScrollToBottom();
                }, 5000);
            }
        }, { passive: true });

        // On touchstart, mark as user scrolling to prevent auto-scroll
        container.addEventListener('touchstart', () => {
            _userScrolling = true;
            // Pause auto-scroll immediately on touch
            _autoScrollPaused = true;
            clearTimeout(_scrollPauseTimer);
        }, { passive: true });

        // On touchend, check if user scrolled to bottom
        container.addEventListener('touchend', () => {
            _userScrolling = false;
            // If already at bottom, resume auto-scroll immediately
            const threshold = 50;
            const atBottom = (container.scrollHeight - container.scrollTop - container.clientHeight) < threshold;
            if (atBottom) {
                _autoScrollPaused = false;
            } else {
                // Resume after a delay
                clearTimeout(_scrollPauseTimer);
                _scrollPauseTimer = setTimeout(() => {
                    _autoScrollPaused = false;
                }, 3000);
            }
        }, { passive: true });
    }

    // ── API: Compilers ─────────────────────────────────────────────

    /**
     * Load available compilers from the backend and populate the select dropdown
     * @returns {Promise<Array>} list of compiler objects
     */
    async function loadCompilers() {
        try {
            const resp = await fetch('/api/compilers');
            if (!resp.ok) throw new Error(`Failed to load compilers: ${resp.statusText}`);
            const data = await resp.json();

            compilers = Array.isArray(data) ? data : (data.compilers || []);

            // Populate the compiler select
            const select = document.getElementById('compiler-select');
            if (select) {
                let html = '';
                for (const compiler of compilers) {
                    const name = compiler.name || compiler.label || compiler.id || compiler;
                    const value = compiler.id || compiler.value || compiler.name || compiler;
                    const selected = compiler.default ? ' selected' : '';
                    html += `<option value="${escapeHTML(value)}"${selected}>${escapeHTML(name)}</option>`;
                }
                // If no compilers loaded, add a default option
                if (compilers.length === 0) {
                    html = '<option value="auto">Auto-detect</option>';
                }
                select.innerHTML = html;
            }

            return compilers;
        } catch (err) {
            showToast(`Failed to load compilers: ${err.message}`, 'error');
            return [];
        }
    }

    /**
     * Get the currently selected compiler
     * @returns {string} compiler identifier
     */
    function getSelectedCompiler() {
        const select = document.getElementById('compiler-select');
        return select ? select.value : 'auto';
    }

    // ── API: Execute ───────────────────────────────────────────────

    /**
     * Execute a file with the given compiler and arguments
     * @param {string} file_path - path to the file to execute
     * @param {string} [compiler] - compiler to use (defaults to selected)
     * @param {string} [args] - command-line arguments
     * @returns {Promise<object>} execution result
     */
    async function execute(file_path, compiler, args) {
        // Auto-stop existing process before starting a new one
        if (isRunning) {
            appendOutput('[system] Stopping existing process before re-run...', 'system');
            await stop();
            // Brief pause to let the OS free the port
            await new Promise(r => setTimeout(r, 500));
        }

        if (!file_path) {
            showToast('No file specified for execution', 'warning');
            return { error: 'No file specified' };
        }

        compiler = compiler || getSelectedCompiler();

        // Ensure panel is visible
        showPanel();

        try {
            const prompt = platformInfo.shell_prompt || '$';
            const cmdDisplay = `${compiler} ${file_path}${args ? ' ' + args : ''}`;
            startCmdBlock(cmdDisplay);
            appendOutput(`─────────────────────────────────────────`, 'status');
            appendOutput(`${prompt} ${cmdDisplay}`, 'system');
            appendOutput(`[info] PID: pending... | Time: ${new Date().toLocaleString()}`, 'info');

            const body = { file_path, compiler };
            if (args) body.args = args;

            const resp = await fetch('/api/run/execute', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
            });

            if (!resp.ok) throw new Error(`Execution failed: ${resp.statusText}`);

            const data = await resp.json();

            // Show auto-killed port info
            if (data.detected_ports && data.detected_ports.length > 0) {
                appendOutput(`[auto] 检测到端口: ${data.detected_ports.join(', ')}`, 'info');
            }
            if (data.killed_ports && data.killed_ports.length > 0) {
                const killInfo = data.killed_ports.map(k => `端口 ${k.port} (PID: ${k.pid || k.managed_proc})`).join(', ');
                appendOutput(`[auto] 已自动释放占用端口: ${killInfo}`, 'warn');
            }

            // Warn if no venv is configured for Python
            if (data.no_venv && (compiler === 'python3' || compiler === 'python')) {
                appendOutput(`[warn] 未检测到虚拟环境。建议在调试面板创建虚拟环境以确保依赖隔离。`, 'warn');
                if (data.cwd) {
                    appendOutput(`[info] CWD: ${data.cwd}`, 'info');
                }
            }

            currentProcId = data.proc_id || data.process_id || data.id || null;
            pollSince = 0;

            if (currentProcId) {
                persistProcId(currentProcId);
                appendOutput(`[info] PID: ${currentProcId} | Streaming output...`, 'info');
                setRunningState(true);
                streamOutput(currentProcId);
            } else {
                // No process ID — output may be included directly
                if (data.output) {
                    appendOutput(data.output, 'stdout');
                }
                if (data.stderr) {
                    appendOutput(data.stderr, 'stderr');
                }
                if (data.error) {
                    appendOutput(data.error, 'error');
                }
                if (data.exit_code !== undefined) {
                    const code = data.exit_code;
                    const type = code === 0 ? 'success' : 'error';
                    appendOutput(`[exit] Code: ${code} (${type === 'success' ? 'OK' : 'FAIL'})`, type);
                }
            }

            return data;
        } catch (err) {
            appendOutput(`[error] Execution failed: ${err.message}`, 'error');
            appendOutput(`[info] Check network connection and try again.`, 'info');
            showToast(`Execution error: ${err.message}`, 'error');
            return { error: err.message };
        }
    }

    /**
     * Execute code directly (without saving to a file first)
     * @param {string} code - source code to execute
     * @param {string} [compiler] - compiler/language to use
     * @returns {Promise<object>} execution result
     */
    async function executeCode(code, compiler) {
        // Auto-stop existing process before starting a new one
        if (isRunning) {
            appendOutput('[system] Stopping existing process before re-run...', 'system');
            await stop();
            await new Promise(r => setTimeout(r, 500));
        }

        if (!code || !code.trim()) {
            showToast('No code to execute', 'warning');
            return { error: 'No code provided' };
        }

        compiler = compiler || getSelectedCompiler();

        showPanel();

        try {
            const displayCode = code.length > 120 ? code.substring(0, 120) + '...' : code;
            appendOutput(`$ [${compiler}] ${displayCode}`, 'system');
            appendOutput(`[info] Shell exec | Time: ${new Date().toLocaleString()}`, 'info');

            const body = { code, compiler };

            const resp = await fetch('/api/run/execute', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
            });

            if (!resp.ok) throw new Error(`Execution failed: ${resp.statusText}`);

            const data = await resp.json();

            // Show auto-killed port info
            if (data.detected_ports && data.detected_ports.length > 0) {
                appendOutput(`[auto] 检测到端口: ${data.detected_ports.join(', ')}`, 'info');
            }
            if (data.killed_ports && data.killed_ports.length > 0) {
                const killInfo = data.killed_ports.map(k => `端口 ${k.port} (PID: ${k.pid || k.managed_proc})`).join(', ');
                appendOutput(`[auto] 已自动释放占用端口: ${killInfo}`, 'warn');
            }

            currentProcId = data.proc_id || data.process_id || data.id || null;
            pollSince = 0;

            if (currentProcId) {
                persistProcId(currentProcId);
                appendOutput(`[info] PID: ${currentProcId} | Streaming output...`, 'info');
                setRunningState(true);
                streamOutput(currentProcId);
            } else {
                if (data.output) appendOutput(data.output, 'stdout');
                if (data.stderr) appendOutput(data.stderr, 'stderr');
                if (data.error) appendOutput(data.error, 'error');
                if (data.exit_code !== undefined) {
                    const code2 = data.exit_code;
                    const type = code2 === 0 ? 'success' : 'error';
                    appendOutput(`[exit] Code: ${code2} (${type === 'success' ? 'OK' : 'FAIL'})`, type);
                }
                // Auto-focus back to input after non-streaming execution
                const si = document.getElementById('shell-input');
                if (si) si.focus();
            }

            return data;
        } catch (err) {
            appendOutput(`[error] Execution failed: ${err.message}`, 'error');
            appendOutput(`[info] Check network connection and try again.`, 'info');
            showToast(`Execution error: ${err.message}`, 'error');
            return { error: err.message };
        }
    }

    // ── API: Shell Command (raw command execution) ─────────────

    /**
     * Execute a raw shell command (e.g. 'dir', 'ls', 'pip install').
     * Unlike executeCode(), this does NOT write to a temp file —
     * it sends the command directly to /api/run/shell for execution.
     * @param {string} command - the shell command to execute
     * @returns {Promise<object>} execution result
     */
    async function executeShellCommand(command) {
        // Auto-stop existing process before starting a new one
        if (isRunning) {
            appendOutput('[system] Stopping existing process before re-run...', 'system');
            await stop();
            await new Promise(r => setTimeout(r, 500));
        }

        if (!command || !command.trim()) {
            showToast('No command to execute', 'warning');
            return { error: 'No command provided' };
        }

        showPanel();

        try {
            const body = { command };

            const resp = await fetch('/api/run/shell', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
            });

            if (!resp.ok) {
                const errData = await resp.json().catch(() => ({}));
                throw new Error(errData.error || `Execution failed: ${resp.statusText}`);
            }

            const data = await resp.json();

            currentProcId = data.proc_id || data.process_id || data.id || null;
            pollSince = 0;

            if (currentProcId) {
                persistProcId(currentProcId);
                appendOutput(`[info] PID: ${currentProcId} | Streaming output...`, 'info');
                setRunningState(true);
                streamOutput(currentProcId);
                // Re-focus input after streaming starts (mobile WebView may lose focus)
                const si = document.getElementById('shell-input');
                if (si) setTimeout(() => { si.focus(); }, 100);
            } else {
                if (data.output) appendOutput(data.output, 'stdout');
                if (data.stderr) appendOutput(data.stderr, 'stderr');
                if (data.error) appendOutput(data.error, 'error');
                if (data.exit_code !== undefined) {
                    const code = data.exit_code;
                    const type = code === 0 ? 'success' : 'error';
                    appendOutput(`[exit] Code: ${code} (${type === 'success' ? 'OK' : 'FAIL'})`, type);
                }
                const si = document.getElementById('shell-input');
                if (si) si.focus();
            }

            return data;
        } catch (err) {
            appendOutput(`[error] Shell command failed: ${err.message}`, 'error');
            appendOutput(`[info] Check network connection and try again.`, 'info');
            showToast(`Shell error: ${err.message}`, 'error');
            return { error: err.message };
        }
    }

    // ── API: Stop ──────────────────────────────────────────────────

    /**
     * Stop the currently running process
     * @param {string} [procId] - process ID (defaults to current)
     * @returns {Promise<object>} stop result
     */
    async function stop(procId) {
        procId = procId || currentProcId;

        if (!procId) {
            showToast('No process running', 'warning');
            return { error: 'No process to stop' };
        }

        try {
            appendOutput(`[system] Sending SIGTERM to process ${procId}...`, 'system');

            const resp = await fetch('/api/run/stop', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ proc_id: procId })
            });

            if (!resp.ok) throw new Error(`Stop failed: ${resp.statusText}`);

            const data = await resp.json();
            appendOutput(`[success] Process ${procId} stopped.`, 'success');
            appendOutput(`─────────────────────────────────────────`, 'status');
            cleanupProcess();
            showToast('Process stopped', 'info');

            return data;
        } catch (err) {
            appendOutput(`Stop error: ${err.message}`, 'error');
            showToast(`Stop error: ${err.message}`, 'error');
            return { error: err.message };
        }
    }

    // ── Output Streaming ───────────────────────────────────────────

    /**
     * Stream output using Server-Sent Events (SSE)
     * Falls back to polling if SSE is not available or fails
     * @param {string} procId - process ID to stream output for
     */
    function streamOutput(procId) {
        // Close any existing SSE connection
        closeEventSource();
        stopPolling();

        try {
            const url = `/api/run/output/stream?proc_id=${encodeURIComponent(procId)}`;
            eventSource = new EventSource(url);

            eventSource.onopen = () => {
                // SSE connection established
            };

            function parseSSEData(raw) {
                try { return JSON.parse(raw); } catch { return null; }
            }

            eventSource.addEventListener('stdout', (e) => {
                const d = parseSSEData(e.data);
                const text = d ? d.text : e.data;
                appendOutput(text, 'stdout');
                // Parse pip output for progress tracking
                if (text) _parsePipLine(text);
            });

            eventSource.addEventListener('stderr', (e) => {
                const d = parseSSEData(e.data);
                appendOutput(d ? d.text : e.data, 'stderr');
            });

            eventSource.addEventListener('error', (e) => {
                const d = parseSSEData(e.data);
                if (d) {
                    appendOutput(d.text || d.message || JSON.stringify(d), 'error');
                } else if (e.data) {
                    appendOutput(e.data, 'error');
                }
            });

            eventSource.addEventListener('status', (e) => {
                const d = parseSSEData(e.data);
                if (d) {
                    appendOutput(d.text || JSON.stringify(d), 'status');
                } else {
                    appendOutput(e.data, 'status');
                }
            });

            eventSource.addEventListener('exit', (e) => {
                const d = parseSSEData(e.data);
                const exitCode = d ? d.exit_code : parseInt(e.data, 10);
                const code = typeof exitCode === 'number' ? exitCode : 0;
                const type = code === 0 ? 'success' : 'error';
                const statusText = code === 0 ? 'completed successfully' : 'failed';
                appendOutput(`─────────────────────────────────────────`, 'status');
                appendOutput(`[exit] Process ${statusText} (code: ${code})`, type);
                cleanupProcess();
            });

            eventSource.addEventListener('done', (e) => {
                const d = parseSSEData(e.data);
                appendOutput(d ? (d.text || 'Done.') : (e.data || 'Done.'), 'success');
                appendOutput(`─────────────────────────────────────────`, 'status');
                cleanupProcess();
            });

            eventSource.onerror = () => {
                // SSE failed — fall back to polling
                closeEventSource();
                if (isRunning && currentProcId) {
                    startPolling(currentProcId);
                }
            };

        } catch (err) {
            // EventSource not supported or failed to connect
            closeEventSource();
            if (isRunning && currentProcId) {
                startPolling(currentProcId);
            }
        }
    }

    /**
     * Close the SSE EventSource connection
     */
    function closeEventSource() {
        if (eventSource) {
            eventSource.close();
            eventSource = null;
        }
    }

    // ── Output Polling (Fallback) ──────────────────────────────────

    /**
     * Consecutive 'not-running' count from polling.
     * Only after RECOVER_NOT_RUNNING_THRESHOLD consecutive false readings
     * do we consider the process truly stopped. This prevents premature cleanup
     * when the page is backgrounded and SSE drops or fetches are throttled.
     */
    let _consecutiveNotRunning = 0;
    const RECOVER_NOT_RUNNING_THRESHOLD = 3;

    /**
     * Start polling for output (fallback when SSE is unavailable)
     * @param {string} procId - process ID to poll
     */
    function startPolling(procId) {
        stopPolling();
        _consecutiveNotRunning = 0;
        pollOutput(procId);
    }

    /**
     * Poll the output endpoint for new lines
     * @param {string} procId - process ID
     */
    async function pollOutput(procId) {
        if (!procId || !isRunning) return;

        try {
            const url = `/api/run/output?proc_id=${encodeURIComponent(procId)}&since=${pollSince}`;
            const resp = await fetch(url);

            if (!resp.ok) {
                // Don't assume process is dead on a single HTTP error —
                // it might be a transient network issue (e.g. throttled in background tab)
                console.warn('Output poll HTTP error:', resp.status);
                if (isRunning && currentProcId) {
                    pollTimer = setTimeout(() => pollOutput(procId), POLL_INTERVAL * 2);
                }
                return;
            }

            const data = await resp.json();

            // Process output lines
            const lines = data.outputs || data.lines || data.output || [];
            if (Array.isArray(lines)) {
                for (const line of lines) {
                    const text = typeof line === 'string' ? line : (line.text || line.content || '');
                    const rawType = typeof line === 'object' ? (line.type || line.stream || 'stdout') : 'stdout';
                    // Map server types to display types with better colors
                    const type = rawType === 'error' ? 'error' :
                                rawType === 'status' ? 'system' :
                                rawType === 'stderr' ? 'stderr' : 'stdout';
                    appendOutput(text, type);
                    // Parse pip output for progress tracking
                    if (type === 'stdout' && text) _parsePipLine(text);
                }
                pollSince = data.since !== undefined ? data.since : pollSince + lines.length;
            } else if (typeof lines === 'string' && lines.trim()) {
                appendOutput(lines, 'stdout');
                pollSince++;
            }

            // Check if process has exited — use consecutive-failure tolerance
            if (data.exited || data.done || data.finished || !data.running) {
                _consecutiveNotRunning++;
                if (_consecutiveNotRunning >= RECOVER_NOT_RUNNING_THRESHOLD) {
                    const exitCode = data.exit_code !== undefined ? data.exit_code : 0;
                    const type = parseInt(exitCode, 10) === 0 ? 'info' : 'error';
                    appendOutput(`Process exited with code ${exitCode}`, type);
                    cleanupProcess();
                    _consecutiveNotRunning = 0;
                    return;
                }
                // Not yet confirmed — keep polling to double-check
                if (isRunning && currentProcId) {
                    pollTimer = setTimeout(() => pollOutput(procId), POLL_INTERVAL);
                }
                return;
            }

            // Process is still running — reset counter
            _consecutiveNotRunning = 0;

            // Continue polling if still running
            if (isRunning && currentProcId) {
                pollTimer = setTimeout(() => pollOutput(procId), POLL_INTERVAL);
            }

        } catch (err) {
            // On network error, don't give up — the tab might just be throttled
            console.warn('Output poll error:', err.message);
            if (isRunning && currentProcId) {
                pollTimer = setTimeout(() => pollOutput(procId), POLL_INTERVAL * 2);
            }
        }
    }

    /**
     * Stop the polling timer
     */
    function stopPolling() {
        if (pollTimer) {
            clearTimeout(pollTimer);
            pollTimer = null;
        }
    }

    // ── Process State Management ───────────────────────────────────

    /**
     * Set the running state and update UI buttons
     * @param {boolean} running - true if a process is running
     */
    function setRunningState(running) {
        isRunning = running;

        const runBtn = document.getElementById('btn-run');
        const stopBtn = document.getElementById('btn-stop');

        if (runBtn) {
            runBtn.style.display = running ? 'none' : '';
        }
        if (stopBtn) {
            stopBtn.style.display = running ? '' : 'none';
        }
    }

    /**
     * Persist the running process ID to sessionStorage.
     * Called when a process starts so we can reconnect after page refresh.
     */
    function persistProcId(procId) {
        try { sessionStorage.setItem(PROC_ID_STORAGE_KEY, procId); } catch (_e) {}
    }

    /**
     * Clear the persisted process ID from sessionStorage.
     * Called when a process finishes or is stopped.
     */
    function clearPersistedProcId() {
        try { sessionStorage.removeItem(PROC_ID_STORAGE_KEY); } catch (_e) {}
    }

    /**
     * Recover a running process after page refresh.
     * Checks sessionStorage for a persisted proc_id and the backend
     * for the actual process state.
     */
    async function recoverRunningProcess() {
        let procId = null;
        try { procId = sessionStorage.getItem(PROC_ID_STORAGE_KEY); } catch (_e) {}

        // Also try to find any running process from backend
        if (!procId) {
            try {
                const resp = await fetch('/api/run/processes');
                if (resp.ok) {
                    const data = await resp.json();
                    const runningProcs = (data.processes || []).filter(p => p.running);
                    if (runningProcs.length > 0) {
                        procId = runningProcs[runningProcs.length - 1].id;
                    }
                }
            } catch (_e) {}
        }

        if (!procId) return;

        // Verify the process is actually running on the backend
        try {
            const resp = await fetch('/api/run/processes');
            if (!resp.ok) { clearPersistedProcId(); return; }
            const data = await resp.json();
            const proc = (data.processes || []).find(p => p.id === procId);
            if (!proc || !proc.running) {
                clearPersistedProcId();
                return;
            }

            // Process is still running — reconnect!
            appendOutput('[info] Detected running process after page refresh, reconnecting...', 'info');
            await reconnectToProcess(procId);
        } catch (_e) {
            clearPersistedProcId();
        }
    }

    /**
     * Reconnect to a specific process (running or finished).
     * Fetches missed output and re-establishes SSE streaming.
     * Can be called from the process tab's "输出" button or on page load recovery.
     */
    async function reconnectToProcess(procId) {
        if (!procId) return;

        // Show the panel
        showPanel();

        // Switch to output tab if not already active
        const outputTab = document.querySelector('[data-btab="output"]');
        if (outputTab && outputTab.classList && !outputTab.classList.contains('active')) {
            outputTab.click();
        }

        // Fetch all output from the beginning
        try {
            const outResp = await fetch(`/api/run/output?proc_id=${encodeURIComponent(procId)}&since=0`);
            if (outResp.ok) {
                const outData = await outResp.json();
                const lines = outData.outputs || [];
                if (Array.isArray(lines) && lines.length > 0) {
                    startCmdBlock(`Process ${procId}`);
                    for (const line of lines) {
                        const text = typeof line === 'string' ? line : (line.text || line.content || '');
                        const rawType = typeof line === 'object' ? (line.type || line.stream || 'stdout') : 'stdout';
                        const type = rawType === 'error' ? 'error' :
                                    rawType === 'status' ? 'system' :
                                    rawType === 'stderr' ? 'stderr' : 'stdout';
                        appendOutput(text, type);
                        if (type === 'stdout' && text) _parsePipLine(text);
                    }
                    pollSince = outData.since !== undefined ? outData.since : lines.length;
                }

                // Check if still running
                if (outData.running) {
                    currentProcId = procId;
                    setRunningState(true);
                    persistProcId(procId);
                    appendOutput(`[info] PID: ${procId} | Reconnected, streaming output...`, 'info');
                    streamOutput(procId);
                } else {
                    finishCmdBlock();
                    currentProcId = null;
                    setRunningState(false);
                    clearPersistedProcId();
                    appendOutput(`[info] Process ${procId} has finished.`, 'info');
                }
            }
        } catch (err) {
            appendOutput(`[error] Failed to reconnect: ${err.message}`, 'error');
        }
    }

    /**
     * Clean up after a process finishes
     */
    function cleanupProcess() {
        closeEventSource();
        stopPolling();
        finishCmdBlock();
        clearPersistedProcId();
        currentProcId = null;
        pollSince = 0;
        setRunningState(false);
        // Execute post-complete callback if registered
        if (onProcessComplete) {
            const cb = onProcessComplete;
            onProcessComplete = null;
            try { cb(); } catch (_e) {}
        }
        // Auto-focus back to input after process completes
        const si = document.getElementById('shell-input');
        if (si) si.focus();
    }

    // ── Visibility Recovery (tab minimize/restore) ──────────────

    /**
     * When the tab is backgrounded, SSE connections drop and setTimeout/setInterval
     * get throttled. When the tab becomes visible again, we need to:
     * 1. ALWAYS check backend for running processes (even if our local state says nothing is running)
     * 2. Re-establish SSE streaming if a process is still alive
     * 3. Otherwise, show the process has completed
     */
    let _visibilityHandler = null;
    function initVisibilityHandler() {
        if (_visibilityHandler) return;
        _visibilityHandler = true;

        document.addEventListener('visibilitychange', () => {
            if (!document.hidden) {
                // Tab just became visible again — always recover from backend
                _recoverRunState();
            }
        });

        // Also handle Page Lifecycle 'resume' event (mobile WebView)
        document.addEventListener('resume', () => {
            _recoverRunState();
        }, { passive: true });
    }

    async function _recoverRunState() {
        try {
            // ALWAYS check backend for running processes, regardless of local state.
            // This is critical because cleanupProcess() may have already cleared
            // currentProcId and isRunning when SSE dropped during background.
            const resp = await fetch('/api/run/processes');
            if (!resp.ok) return;
            const data = await resp.json();
            const processes = data.processes || [];
            const runningProcs = processes.filter(p => p.running !== false);

            if (runningProcs.length === 0) {
                // No running processes on backend either — clean up local state
                if (currentProcId && isRunning) {
                    cleanupProcess();
                }
                return;
            }

            // Found running processes on backend — find the one we should track
            // Prefer the one we were tracking before
            let targetProc = null;
            if (currentProcId) {
                targetProc = runningProcs.find(p => p.id === currentProcId);
            }
            // If our old proc is gone but others are running, pick the latest
            if (!targetProc) {
                targetProc = runningProcs[runningProcs.length - 1];
            }

            if (!targetProc) return;

            const procId = targetProc.id;

            // Update local state to match reality
            if (procId !== currentProcId) {
                currentProcId = procId;
                appendOutput(`[info] Reconnected to process ${procId}`, 'info');
            }
            if (!isRunning) {
                setRunningState(true);
            }
            persistProcId(procId);

            // Fetch any output we missed while backgrounded
            try {
                const outResp = await fetch(`/api/run/output?proc_id=${encodeURIComponent(procId)}&since=0`);
                if (outResp.ok) {
                    const outData = await outResp.json();
                    const lines = outData.outputs || [];
                    if (Array.isArray(lines) && lines.length > 0) {
                        for (const line of lines) {
                            const text = typeof line === 'string' ? line : (line.text || line.content || '');
                            const rawType = typeof line === 'object' ? (line.type || line.stream || 'stdout') : 'stdout';
                            const type = rawType === 'error' ? 'error' :
                                        rawType === 'status' ? 'system' :
                                        rawType === 'stderr' ? 'stderr' : 'stdout';
                            appendOutput(text, type);
                        }
                        pollSince = outData.since !== undefined ? outData.since : lines.length;
                    }
                }
            } catch (outErr) {
                console.warn('TerminalManager: failed to fetch missed output:', outErr.message);
            }

            // Re-connect SSE for live streaming
            streamOutput(procId);

        } catch (err) {
            console.warn('TerminalManager: visibility recovery error:', err.message);
            // On error, try to reconnect SSE with whatever state we have
            if (currentProcId) {
                streamOutput(currentProcId);
            }
        }
    }

    // ── Keyboard / Viewport Handling (Mobile) ──────────────────

    let keyboardOpen = false;
    let savedPanelHeight = 250;

    /**
     * Initialize visualViewport listener to handle soft keyboard
     * On Android WebView, when the keyboard appears, visualViewport shrinks.
     * We detect this and make the bottom panel float above the keyboard.
     */
    function initKeyboardHandler() {
        if (!window.visualViewport) return;

        const vv = window.visualViewport;

        const onResize = () => {
            const keyboardH = window.innerHeight - vv.height;
            const isKeyboard = keyboardH > 100 && document.activeElement &&
                (document.activeElement.id === 'shell-input' || document.activeElement.closest('#shell-input-bar'));

            const panel = document.getElementById('bottom-panel');
            if (!panel) return;

            if (isKeyboard) {
                keyboardOpen = true;
                // Save current height before keyboard override
                savedPanelHeight = panelHeight;
                // Expand panel to fill most of the visible area
                const targetH = vv.height - 44; // leave toolbar visible
                panel.classList.add('keyboard-open');
                panel.style.height = Math.max(targetH, 200) + 'px';
                // Scroll output to bottom so user sees latest
                const outputEl = document.getElementById('output-content');
                if (outputEl) setTimeout(() => outputEl.scrollTop = outputEl.scrollHeight, 50);
            } else if (keyboardOpen) {
                keyboardOpen = false;
                panel.classList.remove('keyboard-open');
                panel.style.height = savedPanelHeight + 'px';
            }
        };

        vv.addEventListener('resize', onResize);
        vv.addEventListener('scroll', onResize);

        // Also listen for focus/blur on shell-input as a fallback
        const shellInput = document.getElementById('shell-input');
        if (shellInput) {
            shellInput.addEventListener('focus', () => {
                // Delay to let keyboard animation start
                setTimeout(onResize, 100);
                setTimeout(onResize, 300);
            });
            shellInput.addEventListener('blur', () => {
                setTimeout(() => {
                    if (keyboardOpen) {
                        keyboardOpen = false;
                        const panel = document.getElementById('bottom-panel');
                        if (panel) {
                            panel.classList.remove('keyboard-open');
                            panel.style.height = savedPanelHeight + 'px';
                        }
                    }
                }, 100);
            });
        }
    }

    // ── Output Display ─────────────────────────────────────────────

    /**
     * Append a line of text to the output panel
     * @param {string} text - the text to append
     * @param {string} [type='stdout'] - type class: stdout, stderr, error, status, info, success, system
     */
    function appendOutput(text, type) {
        const container = document.getElementById('output-content');
        if (!container) return;

        type = type || 'stdout';

        const line = document.createElement('div');
        line.className = `output-line ${type}`;

        // Add timestamp for important lines (not stdout to avoid clutter)
        if (type !== 'stdout') {
            const ts = document.createElement('span');
            ts.className = 'log-time';
            ts.textContent = timestamp();
            line.appendChild(ts);
        }

        const textSpan = document.createTextNode(text || '');
        line.appendChild(textSpan);

        // Append to current command block if exists, otherwise directly to container
        const target = currentCmdBlock || container;
        target.appendChild(line);

        // Trim if too many lines (debounced)
        if (!_trimScheduled) {
            _trimScheduled = true;
            requestAnimationFrame(() => {
                trimOutput();
                _trimScheduled = false;
            });
        }

        // Smart auto-scroll: respect user's scroll position
        smartScrollToBottom();
    }

    /**
     * Start a new command block. Returns the block container.
     */
    function startCmdBlock(cmd) {
        const container = document.getElementById('output-content');
        if (!container) return null;

        // Finish previous block if any
        finishCmdBlock();

        const block = document.createElement('div');
        block.className = 'cmd-block';
        block.dataset.cmd = cmd;

        container.appendChild(block);
        currentCmdBlock = block;
        return block;
    }

    /**
     * Finish the current command block and add the forward-to-AI button.
     */
    function finishCmdBlock() {
        if (!currentCmdBlock) return;

        const block = currentCmdBlock;
        const cmd = block.dataset.cmd || '';
        currentCmdBlock = null;

        // Collect all output text from this block
        const lines = block.querySelectorAll('.output-line');
        let outputText = '';
        lines.forEach(line => {
            // Collect text from child nodes, skipping .log-time spans
            let txt = '';
            for (const node of line.childNodes) {
                if (node.nodeType === Node.TEXT_NODE) {
                    txt += node.textContent;
                } else if (node.nodeType === Node.ELEMENT_NODE && !node.classList.contains('log-time')) {
                    txt += node.textContent;
                }
            }
            if (txt.trim()) outputText += txt.trim() + '\n';
        });
        outputText = outputText.trim();

        // Add forward button
        const actions = document.createElement('div');
        actions.className = 'cmd-block-actions';

        const fwdBtn = document.createElement('button');
        fwdBtn.className = 'cmd-forward-btn';
        fwdBtn.title = '发送给AI助手';
        fwdBtn.textContent = '🤖 发送给AI';
        fwdBtn.addEventListener('click', () => {
            const msg = `命令: ${cmd}\n输出:\n${outputText}`;
            if (window.ChatManager) {
                window.ChatManager.sendMessage(msg);
                // Open chat sidebar if closed
                const sidebar = document.getElementById('sidebar-right');
                if (sidebar && !sidebar.classList.contains('open')) {
                    const chatBtn = document.getElementById('btn-chat');
                    if (chatBtn) chatBtn.click();
                }
            }
        });
        actions.appendChild(fwdBtn);
        block.appendChild(actions);
    }

    /**
     * Clear all output from the output panel
     */
    function clearOutput() {
        const container = document.getElementById('output-content');
        if (container) {
            container.innerHTML = '';
        }
        pollSince = 0;
    }

    // ── Panel Management ───────────────────────────────────────────

    /**
     * Show the bottom panel
     */
    function showPanel() {
        const panel = document.getElementById('bottom-panel');
        if (panel) {
            panel.style.display = '';
            panel.classList.add('visible');
            panel.style.height = panelHeight + 'px';
        }
    }

    /**
     * Hide the bottom panel
     */
    function hidePanel() {
        const panel = document.getElementById('bottom-panel');
        if (panel) {
            panel.style.display = 'none';
            panel.classList.remove('visible');
        }
    }

    /**
     * Toggle the bottom panel visibility
     */
    function togglePanel() {
        const panel = document.getElementById('bottom-panel');
        if (!panel) return;

        // Remove 'hidden' class first (CSS has display:none !important)
        panel.classList.remove('hidden');

        if (panel.style.display === 'none' || !panel.classList.contains('visible')) {
            showPanel();
        } else {
            hidePanel();
 }
    }

    /**
     * Set the panel height and persist it
     * @param {number} height - new height in pixels
     */
    function setPanelHeight(height) {
        height = Math.max(MIN_PANEL_HEIGHT, Math.min(MAX_PANEL_HEIGHT, height));
        panelHeight = height;

        const panel = document.getElementById('bottom-panel');
        if (panel) {
            panel.style.height = panelHeight + 'px';
        }
    }

    // ── Resize Handling ────────────────────────────────────────────

    /**
     * Initialize touch/mouse drag resize for the bottom panel
     */
    function initResize() {
        const handle = document.getElementById('bottom-panel-resize');
        if (!handle) return;

        // ── Touch events ──
        handle.addEventListener('touchstart', (e) => {
            e.preventDefault();
            isDragging = true;
            dragStartY = e.touches[0].clientY;
            dragStartHeight = panelHeight;
            handle.classList.add('active');
            document.body.classList.add('panel-resizing');
        }, { passive: false });

        document.addEventListener('touchmove', (e) => {
            if (!isDragging) return;
            e.preventDefault();
            const currentY = e.touches[0].clientY;
            const delta = dragStartY - currentY; // Dragging up increases height
            setPanelHeight(dragStartHeight + delta);
        }, { passive: false });

        document.addEventListener('touchend', () => {
            if (isDragging) {
                isDragging = false;
                const handle = document.getElementById('bottom-panel-resize');
                if (handle) handle.classList.remove('active');
                document.body.classList.remove('panel-resizing');
            }
        });

        // ── Mouse events (desktop) ──
        handle.addEventListener('mousedown', (e) => {
            e.preventDefault();
            isDragging = true;
            dragStartY = e.clientY;
            dragStartHeight = panelHeight;
            handle.classList.add('active');
            document.body.classList.add('panel-resizing');
        });

        document.addEventListener('mousemove', (e) => {
            if (!isDragging) return;
            e.preventDefault();
            const currentY = e.clientY;
            const delta = dragStartY - currentY;
            setPanelHeight(dragStartHeight + delta);
        });

        document.addEventListener('mouseup', () => {
            if (isDragging) {
                isDragging = false;
                const handle = document.getElementById('bottom-panel-resize');
                if (handle) handle.classList.remove('active');
                document.body.classList.remove('panel-resizing');
            }
        });
    }

    // ── Wire Up ────────────────────────────────────────────────────

    function wireEvents() {
        // Expose for DebugManager keyboard-open state
        window.addEventListener('shell:focus', () => {
            const panel = document.getElementById('bottom-panel');
            if (panel) panel.style.height = savedPanelHeight + 'px';
        });

        // Close panel button
        const closeBtn = document.getElementById('bottom-panel-close');
        if (closeBtn) {
            closeBtn.addEventListener('click', (e) => {
                e.preventDefault();
                hidePanel();
            });
        }

        // Clear output button
        const clearBtn = document.getElementById('bottom-panel-clear');
        if (clearBtn) {
            clearBtn.addEventListener('click', (e) => {
                e.preventDefault();
                clearOutput();
            });
        }

        // Run button is handled by AppManager (with file picker), not here.
        // Stop button
        const stopBtn = document.getElementById('btn-stop');
        if (stopBtn) {
            stopBtn.addEventListener('click', (e) => {
                e.preventDefault();
                stop();
            });
            // Initially hidden (no process running)
            stopBtn.style.display = 'none';
        }

        // Initialize resize handle
        initResize();

        // ── Shell Input Bar ──
        initShellInput();

        // ── Terminal Extra Keys ──
        initExtraKeys();
    }

    // ── Shell Input Bar ──────────────────────────────────────────

    let shellHistory = [];
    let shellHistoryIndex = -1;

    function initShellInput() {
        const shellInput = document.getElementById('shell-input');
        if (!shellInput) return;

        function handleShellEnter() {
            const cmd = shellInput.value.trim();
            if (!cmd) return;

            // Debounce: prevent double-fire from keydown+keyup+keypress
            if (handleShellEnter._busy) return;
            handleShellEnter._busy = true;
            setTimeout(() => { handleShellEnter._busy = false; }, 300);

            // Add to history
            shellHistory.push(cmd);
            shellHistoryIndex = shellHistory.length;

            // Start a new command block
            startCmdBlock(cmd);

            // Show the command in output
            const prompt = platformInfo.shell_prompt || '$';
            appendOutput(`─────────────────────────────────────────`, 'status');
            appendOutput(`${prompt} ${cmd}`, 'system');
            appendOutput(`[info] Shell command | Time: ${new Date().toLocaleString()}`, 'info');

            // Execute via shell API (runs commands directly, not as code files)
            executeShellCommand(cmd);
            shellInput.value = '';
            // Keep focus on input after sending command.
            // On mobile WebView the soft keyboard may dismiss on Enter,
            // so use a short delay to re-focus after the key event settles.
            setTimeout(() => { shellInput.focus(); }, 50);
        }

        shellInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                handleShellEnter();
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                if (shellHistoryIndex > 0) {
                    shellHistoryIndex--;
                    shellInput.value = shellHistory[shellHistoryIndex] || '';
                }
            } else if (e.key === 'ArrowDown') {
                e.preventDefault();
                if (shellHistoryIndex < shellHistory.length - 1) {
                    shellHistoryIndex++;
                    shellInput.value = shellHistory[shellHistoryIndex] || '';
                } else {
                    shellHistoryIndex = shellHistory.length;
                    shellInput.value = '';
                }
            } else if (e.key === 'Tab') {
                e.preventDefault();
                // Simple tab completion - not full, but inserts a tab character
                const start = shellInput.selectionStart;
                const end = shellInput.selectionEnd;
                shellInput.value = shellInput.value.substring(0, start) + '\t' + shellInput.value.substring(end);
                shellInput.selectionStart = shellInput.selectionEnd = start + 1;
            } else if (e.key === 'c' && e.ctrlKey) {
                e.preventDefault();
                appendOutput('^C', 'status');
                stop();
            } else if (e.key === 'l' && e.ctrlKey) {
                e.preventDefault();
                clearOutput();
            }
        });

        // Fallback: some Android WebViews / soft keyboards fire keypress or keyup
        // instead of keydown for the Enter key. Listen for them too.
        shellInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                handleShellEnter();
            }
        });

        shellInput.addEventListener('keyup', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                handleShellEnter();
            }
        });

        // Fallback: some mobile keyboards submit the parent form instead
        const inputBar = document.getElementById('shell-input-bar');
        if (inputBar) {
            inputBar.addEventListener('submit', (e) => {
                e.preventDefault();
                handleShellEnter();
            });
        }

        // Send button: guaranteed to work on all devices
        const sendBtn = document.getElementById('shell-send-btn');
        if (sendBtn) {
            if (window.bindTouchButton) {
                window.bindTouchButton(sendBtn, () => handleShellEnter());
            } else {
                sendBtn.addEventListener('click', () => {
                    handleShellEnter();
                    shellInput.focus();
                });
            }
        }

        // Focus shell input when clicking on output area
        const outputContent = document.getElementById('output-content');
        if (outputContent) {
            outputContent.addEventListener('click', () => {
                shellInput.focus();
            });
        }
    }

    // ── Terminal Extra Keys ──────────────────────────────────────

    let ctrlActive = false;

    function initExtraKeys() {
        const keysBar = document.getElementById('terminal-extra-keys');
        if (!keysBar) return;

        const keyMap = {
            'esc': '\x1b',
            'tab': '\t',
            'up': '\x1b[A',
            'down': '\x1b[B',
            'left': '\x1b[D',
            'right': '\x1b[C',
            'home': '\x1b[H',
            'end': '\x1b[F',
            'pgup': '\x1b[5~',
            'pgdn': '\x1b[6~',
            'pipe': '|',
            'slash': '/',
            'tilde': '~',
            'minus': '-',
            'enter': '\r',
        };

        keysBar.querySelectorAll('.tkey').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                const key = btn.dataset.key;
                const shellInput = document.getElementById('shell-input');
                if (!shellInput) return;

                if (key === 'ctrl') {
                    ctrlActive = !ctrlActive;
                    btn.classList.toggle('active', ctrlActive);
                    return;
                }

                // If CTRL is active and key is a single letter, send Ctrl+key
                if (ctrlActive && key.length === 1 && key >= 'a' && key <= 'z') {
                    // Insert the control character into the shell input
                    const ctrlChar = String.fromCharCode(key.charCodeAt(0) - 96);
                    const pos = shellInput.selectionStart;
                    shellInput.value = shellInput.value.substring(0, pos) + ctrlChar + shellInput.value.substring(shellInput.selectionEnd);
                    shellInput.selectionStart = shellInput.selectionEnd = pos + 1;
                    ctrlActive = false;
                    const ctrlBtn = keysBar.querySelector('[data-key="ctrl"]');
                    if (ctrlBtn) ctrlBtn.classList.remove('active');
                    return;
                }

                // For escape sequences that should trigger shell input behaviors
                if (key === 'up' || key === 'down') {
                    // Simulate arrow key for history navigation
                    const event = new KeyboardEvent('keydown', {
                        key: key === 'up' ? 'ArrowUp' : 'ArrowDown',
                        bubbles: true
                    });
                    shellInput.dispatchEvent(event);
                    return;
                }

                if (key === 'enter') {
                    const event = new KeyboardEvent('keydown', {
                        key: 'Enter',
                        bubbles: true
                    });
                    shellInput.dispatchEvent(event);
                    return;
                }

                // For other keys, insert the character
                const char = keyMap[key];
                if (char) {
                    const pos = shellInput.selectionStart;
                    shellInput.value = shellInput.value.substring(0, pos) + char + shellInput.value.substring(shellInput.selectionEnd);
                    shellInput.selectionStart = shellInput.selectionEnd = pos + char.length;
                    shellInput.focus();
                }
            });
        });
    }

    // ── Initialize ─────────────────────────────────────────────────

    function init() {
        wireEvents();
        loadPlatformInfo();  // Detect OS and adjust shell behavior
        loadCompilers();
        loadVenvInfo();
        setRunningState(false);
        initKeyboardHandler();
        initVisibilityHandler();
        initScrollDetection();  // Smart auto-scroll with user scroll detection

        // Print startup banner with system info
        printStartupBanner();

        // After banner, check for a running process that survived a page refresh.
        // This reads from sessionStorage (set when a process starts) and
        // verifies with the backend whether the process is still alive.
        setTimeout(() => recoverRunningProcess(), 300);

        // Auto-detect venv when workspace changes
        window.addEventListener('workspace:changed', () => {
            loadVenvInfo();
            appendOutput('[system] Workspace changed: ' + (window.FileManager ? window.FileManager.workspacePath : 'unknown'), 'system');
        });

        // Re-bind venv to project directory when project is opened/closed
        document.addEventListener('project:opened', () => {
            loadVenvInfo();
            appendOutput('[system] Project opened — virtual environment re-scanned', 'system');
        });
        document.addEventListener('project:closed', () => {
            loadVenvInfo();
            appendOutput('[system] Project closed — virtual environment reset to workspace', 'system');
        });
    }

    /**
     * Print a startup banner with useful system info
     */
    async function printStartupBanner() {
        const lines = [];
        lines.push('╔══════════════════════════════════════╗');
        lines.push('║         MusIDE Terminal v3.0        ║');
        lines.push('╚══════════════════════════════════════╝');
        lines.push('');

        // System info
        const ua = navigator.userAgent;
        const isAndroid = /Android/i.test(ua);
        const isIOS = /iPhone|iPad|iPod/i.test(ua);
        const platform = isAndroid ? 'Android' : (isIOS ? 'iOS' : navigator.platform);
        const screenInfo = `${window.innerWidth}x${window.innerHeight}`;
        const viewportInfo = window.visualViewport ? `${window.visualViewport.width}x${window.visualViewport.height}` : screenInfo;

        lines.push(`[system] Platform: ${platform}`);
        lines.push(`[system] Screen: ${screenInfo} | Viewport: ${viewportInfo}`);
        if (platformInfo.is_windows) {
            lines.push(`[system] Server OS: Windows (shell: cmd.exe)`);
        } else if (platformInfo.is_termux) {
            lines.push(`[system] Server OS: Linux/Termux`);
        } else {
            lines.push(`[system] Server OS: ${platformInfo.platform || 'Linux'}`);
        }
        lines.push(`[system] Shell: ${platformInfo.default_shell || 'bash'} | Prompt: ${platformInfo.shell_prompt || '$'}`);
        lines.push('');

        // Fetch server info
        try {
            const resp = await fetch('/api/health');
            if (resp.ok) {
                const data = await resp.json();
                lines.push(`[system] Server: OK (v${data.version || '?'}) on port ${data.port || '?'}`);
            }
        } catch (e) {
            lines.push(`[system] Server: Connection failed - ${e.message}`);
        }

        // Fetch config info
        try {
            const resp = await fetch('/api/config');
            if (resp.ok) {
                const cfg = await resp.json();
                lines.push(`[system] Workspace: ${cfg.workspace || '?'}`);
                if (cfg.venv_path) {
                    lines.push(`[system] Venv: ${cfg.venv_path}`);
                }
                lines.push(`[system] Compiler: ${cfg.compiler || 'auto'}`);
            }
        } catch (e) {
            lines.push(`[system] Config: unavailable`);
        }

        // Fetch compilers
        try {
            const resp = await fetch('/api/compilers');
            if (resp.ok) {
                const data = await resp.json();
                const comps = data.compilers || [];
                if (comps.length > 0) {
                    lines.push(`[system] Available: ${comps.map(c => c.id).join(', ')}`);
                }
            }
        } catch (e) {}

        lines.push('');
        lines.push('[info] Type commands below and press Enter to execute.');
        lines.push('[info] Use Run button (▶) to execute the current file.');
        lines.push('');

        for (const l of lines) {
            const type = l.startsWith('[system]') ? 'system' :
                         l.startsWith('[info]') ? 'info' :
                         l.startsWith('╔') || l.startsWith('║') || l.startsWith('╚') ? 'status' : 'stdout';
            appendOutput(l, type);
        }
    }

    // Auto-init when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // ── API: Venv ─────────────────────────────────────────────────

    /**
     * Load venv info from backend and update UI
     */
    async function loadVenvInfo() {
        try {
            const resp = await fetch('/api/venv/list');
            if (!resp.ok) throw new Error(`Failed to load venv: ${resp.statusText}`);
            const data = await resp.json();

            const currentVenvEl = document.getElementById('current-venv');
            if (currentVenvEl) {
                if (data.current) {
                    const name = data.current.split('/').pop();
                    currentVenvEl.textContent = name;
                } else if (data.cleared_stale) {
                    currentVenvEl.textContent = '未设置 (旧环境已清除)';
                } else {
                    currentVenvEl.textContent = '未设置';
                }
            }

            // Auto-activate first found venv if none is active
            if (!data.current && data.venvs && data.venvs.length > 0) {
                const firstVenv = data.venvs[0];
                if (firstVenv.path) {
                    try {
                        await fetch('/api/venv/activate', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ path: firstVenv.path })
                        });
                        if (currentVenvEl) {
                            currentVenvEl.textContent = firstVenv.name;
                        }
                    } catch (_e) {}
                }
            }

            // Show/hide venv packages list
            const venvPackagesDiv = document.getElementById('venv-packages');
            const activeVenv = data.current || (data.venvs && data.venvs.length > 0 ? data.venvs[0].full_path : '');
            if (venvPackagesDiv && activeVenv) {
                try {
                    const pkgResp = await fetch('/api/venv/packages');
                    if (pkgResp.ok) {
                        const pkgData = await pkgResp.json();
                        const pkgList = document.getElementById('venv-pkg-list');
                        if (pkgList && pkgData.packages) {
                            pkgList.innerHTML = pkgData.packages.map(p =>
                                `<div style="padding:2px 8px;">${p.name || p.key} ${p.version || ''}</div>`
                            ).join('');
                            venvPackagesDiv.style.display = '';
                        }
                    }
                } catch (_e) {}
            } else if (venvPackagesDiv) {
                venvPackagesDiv.style.display = 'none';
            }

            return data;
        } catch (err) {
            console.warn('Failed to load venv info:', err.message);
            return null;
        }
    }

    /**
     * Create a virtual environment
     */
    async function createVenv(path) {
        if (!path) {
            if (window.showPromptDialog) {
                path = await new Promise(resolve => {
                    window.showPromptDialog('创建虚拟环境', '输入路径 (留空使用默认 .venv):', '.venv', resolve);
                });
            } else {
                path = prompt('Enter venv path:', '.venv');
            }
        }
        if (!path) return;

        showPanel();
        appendOutput('$ Creating virtual environment...', 'status');

        try {
            const resp = await fetch('/api/venv/create', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path })
            });
            if (!resp.ok) throw new Error(`Failed to create venv: ${resp.statusText}`);

            const data = await resp.json();
            if (data.proc_id) {
                currentProcId = data.proc_id;
                pollSince = 0;
                setRunningState(true);
                streamOutput(data.proc_id);
            } else {
                appendOutput('Virtual environment created.', 'info');
                showToast('虚拟环境已创建', 'success');
            }

            // Refresh venv info after process completes
            if (data.proc_id) {
                onProcessComplete = () => {
                    loadVenvInfo().then(() => {
                        showToast('虚拟环境已创建', 'success');
                    });
                };
            } else {
                await loadVenvInfo();
                showToast('虚拟环境已创建', 'success');
            }
            return data;
        } catch (err) {
            appendOutput(`Error: ${err.message}`, 'error');
            showToast(`创建失败: ${err.message}`, 'error');
            return { error: err.message };
        }
    }

    /**
     * Install a Python package — streaming output so each package appears as installed
     */
    async function installPackage(packageName) {
        if (!packageName) {
            if (window.showPromptDialog) {
                packageName = await new Promise(resolve => {
                    window.showPromptDialog('安装包', '输入包名:', '', resolve);
                });
            } else {
                packageName = prompt('Enter package name:');
            }
        }
        if (!packageName) return;

        showPanel();
        appendOutput(`$ pip install ${packageName}...`, 'status');

        // Show progress area
        _showVenvProgress('正在安装 ' + packageName, 0, 1);

        try {
            // Use streaming subprocess instead of capture_output so output is real-time
            const resp = await fetch('/api/run/execute', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    code: `import subprocess, sys\n\n# Bootstrap pip if missing\ntry:\n    import pip\nexcept ImportError:\n    print('[info] pip not found, bootstrapping via ensurepip...')\n    r = subprocess.run([sys.executable, '-m', 'ensurepip', '--upgrade', '--default-pip'], capture_output=True, text=True)\n    if r.returncode == 0:\n        print('[ok] pip installed successfully')\n    else:\n        print('[error] Failed to bootstrap pip. Please install pip manually.')\n        print(r.stderr)\n        sys.exit(1)\n\nproc = subprocess.Popen([sys.executable, '-m', 'pip', 'install', '${packageName.replace(/'/g, "\\'")}'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)\nfor line in proc.stdout:\n    print(line, end='')\n    sys.stdout.flush()\nproc.wait()\nif proc.returncode != 0:\n    print(f'\\nExit code: {proc.returncode}')`,
                    compiler: 'python3'
                })
            });
            if (!resp.ok) throw new Error(`Failed: ${resp.statusText}`);

            const data = await resp.json();
            if (data.proc_id) {
                currentProcId = data.proc_id;
                pollSince = 0;
                setRunningState(true);
                // Set callback to refresh package list after install
                onProcessComplete = () => {
                    _hideVenvProgress();
                    loadVenvInfo();
                };
                streamOutput(data.proc_id);
            }
            return data;
        } catch (err) {
            appendOutput(`Error: ${err.message}`, 'error');
            showToast(`安装失败: ${err.message}`, 'error');
            _hideVenvProgress();
            return { error: err.message };
        }
    }

    /**
     * Import requirements.txt — streaming pip install with progress tracking
     * Each installed dependency appears in real-time, plus progress bar
     */
    async function importRequirements() {
        // Confirm dialog
        if (window.showConfirmDialog) {
            const confirmed = await new Promise(resolve => {
                window.showConfirmDialog(
                    '导入依赖包',
                    '将按照当前项目根目录的 requirements.txt 安装所有依赖包，安装过程将在控制台显示。\n\n是否继续？',
                    resolve
                );
            });
            if (!confirmed) return;
        }

        // Get the current project root directory for requirements.txt
        let projectRoot = '';
        if (window.ProjectManager) {
            const proj = window.ProjectManager.getCurrentProject();
            if (proj && proj.project) {
                projectRoot = proj.project.replace(/^\//, '');
            }
        }
        if (!projectRoot) {
            showToast('请先打开一个项目', 'error');
            return;
        }

        // Close left sidebar to reveal console
        const sidebarLeft = document.getElementById('sidebar-left');
        if (sidebarLeft && sidebarLeft.classList.contains('open')) {
            const closeBtn = document.getElementById('btn-menu');
            if (closeBtn) closeBtn.click();
        }

        // Expand console panel
        showPanel();
        setPanelHeight(Math.min(400, MAX_PANEL_HEIGHT));

        // Start command block for AI forward button
        startCmdBlock('pip install -r requirements.txt');

        appendOutput(`[info] 项目目录: ${projectRoot}`, 'info');
        appendOutput('正在按照 requirements.txt 安装依赖包...', 'info');
        appendOutput('─────────────────────────────────────────', 'status');
        appendOutput('$ pip install -r requirements.txt', 'system');
        appendOutput(`[info] Time: ${new Date().toLocaleString()}`, 'info');

        // Show progress area with indeterminate state
        _showVenvProgress('正在读取 requirements.txt...', 0, 0);

        // Track collected packages for progress
        let collectedPkgs = 0;
        let installedPkgs = 0;
        const _origAppend = appendOutput;

        try {
            // The server /api/run/execute sets CWD to the project directory,
            // so requirements.txt is simply './requirements.txt' in the current directory.
            // We use os.path.abspath to show the full path in the output for clarity.
            const resp = await fetch('/api/run/execute', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    code: `import subprocess, sys, os\n\n# Bootstrap pip if missing (minimal Python installs may not include it)\ntry:\n    import pip\nexcept ImportError:\n    print('[info] pip not found, bootstrapping via ensurepip...')\n    r = subprocess.run([sys.executable, '-m', 'ensurepip', '--upgrade', '--default-pip'], capture_output=True, text=True)\n    if r.returncode == 0:\n        print('[ok] pip installed successfully')\n    else:\n        print('[warn] ensurepip failed, trying get-pip.py fallback...')\n        try:\n            import urllib.request\n            url = 'https://bootstrap.pypa.io/get-pip.py'\n            dest = os.path.join(os.path.dirname(sys.executable), 'get-pip.py')\n            urllib.request.urlretrieve(url, dest)\n            r2 = subprocess.run([sys.executable, dest, '--force-reinstall'], capture_output=True, text=True)\n            os.remove(dest)\n            if r2.returncode == 0:\n                print('[ok] pip installed via get-pip.py')\n            else:\n                print('[error] Failed to install pip. Please install pip manually.')\n                print(r2.stderr)\n                sys.exit(1)\n        except Exception as e:\n            print(f'[error] get-pip.py fallback also failed: {e}')\n            sys.exit(1)\n\nreq_file = 'requirements.txt'\nif not os.path.exists(req_file):\n    print(f'Error: {os.path.abspath(req_file)} not found')\n    print('Please ensure requirements.txt exists in the project root directory.')\n    sys.exit(1)\n\nabs_path = os.path.abspath(req_file)\nprint(f'Installing from: {abs_path}')\nprint(f'Project root: {os.getcwd()}')\nproc = subprocess.Popen([sys.executable, '-m', 'pip', 'install', '-r', req_file], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)\nfor line in proc.stdout:\n    print(line, end='')\n    sys.stdout.flush()\nproc.wait()\nif proc.returncode != 0:\n    print(f'\\nExit code: {proc.returncode}')`,
                    compiler: 'python3'
                })
            });
            if (!resp.ok) throw new Error(`Failed: ${resp.statusText}`);
            const data = await resp.json();
            if (data.proc_id) {
                currentProcId = data.proc_id;
                pollSince = 0;
                setRunningState(true);

                // Set callback to refresh and hide progress
                onProcessComplete = () => {
                    _hideVenvProgress();
                    loadVenvInfo().then(() => {
                        showToast('依赖安装完成，包列表已刷新', 'success');
                    });
                };

                // Track SSE output for progress updates
                const _origStreamOutput = streamOutput;
                // We hook into appendOutput to detect pip lines
                const _origFn = window.TerminalManager.appendOutput;

                streamOutput(data.proc_id);
            }
        } catch (err) {
            appendOutput(`Error: ${err.message}`, 'error');
            showToast(`安装失败: ${err.message}`, 'error');
            _hideVenvProgress();
        }
    }

    // ── Venv Progress Helpers ──────────────────────────────────────

    let _venvProgressState = { total: 0, collected: 0, installed: 0 };

    function _showVenvProgress(text, current, total) {
        const area = document.getElementById('venv-progress-area');
        if (!area) return;
        area.style.display = '';

        const venvPackages = document.getElementById('venv-packages');
        if (venvPackages) venvPackages.style.display = '';

        const textEl = document.getElementById('venv-progress-text');
        const barEl = document.getElementById('venv-progress-bar');
        const countEl = document.getElementById('venv-progress-count');
        const pkgEl = document.getElementById('venv-current-pkg');

        if (textEl) textEl.textContent = text || '正在安装...';
        if (countEl) countEl.textContent = total > 0 ? `${current}/${total}` : '...';
        if (barEl) {
            if (total > 0 && current > 0) {
                barEl.style.width = Math.round((current / total) * 100) + '%';
            } else {
                // Indeterminate: animate
                barEl.style.width = '30%';
                barEl.style.animation = 'venv-progress-indeterminate 1.5s ease-in-out infinite';
            }
        }
        if (pkgEl) pkgEl.textContent = '';

        _venvProgressState = { total: total, collected: current, installed: current };

        // Ensure venv-packages section is visible
        const venvInfo = document.getElementById('current-venv');
        if (venvInfo && venvInfo.textContent !== '未设置') {
            area.style.display = '';
        }
    }

    function _updateVenvProgress(text, current, total) {
        const textEl = document.getElementById('venv-progress-text');
        const barEl = document.getElementById('venv-progress-bar');
        const countEl = document.getElementById('venv-progress-count');
        const pkgEl = document.getElementById('venv-current-pkg');

        if (textEl && text) textEl.textContent = text;
        if (countEl) countEl.textContent = total > 0 ? `${current}/${total}` : '...';
        if (barEl) {
            if (total > 0 && current > 0) {
                barEl.style.animation = 'none';
                barEl.style.width = Math.round((current / total) * 100) + '%';
            }
        }
        if (pkgEl) pkgEl.textContent = text || '';
    }

    function _hideVenvProgress() {
        const area = document.getElementById('venv-progress-area');
        if (area) area.style.display = 'none';
        _venvProgressState = { total: 0, collected: 0, installed: 0 };
    }

    /**
     * Parse pip output line and update progress bar accordingly.
     * Called for each line of pip output during streaming.
     */
    function _parsePipLine(line) {
        const trimmed = (line || '').trim();

        // "Collecting package-name"
        const collectMatch = trimmed.match(/^Collecting\s+(\S+)/);
        if (collectMatch) {
            _venvProgressState.collected++;
            const total = Math.max(_venvProgressState.total, _venvProgressState.collected);
            _venvProgressState.total = total;
            _updateVenvProgress(
                '正在收集: ' + collectMatch[1],
                _venvProgressState.collected, total
            );
            return;
        }

        // "Using cached package-name" or "Requirement already satisfied: package-name"
        const usingMatch = trimmed.match(/^(?:Using cached|Requirement already satisfied):\s+(\S+)/);
        if (usingMatch) {
            _venvProgressState.collected++;
            _venvProgressState.installed++;
            const total = Math.max(_venvProgressState.total, _venvProgressState.collected);
            _venvProgressState.total = total;
            _updateVenvProgress(
                '已缓存: ' + usingMatch[1],
                _venvProgressState.installed, total
            );
            return;
        }

        // "Downloading package-name"
        const dlMatch = trimmed.match(/^Downloading\s+(\S+)/);
        if (dlMatch) {
            _updateVenvProgress(
                '正在下载: ' + dlMatch[1],
                _venvProgressState.collected, _venvProgressState.total
            );
            return;
        }

        // "Installing collected packages: ..."
        const installMatch = trimmed.match(/^Installing collected packages:\s*(.+)/);
        if (installMatch) {
            _venvProgressState.total = _venvProgressState.collected;
            _venvProgressState.installed = 0;
            const pkgs = installMatch[1].split(',').map(s => s.trim());
            _updateVenvProgress(
                '正在安装...',
                0, pkgs.length
            );
            // Count each comma-separated package as installed
            pkgs.forEach((pkg, i) => {
                setTimeout(() => {
                    _venvProgressState.installed = i + 1;
                    _updateVenvProgress(
                        '正在安装: ' + pkg.trim(),
                        i + 1, pkgs.length
                    );
                }, (i + 1) * 200);
            });
            return;
        }

        // "Successfully installed package-name ..."
        const successMatch = trimmed.match(/^Successfully installed\s+(.+)/);
        if (successMatch) {
            const pkgs = successMatch[1].split(/\s+/).filter(Boolean);
            _venvProgressState.total = pkgs.length;
            _venvProgressState.installed = pkgs.length;
            _updateVenvProgress(
                '安装完成!',
                pkgs.length, pkgs.length
            );
            return;
        }
    }

    // Hook into appendOutput to parse pip lines for progress bar
    const _originalAppendOutput = appendOutput;
    // We need to wrap appendOutput after it's defined, so we do it at init time
    let _appendOutputWrapped = false;

    function _wrapAppendOutput() {
        if (_appendOutputWrapped) return;
        _appendOutputWrapped = true;
        const orig = appendOutput;
        // Store reference for wrapper
        window.TerminalManager._origAppendOutput = orig;
    }

    // ── Public API ─────────────────────────────────────────────────
    return {
        execute,
        executeCode,
        stop,
        togglePanel,
        clearOutput,
        streamOutput,
        appendOutput,
        pollOutput,
        loadCompilers,
        getSelectedCompiler,
        showPanel,
        hidePanel,
        loadVenvInfo,
        createVenv,
        installPackage,
        importRequirements,
        reconnectToProcess,
        recoverRunningProcess,
        startCmdBlock,
        finishCmdBlock,

        // Getters
        get currentProcId() { return currentProcId; },
        get isRunning() { return isRunning; },
        get compilers() { return compilers; },
        get panelHeight() { return panelHeight; },
        set panelHeight(v) { setPanelHeight(v); }
    };
})();

// Also expose as window.TerminalManager for external access
window.TerminalManager = TerminalManager;
