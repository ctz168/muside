/**
 * DebugManager - 调试工具面板
 * 控制台、网络监视、错误日志、HTTP 客户端、进程管理
 */
const DebugManager = (() => {
    'use strict';

    // ── State ──
    const consoleLogs = [];
    const networkLogs = [];
    const errorLogs = [];
    let activeTab = 'output';
    let processRefreshTimer = null;

    // ── Init ──
    function init() {
        initBottomTabs();
        interceptConsole();
        interceptErrors();
        interceptNetwork();
        _ensureProcessTimer();
        // Always refresh process list immediately on init (page load).
        // This ensures running processes are shown even after a page refresh.
        refreshProcesses();

        // When tab becomes visible again, always refresh process list
        // (browser throttles/cancels setInterval in background tabs)
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden) {
                // Ensure the timer is alive (browser may have cancelled it)
                _ensureProcessTimer();
                // Always refresh immediately, regardless of which tab is active
                refreshProcesses();
            }
        });

        // Page Lifecycle 'resume' for mobile WebView
        document.addEventListener('resume', () => {
            _ensureProcessTimer();
            refreshProcesses();
        }, { passive: true });
    }

    /**
     * Ensure the process refresh timer is running.
     * Called on init and after visibility restore to handle timer cancellation.
     */
    function _ensureProcessTimer() {
        if (processRefreshTimer) clearInterval(processRefreshTimer);
        processRefreshTimer = setInterval(() => {
            if (activeTab === 'procs') refreshProcesses();
        }, 3000);
    }

    // ── Bottom Tab Switching ──
    function initBottomTabs() {
        const tabs = document.querySelectorAll('#bottom-tabs .btab');
        tabs.forEach(tab => {
            tab.addEventListener('click', () => {
                tabs.forEach(t => t.classList.remove('active'));
                tab.classList.add('active');
                const target = tab.dataset.btab;
                activeTab = target;
                // Deactivate all panels, activate selected
                document.querySelectorAll('.bpanel').forEach(p => {
                    p.classList.remove('active');
                });
                const panel = document.getElementById('bpanel-' + target);
                if (panel) panel.classList.add('active');
                // Special handling for browser/preview panel:
                // When switching AWAY from browser, aggressively hide the iframe
                // to prevent it from leaking through on some mobile WebViews
                const browserPanel = document.getElementById('bpanel-browser');
                const previewFrame = document.getElementById('preview-frame');
                if (browserPanel && previewFrame) {
                    if (target !== 'browser') {
                        // Extra safety: force hide iframe on top of CSS display:none
                        previewFrame.style.visibility = 'hidden';
                        previewFrame.style.height = '0';
                    } else {
                        previewFrame.style.visibility = '';
                        previewFrame.style.height = '';
                    }
                }
                if (target === 'console') renderConsole();
                if (target === 'network') renderNetwork();
                if (target === 'errors') renderErrors();
                if (target === 'procs') refreshProcesses();
            });
        });
    }

    // ── Console Capture ──
    function interceptConsole() {
        const origLog = console.log;
        const origWarn = console.warn;
        const origError = console.error;
        const origInfo = console.info;

        function addEntry(type, args) {
            const entry = {
                type,
                time: new Date().toLocaleTimeString(),
                text: Array.from(args).map(a => {
                    if (typeof a === 'object') {
                        try { return JSON.stringify(a, null, 2); } catch { return String(a); }
                    }
                    return String(a);
                }).join(' '),
                source: getCallerSource()
            };
            consoleLogs.push(entry);
            if (consoleLogs.length > 500) consoleLogs.splice(0, consoleLogs.length - 500);
            if (activeTab === 'console') renderConsole();
        }

        console.log = function() { addEntry('log', arguments); origLog.apply(console, arguments); };
        console.warn = function() { addEntry('warn', arguments); origWarn.apply(console, arguments); };
        console.error = function() { addEntry('error', arguments); origError.apply(console, arguments); };
        console.info = function() { addEntry('info', arguments); origInfo.apply(console, arguments); };
    }

    function getCallerSource() {
        try {
            const stack = new Error().stack;
            if (!stack) return '';
            const lines = stack.split('\n');
            for (let i = 3; i < lines.length; i++) {
                const line = lines[i].trim();
                if (line && !line.includes('debug.js') && !line.includes('interceptConsole')) {
                    const match = line.match(/(?:at\s+)?(?:.*?\s+\()?([^:)]+):(\d+)/);
                    if (match) {
                        const file = match[1].split('/').pop();
                        return file + ':' + match[2];
                    }
                    return line.substring(0, 60);
                }
            }
        } catch {}
        return '';
    }

    function renderConsole() {
        const container = document.getElementById('debug-console-content');
        if (!container) return;
        container.innerHTML = '';
        const start = Math.max(0, consoleLogs.length - 200);
        for (let i = start; i < consoleLogs.length; i++) {
            const entry = consoleLogs[i];
            const div = document.createElement('div');
            div.className = 'console-entry ' + entry.type;
            let html = '<span class="c-time">' + escapeHTML(entry.time) + '</span>' +
                '<span>' + escapeHTML(entry.text) + '</span>';
            if (entry.source) {
                html += '<span class="c-source">' + escapeHTML(entry.source) + '</span>';
            }
            div.innerHTML = html;
            container.appendChild(div);
        }
        container.scrollTop = container.scrollHeight;
    }

    // ── Error Capture ──
    function interceptErrors() {
        window.addEventListener('error', (e) => {
            const entry = {
                message: e.message || 'Unknown error',
                source: e.filename ? e.filename.split('/').pop() + ':' + e.lineno : '',
                time: new Date().toLocaleTimeString(),
                type: 'runtime'
            };
            errorLogs.push(entry);
            if (errorLogs.length > 200) errorLogs.splice(0, errorLogs.length - 200);
            if (activeTab === 'errors') renderErrors();
        });

        window.addEventListener('unhandledrejection', (e) => {
            const entry = {
                message: 'Unhandled Promise: ' + (e.reason ? (e.reason.message || String(e.reason)) : 'Unknown'),
                source: '',
                time: new Date().toLocaleTimeString(),
                type: 'promise'
            };
            errorLogs.push(entry);
            if (activeTab === 'errors') renderErrors();
        });
    }

    function addError(message, source) {
        const entry = { message, source: source || '', time: new Date().toLocaleTimeString(), type: 'custom' };
        errorLogs.push(entry);
        if (activeTab === 'errors') renderErrors();
    }

    function renderErrors() {
        const container = document.getElementById('debug-errors-content');
        if (!container) return;
        container.innerHTML = '';
        if (errorLogs.length === 0) {
            container.innerHTML = '<div style="color:var(--text-muted);padding:12px;text-align:center;">暂无错误记录</div>';
            return;
        }
        for (const entry of errorLogs) {
            const div = document.createElement('div');
            div.style.cssText = 'padding:6px 0;border-bottom:1px solid var(--border);';
            let html = '<div style="display:flex;align-items:center;gap:6px;">' +
                '<span style="color:var(--red);font-size:10px;font-weight:bold;">' + escapeHTML(entry.type.toUpperCase()) + '</span>' +
                '<span style="color:var(--text-muted);font-size:10px;">' + escapeHTML(entry.time) + '</span></div>' +
                '<div style="color:var(--text-primary);margin-top:2px;">' + escapeHTML(entry.message) + '</div>';
            if (entry.source) {
                html += '<div style="color:var(--text-muted);font-size:10px;margin-top:2px;">' + escapeHTML(entry.source) + '</div>';
            }
            div.innerHTML = html;
            container.appendChild(div);
        }
    }

    // ── Network Interceptor ──
    function interceptNetwork() {
        const origFetch = window.fetch;
        window.fetch = function(...args) {
            const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url ? args[0].url : '');
            const method = args[1] && args[1].method ? args[1].method : 'GET';
            const startTime = performance.now();

            return origFetch.apply(this, args).then(async resp => {
                const duration = Math.round(performance.now() - startTime);
                const entry = {
                    method,
                    url: url.split('?')[0],
                    fullUrl: url,
                    status: resp.status,
                    statusText: resp.statusText,
                    duration,
                    time: new Date().toLocaleTimeString(),
                    type: resp.ok ? 'ok' : 'error'
                };
                networkLogs.push(entry);
                if (networkLogs.length > 200) networkLogs.splice(0, networkLogs.length - 200);
                if (activeTab === 'network') renderNetwork();
                return resp;
            }).catch(err => {
                const duration = Math.round(performance.now() - startTime);
                networkLogs.push({
                    method, url: url.split('?')[0], fullUrl: url,
                    status: 0, statusText: 'Error', duration,
                    time: new Date().toLocaleTimeString(), type: 'error',
                    error: err.message
                });
                if (activeTab === 'network') renderNetwork();
                throw err;
            });
        };
    }

    function renderNetwork() {
        const container = document.getElementById('debug-network-content');
        if (!container) return;
        container.innerHTML = '';
        if (networkLogs.length === 0) {
            container.innerHTML = '<div style="color:var(--text-muted);padding:12px;text-align:center;">暂无网络请求</div>';
            return;
        }
        for (let i = networkLogs.length - 1; i >= 0; i--) {
            const entry = networkLogs[i];
            const div = document.createElement('div');
            div.style.cssText = 'padding:4px 0;border-bottom:1px solid var(--border);';
            const statusColor = entry.type === 'ok' ? 'var(--green)' : 'var(--red)';
            div.innerHTML = '<div style="display:flex;align-items:center;gap:6px;">' +
                '<span style="color:var(--mauve);font-weight:bold;font-size:10px;width:40px;">' + escapeHTML(entry.method) + '</span>' +
                '<span style="color:' + statusColor + ';font-size:10px;">' + entry.status + '</span>' +
                '<span style="color:var(--text-secondary);font-size:11px;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">' + escapeHTML(entry.url) + '</span>' +
                '<span style="color:var(--text-muted);font-size:10px;">' + entry.duration + 'ms</span></div>';
            container.appendChild(div);
        }
    }



    // ── Process Manager ──
    async function refreshProcesses() {
        const tbody = document.getElementById('process-list');
        if (!tbody) return;

        try {
            const origFetch = window.fetch;
            const resp = await origFetch.call(window, '/api/run/processes');
            if (!resp.ok) {
                tbody.innerHTML = '<tr><td colspan="4" style="color:var(--text-muted);">无法获取进程列表</td></tr>';
                return;
            }
            const data = await resp.json();
            const processes = data.processes || [];

            if (processes.length === 0) {
                tbody.innerHTML = '<tr><td colspan="4" style="color:var(--text-muted);">无运行中的进程</td></tr>';
                return;
            }

            tbody.innerHTML = '';
            for (const proc of processes) {
                const tr = document.createElement('tr');
                const isRunning = proc.running !== false;
                const cmd = proc.cmd || '-';
                const shortCmd = cmd.length > 50 ? cmd.substring(0, 50) + '...' : cmd;
                const exitInfo = !isRunning && proc.exit_code !== undefined && proc.exit_code !== null
                    ? ` (exit: ${proc.exit_code})` : '';
                const statusClass = isRunning ? 'proc-running' : 'proc-stopped';
                const statusText = isRunning ? '运行中' : ('已停止' + exitInfo);

                // Title shows full command on hover
                tr.title = cmd;
                tr.innerHTML =
                    '<td>' + escapeHTML(proc.id || '-') + '</td>' +
                    '<td class="' + statusClass + '">' + escapeHTML(statusText) + '</td>' +
                    '<td title="' + escapeHTML(cmd) + '" style="max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + escapeHTML(shortCmd) + '</td>' +
                    '<td>' + (proc.uptime || '-') + '</td>' +
                    '<td>' +
                        (isRunning
                            ? '<button onclick="DebugManager.killProcess(\'' + escapeHTML(proc.id) + '\')">终止</button>'
                            : '<button onclick="DebugManager.viewProcessOutput(\'' + escapeHTML(proc.id) + '\')">输出</button>') +
                    '</td>';
                tbody.appendChild(tr);
            }
        } catch {
            tbody.innerHTML = '<tr><td colspan="4" style="color:var(--text-muted);">加载失败</td></tr>';
        }
    }

    /**
     * View the output of a finished/running process in the terminal output panel.
     */
    async function viewProcessOutput(procId) {
        // Switch to the output tab
        const outputTab = document.querySelector('[data-btab="output"]');
        if (outputTab) outputTab.click();

        if (window.TerminalManager) {
            await window.TerminalManager.reconnectToProcess(procId);
        }
    }

    async function killProcess(procId) {
        try {
            await fetch('/api/run/stop', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ proc_id: procId })
            });
            if (window.TerminalManager && window.TerminalManager.currentProcId === procId) {
                window.TerminalManager.cleanupProcess && window.TerminalManager.cleanupProcess();
            }
            setTimeout(refreshProcesses, 500);
        } catch {}
    }

    // ── Helpers ──
    function escapeHTML(str) {
        const div = document.createElement('div');
        div.textContent = str || '';
        return div.innerHTML;
    }

    // ── Boot ──
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    return {
        init,
        addError,
        killProcess,
        viewProcessOutput,
        refreshProcesses,
        get activeTab() { return activeTab; }
    };
})();

window.DebugManager = DebugManager;
