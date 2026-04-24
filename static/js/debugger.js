/**
 * DebuggerUI - Python Runtime Debugger UI Module
 * Manages the debug tab in the bottom panel, breakpoint gutter in editor,
 * variable inspection, call stack display, and AI debug activity log.
 */
const DebuggerUI = (() => {
    'use strict';

    // ── State ──
    let state = {
        debugState: 'idle',    // idle, running, paused, stopped
        file: '',
        line: 0,
        func: '',
        breakpoints: {},       // filepath -> [line, ...]
        callStack: [],
        localVars: {},
        output: [],
    };
    let sseSource = null;
    let currentLineMarker = null;
    let breakpointMarkers = {};  // filepath -> [lineWidget, ...]

    // ── Init ──
    function init() {
        initControlButtons();
        initEvaluateInput();
        initAIActivityLog();
        // Listen for file open/close to manage breakpoint persistence
        document.addEventListener('file:opened', onFileOpened);
        document.addEventListener('debug:breakpoint_toggle', onBreakpointToggle);
        // Listen for AI debug tool calls
        document.addEventListener('debug:ai_activity', onAIActivity);
    }

    // ── Control Buttons ──
    function initControlButtons() {
        bindBtn('debug-btn-start', startDebug);
        bindBtn('debug-btn-stop', stopDebug);
        bindBtn('debug-btn-continue', continueDebug);
        bindBtn('debug-btn-step-in', () => stepDebug('step_in'));
        bindBtn('debug-btn-step-over', () => stepDebug('step_over'));
        bindBtn('debug-btn-step-out', () => stepDebug('step_out'));
    }

    function bindBtn(id, handler) {
        const btn = document.getElementById(id);
        if (btn) {
            btn.addEventListener('click', handler);
        }
    }

    // ── Start Debug ──
    async function startDebug() {
        // Priority: 1) persisted run file, 2) currently open editor file
        let filePath = window.RunConfig ? RunConfig.getRunFile() : '';
        filePath = filePath || (window.EditorManager ? EditorManager.getCurrentFile() : null);

        if (!filePath) {
            // No file bound — show file picker
            if (window.showFilePickerDialog) {
                try {
                    filePath = await showFilePickerDialog('选择调试文件');
                    if (filePath && window.RunConfig) {
                        RunConfig.setRunFile(filePath);
                    }
                } catch (e) {
                    if (window.showToast) window.showToast('获取文件列表失败', 'error');
                    return;
                }
            }
            if (!filePath) {
                if (window.showToast) window.showToast('请先选择一个文件', 'error');
                return;
            }
        }

        // Collect breakpoints for current file
        const bpLines = getBreakpointsForFile(filePath);

        try {
            const resp = await fetch('/api/debug/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ file_path: filePath, breakpoints: bpLines })
            });
            const data = await resp.json();
            if (data.ok) {
                // Open bottom panel and switch to debug tab
                openDebugTab();
                // Start SSE listener
                connectSSE();
                if (window.showToast) window.showToast('调试已启动', 'success', 1500);
            } else {
                if (window.showToast) window.showToast('启动调试失败: ' + (data.error || ''), 'error');
            }
        } catch (err) {
            if (window.showToast) window.showToast('调试请求失败: ' + err.message, 'error');
        }
    }

    // ── Stop Debug ──
    async function stopDebug() {
        try {
            await fetch('/api/debug/stop', { method: 'POST' });
            disconnectSSE();
            clearCurrentLineHighlight();
            updateUI();
            if (window.showToast) window.showToast('调试已停止', 'info', 1500);
        } catch (e) {}
    }

    // ── Continue ──
    async function continueDebug() {
        try {
            const resp = await fetch('/api/debug/continue', { method: 'POST' });
            const data = await resp.json();
            if (!data.ok && window.showToast) {
                window.showToast('无法继续: 会话未暂停', 'error');
            }
        } catch (e) {}
    }

    // ── Step ──
    async function stepDebug(action) {
        try {
            const resp = await fetch('/api/debug/step', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action })
            });
            const data = await resp.json();
            if (!data.ok && window.showToast) {
                window.showToast('无法步进: 会话未暂停', 'error');
            }
        } catch (e) {}
    }

    // ── Evaluate Expression ──
    function initEvaluateInput() {
        const input = document.getElementById('debug-eval-input');
        const btn = document.getElementById('debug-eval-btn');
        if (input) {
            input.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    evaluateExpression(input.value.trim());
                }
            });
        }
        if (btn) {
            btn.addEventListener('click', () => {
                if (input) evaluateExpression(input.value.trim());
            });
        }
    }

    async function evaluateExpression(expr) {
        if (!expr) return;
        const output = document.getElementById('debug-eval-output');
        if (output) {
            output.innerHTML += '<div class="debug-eval-line"><span class="eval-prompt">&gt;&gt;&gt;</span> ' + escapeHTML(expr) + '</div>';
        }

        try {
            const resp = await fetch('/api/debug/evaluate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ expression: expr })
            });
            const data = await resp.json();
            if (data.ok) {
                if (output) {
                    output.innerHTML += '<div class="debug-eval-line eval-result">' + escapeHTML(data.result) + '</div>';
                }
            } else {
                if (output) {
                    output.innerHTML += '<div class="debug-eval-line eval-error">' + escapeHTML(data.error || 'Error') + '</div>';
                }
            }
        } catch (err) {
            if (output) {
                output.innerHTML += '<div class="debug-eval-line eval-error">' + escapeHTML(err.message) + '</div>';
            }
        }
        if (output) output.scrollTop = output.scrollHeight;
    }

    // ── SSE Connection ──
    function connectSSE() {
        disconnectSSE();
        sseSource = new EventSource('/api/debug/state/stream');
        sseSource.onmessage = (e) => {
            try {
                const newState = JSON.parse(e.data);
                onStateUpdate(newState);
            } catch (err) {}
        };
        sseSource.addEventListener('done', () => {
            disconnectSSE();
        });
        sseSource.onerror = () => {
            // Don't auto-reconnect for debug sessions
        };
    }

    function disconnectSSE() {
        if (sseSource) {
            sseSource.close();
            sseSource = null;
        }
    }

    // ── State Update Handler ──
    function onStateUpdate(newState) {
        const prevState = state.debugState;
        state.debugState = newState.state || 'idle';
        state.file = newState.file || '';
        state.line = newState.line || 0;
        state.func = newState.func || '';
        state.breakpoints = newState.breakpoints || {};
        state.callStack = newState.call_stack || [];
        state.localVars = newState.local_vars || {};
        state.output = newState.output || [];

        // Update UI
        updateUI();

        // Handle state transitions
        if (state.debugState === 'paused') {
            // Highlight current line in editor
            highlightCurrentLine(state.file, state.line);
            // Auto-open file if not already open
            if (state.file && window.EditorManager && EditorManager.getCurrentFile() !== state.file) {
                if (window.FileManager) {
                    FileManager.openFile(state.file, null, (err, content) => {
                        if (!err && content !== undefined) {
                            // Jump to line after file opens
                            setTimeout(() => {
                                if (window.EditorManager) {
                                    EditorManager.goToLine(state.line);
                                }
                            }, 200);
                        }
                    });
                }
            } else if (window.EditorManager) {
                EditorManager.goToLine(state.line);
            }
        } else if (prevState === 'paused' && state.debugState === 'running') {
            clearCurrentLineHighlight();
        } else if (state.debugState === 'stopped') {
            clearCurrentLineHighlight();
            disconnectSSE();
        }

        // Update output panel
        renderOutput();
    }

    // ── UI Update ──
    function updateUI() {
        // Status indicator
        const statusEl = document.getElementById('debug-status-text');
        if (statusEl) {
            const labels = {
                'idle': '⏸ 就绪',
                'running': '▶ 运行中',
                'paused': '⏸ 已暂停',
                'stopped': '⏹ 已停止',
            };
            statusEl.textContent = labels[state.debugState] || state.debugState;
            statusEl.className = 'debug-status-' + state.debugState;
        }

        // Button states
        setBtnDisabled('debug-btn-start', state.debugState === 'running' || state.debugState === 'paused');
        setBtnDisabled('debug-btn-stop', state.debugState === 'idle' || state.debugState === 'stopped');
        setBtnDisabled('debug-btn-continue', state.debugState !== 'paused');
        setBtnDisabled('debug-btn-step-in', state.debugState !== 'paused');
        setBtnDisabled('debug-btn-step-over', state.debugState !== 'paused');
        setBtnDisabled('debug-btn-step-out', state.debugState !== 'paused');

        // Current position info
        const posEl = document.getElementById('debug-position');
        if (posEl) {
            if (state.file && state.line) {
                const fileName = state.file.split('/').pop();
                posEl.textContent = fileName + ':' + state.line + ' in ' + state.func + '()';
            } else {
                posEl.textContent = '';
            }
        }

        // Variables table
        renderVariables();

        // Call stack
        renderCallStack();

        // Breakpoints list
        renderBreakpointList();
    }

    function setBtnDisabled(id, disabled) {
        const btn = document.getElementById(id);
        if (btn) btn.disabled = disabled;
    }

    // ── Variables ──
    function renderVariables() {
        const container = document.getElementById('debug-variables');
        if (!container) return;
        container.innerHTML = '';

        const vars = state.localVars;
        if (!vars || Object.keys(vars).length === 0) {
            container.innerHTML = '<div class="debug-empty">无变量</div>';
            return;
        }

        for (const [name, value] of Object.entries(vars)) {
            const row = document.createElement('div');
            row.className = 'debug-var-row';
            row.innerHTML = '<span class="var-name">' + escapeHTML(name) + '</span>' +
                '<span class="var-value">' + escapeHTML(truncate(value, 200)) + '</span>';
            row.addEventListener('click', () => {
                // Click to copy variable value
                if (navigator.clipboard) {
                    navigator.clipboard.writeText(value);
                    if (window.showToast) window.showToast('已复制: ' + name, 'success', 1000);
                }
            });
            container.appendChild(row);
        }
    }

    // ── Call Stack ──
    function renderCallStack() {
        const container = document.getElementById('debug-callstack');
        if (!container) return;
        container.innerHTML = '';

        const stack = state.callStack;
        if (!stack || stack.length === 0) {
            container.innerHTML = '<div class="debug-empty">无调用栈</div>';
            return;
        }

        for (let i = stack.length - 1; i >= 0; i--) {
            const entry = stack[i];
            const row = document.createElement('div');
            row.className = 'debug-stack-row' + (i === stack.length - 1 ? ' current' : '');
            const fileName = (entry[0] || '').split('/').pop();
            row.innerHTML = '<span class="stack-func">' + escapeHTML(entry[2] || '?') + '</span>' +
                '<span class="stack-loc">' + escapeHTML(fileName) + ':' + (entry[1] || 0) + '</span>';
            row.addEventListener('click', () => {
                // Jump to file and line
                if (entry[0] && window.EditorManager) {
                    if (window.FileManager) {
                        FileManager.openFile(entry[0], null, (err) => {
                            if (!err) {
                                setTimeout(() => EditorManager.goToLine(entry[1]), 200);
                            }
                        });
                    }
                }
            });
            container.appendChild(row);
        }
    }

    // ── Breakpoint List ──
    function renderBreakpointList() {
        const container = document.getElementById('debug-breakpoint-list');
        if (!container) return;
        container.innerHTML = '';

        const bps = state.breakpoints || {};
        let count = 0;
        for (const [file, lines] of Object.entries(bps)) {
            if (!lines || lines.length === 0) continue;
            const fileName = file.split('/').pop();
            for (const line of lines) {
                count++;
                const row = document.createElement('div');
                row.className = 'debug-bp-row';
                row.innerHTML = '<span class="bp-file">' + escapeHTML(fileName) + '</span>' +
                    '<span class="bp-line">L' + line + '</span>' +
                    '<span class="bp-remove" title="删除断点">&#10005;</span>';
                row.querySelector('.bp-remove').addEventListener('click', (e) => {
                    e.stopPropagation();
                    removeBreakpoint(file, line);
                });
                row.addEventListener('click', () => {
                    if (window.FileManager) {
                        FileManager.openFile(file, null, (err) => {
                            if (!err) {
                                setTimeout(() => EditorManager.goToLine(line), 200);
                            }
                        });
                    }
                });
                container.appendChild(row);
            }
        }

        if (count === 0) {
            container.innerHTML = '<div class="debug-empty">无断点 (点击行号添加)</div>';
        }
    }

    // ── Output ──
    function renderOutput() {
        const container = document.getElementById('debug-output');
        if (!container) return;

        const output = state.output || [];
        container.innerHTML = output.map(line =>
            '<div class="debug-output-line">' + escapeHTML(line) + '</div>'
        ).join('');
        container.scrollTop = container.scrollHeight;
    }

    // ── AI Activity Log ──
    const aiActivities = [];

    function initAIActivityLog() {
        // Listen for debug AI tool events
    }

    function onAIActivity(e) {
        const detail = e.detail || {};
        aiActivities.push({
            time: new Date().toLocaleTimeString(),
            tool: detail.tool || '',
            args: detail.args || {},
            result: detail.result || '',
        });
        if (aiActivities.length > 100) aiActivities.splice(0, aiActivities.length - 100);
        renderAIActivity();
    }

    function renderAIActivity() {
        const container = document.getElementById('debug-ai-log');
        if (!container) return;
        container.innerHTML = '';

        for (let i = aiActivities.length - 1; i >= Math.max(0, aiActivities.length - 50); i--) {
            const act = aiActivities[i];
            const row = document.createElement('div');
            row.className = 'debug-ai-row';
            let summary = '';
            if (act.tool === 'debug_set_breakpoints') {
                summary = '设置断点: ' + (act.args.file_path || '').split('/').pop() + ' L' + (act.args.lines || []);
            } else if (act.tool === 'debug_start') {
                summary = '启动调试: ' + (act.args.file_path || '').split('/').pop();
            } else if (act.tool === 'debug_continue') {
                summary = '继续执行';
            } else if (act.tool === 'debug_step') {
                summary = '步进: ' + (act.args.action || 'step_in');
            } else if (act.tool === 'debug_inspect') {
                summary = '检查变量';
            } else if (act.tool === 'debug_evaluate') {
                summary = '求值: ' + (act.args.expression || '');
            } else if (act.tool === 'debug_stop') {
                summary = '停止调试';
            } else if (act.tool === 'debug_stack') {
                summary = '查看调用栈';
            } else if (act.tool === 'browser_navigate') {
                summary = '浏览页面: ' + ((act.args.url || '').substring(0, 50));
            } else if (act.tool === 'browser_console') {
                summary = '查看控制台';
            } else if (act.tool === 'browser_evaluate') {
                summary = '执行JS: ' + ((act.args.expression || '').substring(0, 40));
            } else if (act.tool === 'browser_inspect') {
                summary = '检查元素: ' + (act.args.selector || '');
            } else if (act.tool === 'browser_click') {
                summary = '点击: ' + (act.args.selector || '');
            } else if (act.tool === 'browser_input') {
                summary = '输入: ' + (act.args.selector || '');
            } else if (act.tool === 'browser_page_info') {
                summary = '页面信息';
            } else if (act.tool === 'browser_query_all') {
                summary = '查询元素: ' + (act.args.selector || '');
            } else if (act.tool === 'server_logs') {
                summary = '查看服务器日志';
            } else {
                summary = act.tool;
            }
            row.innerHTML = '<span class="ai-time">' + escapeHTML(act.time) + '</span>' +
                '<span class="ai-tool">' + escapeHTML(summary) + '</span>';
            container.appendChild(row);
        }
    }

    // ── Editor Integration ──

    /**
     * Toggle breakpoint on a line (called from editor gutter click)
     */
    function toggleBreakpoint(filePath, line) {
        if (!filePath) return;

        if (!state.breakpoints[filePath]) {
            state.breakpoints[filePath] = [];
        }

        const lines = state.breakpoints[filePath];
        const idx = lines.indexOf(line);
        if (idx >= 0) {
            lines.splice(idx, 1);
        } else {
            lines.push(line);
            lines.sort((a, b) => a - b);
        }

        // Send to server
        fetch('/api/debug/breakpoints', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                file_path: filePath,
                lines: lines,
                action: 'set'
            })
        }).catch(() => {});

        // Update editor gutter markers
        updateBreakpointGutter(filePath);
        renderBreakpointList();
    }

    /**
     * Add a breakpoint (from AI or manual)
     */
    function addBreakpoint(filePath, line) {
        if (!filePath || !line) return;
        if (!state.breakpoints[filePath]) {
            state.breakpoints[filePath] = [];
        }
        if (!state.breakpoints[filePath].includes(line)) {
            state.breakpoints[filePath].push(line);
            state.breakpoints[filePath].sort((a, b) => a - b);
        }

        fetch('/api/debug/breakpoints', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ file_path: filePath, lines: state.breakpoints[filePath] })
        }).catch(() => {});

        updateBreakpointGutter(filePath);
        renderBreakpointList();
    }

    function removeBreakpoint(filePath, line) {
        if (!state.breakpoints[filePath]) return;
        state.breakpoints[filePath] = state.breakpoints[filePath].filter(l => l !== line);

        fetch('/api/debug/breakpoints', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ file_path: filePath, lines: state.breakpoints[filePath] })
        }).catch(() => {});

        updateBreakpointGutter(filePath);
        renderBreakpointList();
    }

    function getBreakpointsForFile(filePath) {
        return state.breakpoints[filePath] || [];
    }

    function onBreakpointToggle(e) {
        const detail = e.detail || {};
        toggleBreakpoint(detail.file, detail.line);
    }

    function onFileOpened(e) {
        // Update gutter when a new file is opened
        const detail = e.detail || {};
        setTimeout(() => {
            updateBreakpointGutter(detail.path || detail.filePath);
        }, 100);
    }

    // ── Line Highlight ──

    function highlightCurrentLine(filePath, line) {
        if (!window.EditorManager) return;
        const editor = EditorManager.getEditor ? EditorManager.getEditor() : null;
        if (!editor) return;

        // Clear old marker
        clearCurrentLineHighlight();

        // Add new marker
        const lineHandle = editor.getLineHandle(line - 1);
        if (lineHandle) {
            currentLineMarker = editor.addLineClass(line - 1, 'background', 'debug-current-line');
        }
    }

    function clearCurrentLineHighlight() {
        if (!window.EditorManager) return;
        const editor = EditorManager.getEditor ? EditorManager.getEditor() : null;
        if (!editor || !currentLineMarker) return;

        try {
            editor.removeLineClass(currentLineMarker, 'background', 'debug-current-line');
        } catch (e) {}
        currentLineMarker = null;
    }

    // ── Breakpoint Gutter Markers ──

    function updateBreakpointGutter(filePath) {
        if (!window.EditorManager) return;
        const editor = EditorManager.getEditor ? EditorManager.getEditor() : null;
        if (!editor) return;

        // Get current file
        const currentFile = EditorManager.getCurrentFile();
        if (currentFile !== filePath) return;

        // Clear all breakpoint markers in the breakpoints gutter
        editor.clearGutter('breakpoints');

        // Add new markers for each breakpoint line
        const lines = getBreakpointsForFile(filePath);
        for (const line of lines) {
            const marker = document.createElement('div');
            marker.style.cssText = 'width:12px;height:12px;border-radius:50%;background:#ff5555;margin:2px auto;cursor:pointer;box-shadow:0 0 4px rgba(255,85,85,0.5);transition:transform 0.15s ease;';
            marker.addEventListener('mouseenter', () => { marker.style.transform = 'scale(1.3)'; });
            marker.addEventListener('mouseleave', () => { marker.style.transform = 'scale(1)'; });
            editor.setGutterMarker(line - 1, 'breakpoints', marker);
        }

        // Also remove old line-class background markers
        editor.getAllMarks().forEach(mark => {
            if (mark._isBreakpoint) {
                mark.clear();
            }
        });
    }

    // ── Open Debug Tab ──
    function openDebugTab() {
        // Show bottom panel
        const bottomPanel = document.getElementById('bottom-panel');
        if (bottomPanel) bottomPanel.classList.remove('hidden');

        // Switch to debug tab
        const debugTab = document.querySelector('#bottom-tabs .btab[data-btab="debug"]');
        if (debugTab) debugTab.click();

        // Resize editor
        if (window.EditorManager) {
            setTimeout(() => EditorManager.resize(), 100);
        }
    }

    // ── Helpers ──
    function escapeHTML(str) {
        if (!str) return '';
        const div = document.createElement('div');
        div.textContent = String(str);
        return div.innerHTML;
    }

    function truncate(str, maxLen) {
        if (!str) return '';
        return str.length > maxLen ? str.substring(0, maxLen) + '...' : str;
    }

    // ── Boot ──
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    return {
        init,
        startDebug,
        stopDebug,
        continueDebug,
        stepDebug,
        toggleBreakpoint,
        addBreakpoint,
        removeBreakpoint,
        getBreakpointsForFile,
        getState: () => state,
        openDebugTab,
    };
})();

window.DebuggerUI = DebuggerUI;
