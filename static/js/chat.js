/**
 * ChatManager - LLM chat interface with SSE streaming and agent tool execution for MusIDE
 * Works with Flask backend on port 12346
 */
const ChatManager = (() => {
    'use strict';

    // ── State ──────────────────────────────────────────────────────
    let isProcessing = false;
    let messages = [];                // local cache of chat history
    let lastUserMessage = null;       // for re-send
    let settingsDialogEl = null;      // cached settings dialog
    let currentAbortController = null; // for aborting SSE streams
    let currentStreamEl = null;       // current streaming message element
    let streamBuffer = '';            // buffer for accumulating streamed text
    let reasoningEl = null;           // reasoning/thinking collapsible element
    let reasoningBuffer = '';         // buffer for accumulating reasoning text
    let reasoningVisible = false;     // whether reasoning section is visible
    let autoScrollEnabled = true;     // auto-scroll state
    let turnIndicator = null;         // turn X/Y element reference
    let iterationCount = 0;           // agent iteration counter
    let streamingStartTime = null;    // for execution time tracking
    let chatMode = localStorage.getItem('muside_chat_mode') || 'execute';
    let planContent = '';               // stored plan markdown for editing
    let lastPlanMsgEl = null;           // reference to plan message element for actions
    let currentConvId = null;           // current conversation id (null = unsaved new chat)
    let isReconnecting = false;         // true when reconnecting to a running task
    let taskStatusInterval = null;      // interval for polling task status
    let taskActivityBadge = null;       // badge element on #btn-chat
    let sseConnectionAlive = false;     // whether the SSE connection is currently alive
    let sseConnectionLostWhileHidden = false; // set to true if SSE dies while page is hidden

    // ── Pending Message Queue ──────────────────────────────────────
    // When user sends a message while AI is processing, it gets queued.
    // After current task completes, queued messages are auto-sent.
    // Queue is persisted in localStorage for crash/recovery.
    const PENDING_QUEUE_KEY = 'muside_pending_messages';
    let pendingMessages = [];  // array of {text, convId, timestamp}

    // ── Chat Search State ──────────────────────────────────────────
    let searchMatches = [];             // array of {el, mark} for each match
    let searchCurrentIndex = -1;        // current highlighted match index
    let searchVisible = false;          // whether search bar is shown
    let _originalTextNodes = [];        // text nodes saved before highlighting (for cleanup)

    // ── Constants ──────────────────────────────────────────────────
    const MSG_BACKUP_KEY = 'muside_chat_backup'; // localStorage key for crash recovery
    const MSG_BACKUP_INTERVAL = 3000; // save backup every 3s during streaming
    let _backupTimer = null;
    const KNOWN_TOOLS = [
        'read_file', 'write_file', 'edit_file', 'search_files',
        'list_directory', 'git_status', 'git_diff', 'run_command', 'install_package',
        'web_search', 'web_fetch', 'git_commit', 'git_log', 'git_checkout',
        'create_directory', 'delete_path', 'file_info', 'grep_code', 'list_packages',
        'browser_navigate', 'browser_console', 'browser_page_info',
        'browser_evaluate', 'browser_inspect', 'browser_query_all',
        'browser_click', 'browser_input', 'browser_cookies', 'server_logs',
        'glob_files', 'find_definition', 'find_references', 'file_structure',
        'delegate_task', 'parallel_tasks', 'todo_write', 'todo_read',
        'move_file', 'append_file', 'run_linter', 'run_tests', 'kill_port',
        // Audio / Music Production tools
        'play_audio', 'stop_audio', 'pause_audio', 'seek_audio',
        'load_audio', 'edit_audio', 'export_audio', 'record_audio',
        'list_tracks', 'add_track', 'remove_track',
        'set_track_volume', 'set_track_pan', 'set_track_mute', 'set_track_solo',
        'set_bpm', 'set_time_signature', 'get_project_info',
        // Lyrics & Vocal tools
        'edit_lyrics', 'synthesize_vocals',
        // Timbre tools
        'set_timbre', 'get_timbre',
        // Beat Control tools
        'set_swing', 'quantize', 'set_humanize',
    ];

    const TOOL_ICONS = {
        read_file:     '📖',
        write_file:    '✏️',
        edit_file:     '✏️',
        execute_code:  '▶️',
        search_files:  '🔍',
        list_files:    '📁',
        create_directory: '📁',
        delete_path:   '🗑️',
        git_status:    '🔀',
        git_diff:      '📝',
        git_commit:    '📝',
        git_log:       '📋',
        git_checkout:  '🔀',
        terminal:      '💻',
        install_package: '📦',
        list_packages: '📦',
        file_info:     'ℹ️',
        grep_code:     '🔎',
        web_search:    '🌐',
        web_fetch:     '📄',
        browser_navigate:    '🌐',
        browser_console:     '📋',
        browser_page_info:   'ℹ️',
        browser_evaluate:    '⚡',
        browser_inspect:     '🔍',
        browser_query_all:   '🔎',
        browser_click:       '👆',
        browser_input:       '⌨️',
        browser_cookies:     '🍪',
        server_logs:         '📋',
        // P0+P1 new tools
        glob_files:          '📂',
        find_definition:     '🎯',
        find_references:     '🔗',
        file_structure:      '🏗️',
        delegate_task:       '🤖',
        parallel_tasks:      '🔄',
        todo_write:          '📋',
        todo_read:           '📋',
        // Quality Assurance tools
        run_linter:          '🔍',
        run_tests:           '🧪',
        // Process & Port Management
        kill_port:           '🛑',
        // Audio / Music Production tools
        play_audio:          '▶️',
        stop_audio:          '⏹️',
        pause_audio:         '⏸️',
        seek_audio:          '⏩',
        load_audio:          '📂',
        edit_audio:          '✂️',
        export_audio:        '💾',
        record_audio:        '⏺️',
        list_tracks:         '🎵',
        add_track:           '➕',
        remove_track:        '🗑️',
        set_track_volume:    '🔊',
        set_track_pan:       '↔️',
        set_track_mute:      '🔇',
        set_track_solo:      '🎯',
        set_bpm:             '🥁',
        set_time_signature:  '🎼',
        get_project_info:    'ℹ️',
        // Lyrics & Vocal tools
        edit_lyrics:         '📝',
        synthesize_vocals:   '🎤',
        // Timbre tools
        set_timbre:          '🎛',
        get_timbre:          '🔊',
        // Beat Control tools
        set_swing:           '💃',
        quantize:            '📏',
        set_humanize:        '🤲',
    };

    const COLLAPSE_THRESHOLD = 500; // chars before showing "Show more"

    // ==================== Todo Panel ====================
    let todoPanelEl = null;
    let currentTodoData = [];
    let todoPanelCollapsed = false;

    // Global toggle function for the collapse/expand button
    window._toggleTodoPanel = function() {
        const body = document.getElementById('ai-todo-panel-body');
        const btn = document.getElementById('todo-toggle-btn');
        if (!body) return;
        todoPanelCollapsed = !todoPanelCollapsed;
        if (todoPanelCollapsed) {
            body.classList.add('collapsed');
            if (btn) btn.textContent = '▸';
        } else {
            body.classList.remove('collapsed');
            if (btn) btn.textContent = '▾';
        }
    };

    function createOrUpdateTodoPanel(todos) {
        if (Array.isArray(todos)) {
            currentTodoData = todos;
        }
        if (!todoPanelEl) {
            todoPanelEl = document.createElement('div');
            todoPanelEl.className = 'todo-panel';
            todoPanelEl.id = 'ai-todo-panel';
            const chatContainer = document.getElementById('chat-messages');
            if (chatContainer) {
                chatContainer.parentElement.insertBefore(todoPanelEl, chatContainer);
            }
        }
        if (currentTodoData.length === 0) {
            todoPanelEl.style.display = 'none';
            return;
        }
        todoPanelEl.style.display = 'block';
        const completed = currentTodoData.filter(t => t.status === 'completed').length;
        const total = currentTodoData.length;
        const pct = total > 0 ? Math.round((completed / total) * 100) : 0;
        let html = '<div class="todo-panel-header" id="todo-panel-header">';
        html += '<div class="todo-panel-header-row">';
        html += '<span class="todo-panel-title">📋 Task Plan</span>';
        html += '<button class="todo-toggle-btn" id="todo-toggle-btn">' + (todoPanelCollapsed ? '▸' : '▾') + '</button>';
        html += '</div>';
        html += '<span class="todo-panel-progress">' + completed + '/' + total + ' (' + pct + '%)</span>';
        html += '<div class="todo-panel-bar"><div class="todo-panel-bar-fill" style="width:' + pct + '%"></div></div>';
        html += '</div>';
        html += '<div id="ai-todo-panel-body" class="todo-panel-body' + (todoPanelCollapsed ? ' collapsed' : '') + '">';
        currentTodoData.forEach(function(t) {
            const isCompleted = t.status === 'completed';
            const isInProgress = t.status === 'in_progress';
            let statusClass = 'todo-pending';
            if (isCompleted) statusClass = 'todo-completed';
            else if (isInProgress) statusClass = 'todo-inprogress';
            const icon = isCompleted ? '✅' : (isInProgress ? '🔄' : '⬜');
            const pri = t.priority === 'high' ? '🔴' : (t.priority === 'medium' ? '🟡' : (t.priority === 'low' ? '🟢' : ''));
            html += '<div class="todo-item ' + statusClass + '">' + icon + ' ' + pri + ' <span>' + escapeHTML(t.content) + '</span></div>';
        });
        html += '</div>';
        todoPanelEl.innerHTML = html;

        // Attach event listeners after innerHTML update (more reliable than inline onclick on mobile)
        const header = document.getElementById('todo-panel-header');
        if (header) {
            header.addEventListener('click', function(e) {
                // Don't toggle if clicking on progress bar or other non-header-row elements
                if (e.target.closest('.todo-panel-header-row')) {
                    window._toggleTodoPanel();
                }
            });
        }
        // Also attach directly to the toggle button as fallback
        const toggleBtn = document.getElementById('todo-toggle-btn');
        if (toggleBtn) {
            toggleBtn.addEventListener('click', function(e) {
                e.stopPropagation();
                window._toggleTodoPanel();
            });
        }
    }

    function hideTodoPanel() {
        currentTodoData = [];
        todoPanelCollapsed = false;
        if (todoPanelEl) {
            todoPanelEl.style.display = 'none';
        }
    }

    // Plan mode system prompt
    const PLAN_MODE_PROMPT = `[PLAN MODE] Please analyze the request and create a detailed execution plan in Markdown format. Include:
1. **Analysis** - What needs to be done
2. **Files to modify** - List specific files
3. **Step-by-step approach** - Detailed steps
4. **Expected outcome** - What the result should look like

Do NOT execute any tools. Only generate the plan.\n\nUser request: `;

    // ── Chat Message Backup (crash/refresh recovery) ───────────
    // During streaming, messages are only saved to backend after completion.
    // We periodically save to localStorage so nothing is lost on refresh/close.

    function backupMessages() {
        if (messages.length === 0) return;
        try {
            localStorage.setItem(MSG_BACKUP_KEY, JSON.stringify({
                messages: messages,
                convId: currentConvId,
                timestamp: Date.now()
            }));
        } catch (e) { /* storage full — ignore */ }
    }

    function restoreBackup() {
        try {
            const raw = localStorage.getItem(MSG_BACKUP_KEY);
            if (!raw) return null;
            const data = JSON.parse(raw);
            // Only restore if backup is less than 24 hours old
            if (data.timestamp && Date.now() - data.timestamp > 24 * 60 * 60 * 1000) {
                localStorage.removeItem(MSG_BACKUP_KEY);
                return null;
            }
            return data;
        } catch (e) {
            localStorage.removeItem(MSG_BACKUP_KEY);
            return null;
        }
    }

    function clearBackup() {
        localStorage.removeItem(MSG_BACKUP_KEY);
        stopBackupTimer();
    }

    function startBackupTimer() {
        stopBackupTimer();
        _backupTimer = setInterval(backupMessages, MSG_BACKUP_INTERVAL);
    }

    function stopBackupTimer() {
        if (_backupTimer) {
            clearInterval(_backupTimer);
            _backupTimer = null;
        }
    }

    // ── Helpers ────────────────────────────────────────────────────

    function escapeHTML(str) {
        const div = document.createElement('div');
        div.textContent = str || '';
        return div.innerHTML;
    }

    function escapeAttr(str) {
        return (str || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;')
                          .replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    function formatTime(time) {
        let d;
        if (time instanceof Date) {
            d = time;
        } else if (typeof time === 'number') {
            d = new Date(time);
        } else if (typeof time === 'string') {
            d = new Date(time);
            if (isNaN(d.getTime())) d = new Date();
        } else {
            d = new Date();
        }
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    /**
     * Play a completion notification sound using Web Audio API
     */
    function playCompletionSound() {
        try {
            const audioContext = new (window.AudioContext || window.webkitAudioContext)();
            const oscillator = audioContext.createOscillator();
            const gainNode = audioContext.createGain();

            oscillator.connect(gainNode);
            gainNode.connect(audioContext.destination);

            // Pleasant "ding" sound - two tone
            oscillator.frequency.setValueAtTime(523.25, audioContext.currentTime); // C5
            oscillator.frequency.setValueAtTime(659.25, audioContext.currentTime + 0.1); // E5
            oscillator.frequency.setValueAtTime(783.99, audioContext.currentTime + 0.2); // G5

            gainNode.gain.setValueAtTime(0.3, audioContext.currentTime);
            gainNode.gain.linearRampToValueAtTime(0.001, audioContext.currentTime + 0.4);

            oscillator.start(audioContext.currentTime);
            oscillator.stop(audioContext.currentTime + 0.4);
        } catch (e) {
            console.warn('Failed to play completion sound:', e);
        }
    }

    function truncate(str, maxLen) {
        if (!str) return '';
        if (str.length <= maxLen) return str;
        return str.substring(0, maxLen) + '...';
    }

    function msgId() {
        return 'chat-msg-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
    }

    /**
     * Estimate token count from text (rough heuristic: ~4 chars per token)
     */
    function estimateTokens(text) {
        if (!text) return 0;
        return Math.ceil(text.length / 4);
    }

    // ── Context Progress Ring ───────────────────────────────────────

    /**
     * Calculate current context token usage and update the ring indicator.
     * The ring shows: thin white outline at 0% → thick black fill at 100%.
     * Over 80% turns red as a warning.
     */
    function updateContextRing() {
        const ring = document.getElementById('ctx-progress-ring');
        if (!ring) return;
        const fillEl = ring.querySelector('.ctx-ring-fill');
        if (!fillEl) return;

        // Get max_context from current selected model config
        let maxCtx = 128000; // default
        const modelSelect = document.getElementById('chat-model-select');
        if (modelSelect && modelSelect._modelConfigs) {
            const idx = parseInt(modelSelect.value);
            if (!isNaN(idx) && modelSelect._modelConfigs[idx]) {
                maxCtx = modelSelect._modelConfigs[idx].max_context || 128000;
            }
        }

        // Estimate total tokens from all messages
        let totalTokens = 0;
        for (const msg of messages) {
            totalTokens += estimateTokens(msg.content || '');
        }

        const pct = Math.min(totalTokens / maxCtx, 1);
        const circumference = 97.4; // 2 * PI * 15.5
        const offset = circumference * (1 - pct);

        fillEl.style.strokeDashoffset = offset;

        // Color: white at low, darken toward black at high, red over 80%
        if (pct >= 0.8) {
            fillEl.style.stroke = pct >= 1 ? '#ff4444' : '#ff6644';
            fillEl.style.strokeWidth = '4';
        } else {
            // Interpolate from thin white to thick dark
            const gray = Math.round(255 - pct * 255);
            fillEl.style.stroke = `rgb(${gray},${gray},${gray})`;
            fillEl.style.strokeWidth = pct > 0.05 ? (3 + pct * 2) + 'px' : '3px';
        }

        // Update tooltip
        ring.title = `上下文: ~${totalTokens} / ${maxCtx} tokens (${Math.round(pct * 100)}%)`;

        // Show percentage text inside ring
        let pctLabel = ring.querySelector('.ctx-pct');
        if (!pctLabel) {
            pctLabel = document.createElement('span');
            pctLabel.className = 'ctx-pct';
            ring.appendChild(pctLabel);
        }
        pctLabel.textContent = Math.round(pct * 100) + '%';
    }

    // ── Markdown-Lite Rendering ────────────────────────────────────

    /**
     * Render markdown-lite formatting with extended support for:
     *  - Code blocks: ```lang\n...\n```
     *  - Inline code: `...`
     *  - Bold: **...** or __...__
     *  - Italic: *...* or _..._
     *  - Links: [text](url) → clickable anchor tags
     *  - Headings: # heading, ## heading, ### heading
     *  - Horizontal rules: --- or ***
     *  - Blockquotes: > text
     *  - Unordered lists: - item or * item
     *  - Ordered lists: 1. item
     *  - Line breaks: double newline
     *
     * Returns HTML string. Input is raw text (will be HTML-escaped internally).
     */
    function renderMarkdownLite(text) {
        if (!text) return '';

        let html = escapeHTML(text);

        // Extract and protect fenced code blocks first
        const codeBlocks = [];
        html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
            const idx = codeBlocks.length;
            codeBlocks.push({ lang: lang || '', code: code.replace(/\n$/, '') });
            return `\x00CODEBLOCK_${idx}\x00`;
        });

        // Handle ```...``` without explicit newlines (inline code blocks)
        html = html.replace(/```([\s\S]*?)```/g, (_, code) => {
            const idx = codeBlocks.length;
            codeBlocks.push({ lang: '', code: code.replace(/^\n/, '').replace(/\n$/, '') });
            return `\x00CODEBLOCK_${idx}\x00`;
        });

        // Inline code: `...`
        const inlineCodes = [];
        html = html.replace(/`([^`\n]+)`/g, (_, code) => {
            const idx = inlineCodes.length;
            inlineCodes.push(code);
            return `\x00INLINE_${idx}\x00`;
        });

        // Links: [text](url) — must be done before bold/italic
        html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');

        // Headings: ### heading, ## heading, # heading (at start of line)
        html = html.replace(/^### (.+)$/gm, '<strong class="md-h3">$1</strong>');
        html = html.replace(/^## (.+)$/gm, '<strong class="md-h2">$1</strong>');
        html = html.replace(/^# (.+)$/gm, '<strong class="md-h1">$1</strong>');

        // Horizontal rules: --- or *** (on their own line)
        html = html.replace(/^[-*]{3,}$/gm, '<hr>');

        // Blockquotes: > text
        html = html.replace(/^&gt;\s?(.+)$/gm, '<blockquote>$1</blockquote>');
        // Merge consecutive blockquotes
        html = html.replace(/<\/blockquote>\n<blockquote>/g, '\n');

        // Bold: **...** or __...__
        html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
        html = html.replace(/__(.+?)__/g, '<strong>$1</strong>');

        // Italic: *...* (but not inside already processed tags)
        html = html.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, '<em>$1</em>');

        // Restore inline code
        html = html.replace(/\x00INLINE_(\d+)\x00/g, (_, idx) => {
            const code = inlineCodes[parseInt(idx, 10)];
            return `<code>${code}</code>`;
        });

        // Restore code blocks with copy button
        html = html.replace(/\x00CODEBLOCK_(\d+)\x00/g, (_, idx) => {
            const block = codeBlocks[parseInt(idx, 10)];
            const escapedCode = block.code.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            const copyBtn = `<button class="code-copy-btn" data-code="${escapeAttr(block.code)}" title="Copy">📋</button>`;
            const langLabel = block.lang ? `<span class="code-lang">${escapeHTML(block.lang)}</span>` : '';
            return `<div class="code-block-wrapper">${langLabel}${copyBtn}<pre><code>${escapedCode}</code></pre></div>`;
        });

        // Unordered lists: lines starting with - or * followed by space
        html = html.replace(/^(\s*)([-*])\s+(.+)$/gm, '$1<li>$3</li>');
        html = html.replace(/((?:<li>.*<\/li>\n?)+)/g, '<ul>$1</ul>');

        // Ordered lists: lines starting with \d+.
        html = html.replace(/^(\s*)(\d+)\.\s+(.+)$/gm, '$1<li>$3</li>');

        // Paragraphs: double newlines
        html = html.replace(/\n{2,}/g, '</p><p>');

        // Single newlines -> <br>
        html = html.replace(/\n/g, '<br>');

        // Wrap in paragraphs if not already wrapped
        if (!html.startsWith('<')) {
            html = '<p>' + html + '</p>';
        }

        return html;
    }

    // ── Code Copy Handler ──────────────────────────────────────────

    function bindCopyButtons(container) {
        const btns = (container || document).querySelectorAll('.code-copy-btn');
        btns.forEach(btn => {
            if (btn._bound) return;
            btn._bound = true;
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const code = btn.dataset.code || '';
                navigator.clipboard.writeText(code).then(() => {
                    const original = btn.textContent;
                    btn.textContent = '✅';
                    setTimeout(() => { btn.textContent = original; }, 1500);
                }).catch(() => {
                    const ta = document.createElement('textarea');
                    ta.value = code;
                    ta.style.position = 'fixed';
                    ta.style.opacity = '0';
                    document.body.appendChild(ta);
                    ta.select();
                    try { document.execCommand('copy'); } catch (_) {}
                    document.body.removeChild(ta);
                    const original = btn.textContent;
                    btn.textContent = '✅';
                    setTimeout(() => { btn.textContent = original; }, 1500);
                });
            });
        });
    }

    /**
     * Create a copy button that copies plain text from a message element
     * @param {string} text - The plain text to copy
     * @returns {HTMLElement}
     */
    function createMsgCopyBtn(text) {
        const btn = document.createElement('button');
        btn.className = 'msg-copy-btn';
        btn.textContent = '📋 复制';
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const copyText = typeof text === 'string' ? text : '';
            navigator.clipboard.writeText(copyText).then(() => {
                btn.textContent = '✅ 已复制';
                setTimeout(() => { btn.textContent = '📋 复制'; }, 1500);
            }).catch(() => {
                const ta = document.createElement('textarea');
                ta.value = copyText;
                ta.style.position = 'fixed';
                ta.style.opacity = '0';
                document.body.appendChild(ta);
                ta.select();
                try { document.execCommand('copy'); } catch (_) {}
                document.body.removeChild(ta);
                btn.textContent = '✅ 已复制';
                setTimeout(() => { btn.textContent = '📋 复制'; }, 1500);
            });
        });
        return btn;
    }

    // ── Auto-Scroll with User Detection ────────────────────────────

    function initAutoScroll() {
        const container = document.getElementById('chat-messages');
        if (!container || container._autoScrollInit) return;
        container._autoScrollInit = true;

        container.addEventListener('scroll', () => {
            const threshold = 80;
            const atBottom = container.scrollHeight - container.scrollTop - container.clientHeight < threshold;
            autoScrollEnabled = atBottom;
        });
    }

    function scrollToBottom() {
        if (!autoScrollEnabled) return;
        const container = document.getElementById('chat-messages');
        if (container) {
            container.scrollTop = container.scrollHeight;
        }
    }

    function forceScrollToBottom() {
        autoScrollEnabled = true;
        const container = document.getElementById('chat-messages');
        if (container) {
            container.scrollTop = container.scrollHeight;
        }
    }

    // ── Message Rendering ──────────────────────────────────────────

    function createMessageEl(role, content, extra) {
        extra = extra || {};

        const div = document.createElement('div');
        div.className = 'chat-msg';
        div.id = extra.id || msgId();

        if (role === 'user') div.classList.add('user');
        else if (role === 'assistant') div.classList.add('assistant');
        else if (role === 'tool') div.classList.add('tool');
        else if (role === 'error') div.classList.add('error');
        else if (role === 'system') div.classList.add('system');

        // Role badge for assistant
        if (role === 'assistant') {
            const badge = document.createElement('div');
            badge.className = 'chat-role-badge';
            badge.textContent = '🤖 Assistant';
            div.appendChild(badge);
        }

        // Tool execution details
        if (role === 'tool') {
            const toolName = extra.tool || extra.name || 'unknown';
            const icon = TOOL_ICONS[toolName] || '🔧';
            const argsStr = extra.args ? formatToolArgs(extra.args) : '';
            const ok = extra.ok !== false;

            const header = document.createElement('div');
            header.className = 'tool-header';
            header.innerHTML = `<span class="tool-name">${icon} ${escapeHTML(toolName)}</span>`
                + (ok ? '<span class="tool-status tool-ok">✓</span>' : '<span class="tool-status tool-fail">✗</span>');
            div.appendChild(header);

            if (extra.duration) {
                const durEl = document.createElement('span');
                durEl.className = 'tool-duration';
                durEl.textContent = formatDuration(extra.duration);
                header.appendChild(durEl);
            }

            if (argsStr) {
                const argsEl = document.createElement('div');
                argsEl.className = 'tool-args';
                argsEl.textContent = argsStr;
                if (argsStr.length > 120) {
                    argsEl.classList.add('collapsible', 'collapsed');
                    argsEl.addEventListener('click', () => {
                        argsEl.classList.toggle('collapsed');
                        argsEl.classList.toggle('expanded');
                    });
                }
                div.appendChild(argsEl);
            }

            if (content) {
                const resultEl = document.createElement('div');
                resultEl.className = 'tool-result';
                const resultStr = typeof content === 'string' ? content : JSON.stringify(content, null, 2);

                if (resultStr.length > COLLAPSE_THRESHOLD) {
                    const shortContent = resultStr.substring(0, COLLAPSE_THRESHOLD);
                    const fullContent = resultStr;

                    const shortSpan = document.createElement('div');
                    shortSpan.className = 'tool-result-short';
                    shortSpan.textContent = shortContent + '...';

                    const fullSpan = document.createElement('div');
                    fullSpan.className = 'tool-result-full';
                    fullSpan.style.display = 'none';
                    fullSpan.textContent = fullContent;

                    const toggleBtn = document.createElement('button');
                    toggleBtn.className = 'tool-toggle-btn';
                    toggleBtn.textContent = 'Show more';
                    toggleBtn.addEventListener('click', () => {
                        const expanded = fullSpan.style.display !== 'none';
                        fullSpan.style.display = expanded ? 'none' : '';
                        toggleBtn.textContent = expanded ? 'Show more' : 'Show less';
                    });

                    resultEl.appendChild(shortSpan);
                    resultEl.appendChild(fullSpan);
                    resultEl.appendChild(toggleBtn);
                } else {
                    resultEl.textContent = resultStr;
                }
                div.appendChild(resultEl);
            }

            // Timestamp
            if (extra.time) {
                const timeEl = document.createElement('div');
                timeEl.className = 'chat-time';
                timeEl.textContent = formatTime(extra.time);
                div.appendChild(timeEl);
            }

            // Copy button for tool result
            if (content) {
                const resultStr = typeof content === 'string' ? content : JSON.stringify(content, null, 2);
                if (resultStr.trim()) {
                    div.appendChild(createMsgCopyBtn(resultStr));
                }
            }

            return div;
        }

        // Error messages
        if (role === 'error') {
            const icon = document.createElement('span');
            icon.textContent = '⚠️ ';
            div.appendChild(icon);
            const textEl = document.createElement('span');
            textEl.innerHTML = renderMarkdownLite(content);
            div.appendChild(textEl);

            // Retry button
            if (extra.retryable) {
                const retryBtn = document.createElement('button');
                retryBtn.className = 'chat-retry-btn';
                retryBtn.textContent = '🔄 重试';
                retryBtn.addEventListener('click', () => {
                    retryFromError();
                });
                div.appendChild(retryBtn);
            }
        } else if (role === 'system') {
            // System / status messages (thinking, done, etc.)
            const textEl = document.createElement('div');
            textEl.className = 'chat-system-msg';
            textEl.innerHTML = renderMarkdownLite(content);
            div.appendChild(textEl);
        } else {
            // Regular text content with markdown-lite
            const textEl = document.createElement('div');
            textEl.className = 'chat-content';
            textEl.innerHTML = renderMarkdownLite(content);
            div.appendChild(textEl);
        }

        // Timestamp
        const timeStr = extra.time || (Date.now());
        const timeEl = document.createElement('div');
        timeEl.className = 'chat-time';
        timeEl.textContent = formatTime(timeStr);
        div.appendChild(timeEl);

        // Message-level copy button for user and assistant messages
        if ((role === 'user' || role === 'assistant') && content) {
            const plainText = content.replace(/<[^>]*>/g, ''); // strip HTML tags
            if (plainText.trim()) {
                div.appendChild(createMsgCopyBtn(plainText));
            }
        }

        // Bind copy buttons inside this message
        bindCopyButtons(div);

        return div;
    }

    function formatToolArgs(args) {
        if (!args) return '';
        if (typeof args === 'string') {
            try { args = JSON.parse(args); } catch (_) { return truncate(args, 200); }
        }
        if (typeof args !== 'object') return String(args);
        const parts = [];
        for (const [key, val] of Object.entries(args)) {
            const valStr = typeof val === 'string' ? truncate(val, 80) : JSON.stringify(val);
            parts.push(`${key}: ${valStr}`);
        }
        return parts.join(', ');
    }

    function formatDuration(ms) {
        if (!ms) return '';
        if (ms < 1000) return ms + 'ms';
        return (ms / 1000).toFixed(1) + 's';
    }

    function renderMessages(msgs) {
        const container = document.getElementById('chat-messages');
        if (!container) return;

        container.innerHTML = '';
        messages = Array.isArray(msgs) ? msgs : [];

        if (messages.length === 0) {
            container.innerHTML = '<div class="empty-state"><div class="emoji">🤖</div><div>Ask me anything about your code</div></div>';
            return;
        }

        for (const msg of messages) {
            const el = createMessageEl(msg.role, msg.content, msg);
            container.appendChild(el);
        }

        bindCopyButtons(container);
        forceScrollToBottom();
        updateContextRing();
    }

    function addMessage(role, content, extra) {
        const container = document.getElementById('chat-messages');
        if (!container) return;

        const emptyState = container.querySelector('.empty-state');
        if (emptyState) emptyState.remove();

        extra = extra || {};
        extra.time = extra.time || new Date();

        const el = createMessageEl(role, content, extra);
        container.appendChild(el);

        messages.push({ role, content, time: extra.time, ...extra });

        bindCopyButtons(container);
        forceScrollToBottom();
        updateContextRing();

        return el;
    }

    // ── Typing / Status Indicators ─────────────────────────────────

    function showTyping() {
        const container = document.getElementById('chat-messages');
        if (!container) return null;
        if (container.querySelector('.chat-typing')) return null;

        const indicator = document.createElement('div');
        indicator.className = 'chat-typing';
        indicator.textContent = 'Thinking';
        container.appendChild(indicator);
        forceScrollToBottom();
        return indicator;
    }

    function hideTyping() {
        const container = document.getElementById('chat-messages');
        if (!container) return;
        const indicator = container.querySelector('.chat-typing');
        if (indicator) indicator.remove();
    }

    /**
     * Create or update the turn indicator (Turn X/Y)
     */
    function updateTurnIndicator(current, total) {
        const container = document.getElementById('chat-messages');
        if (!container) return;

        if (!turnIndicator) {
            turnIndicator = document.createElement('div');
            turnIndicator.className = 'chat-turn-indicator';
            turnIndicator.id = 'chat-turn-indicator';
        }

        turnIndicator.textContent = total > 0
            ? `Turn ${current}/${total}`
            : `Turn ${current}`;
        turnIndicator.style.display = '';

        // Insert at the top of the messages container (after empty state if any)
        const existing = container.querySelector('#chat-turn-indicator');
        if (!existing) {
            const first = container.firstChild;
            container.insertBefore(turnIndicator, first);
        }
    }

    function hideTurnIndicator() {
        if (turnIndicator) {
            turnIndicator.style.display = 'none';
        }
    }

    // ── Tool Progress Visualization ────────────────────────────────

    /**
     * Show a tool execution in progress with spinning indicator
     * @returns {HTMLElement} the tool element for later updating
     */
    function showToolProgress(toolName, args) {
        const container = document.getElementById('chat-messages');
        if (!container) return null;

        const el = document.createElement('div');
        el.className = 'chat-msg tool tool-progress';
        el.id = msgId();

        const icon = TOOL_ICONS[toolName] || '🔧';
        const argsStr = args ? formatToolArgs(args) : '';

        el.innerHTML = `
            <div class="tool-header">
                <span class="tool-name">${icon} ${escapeHTML(toolName)}</span>
                <span class="tool-spinner" role="status" aria-label="Running">⏳</span>
            </div>
            ${argsStr ? `<div class="tool-args">${escapeHTML(argsStr)}</div>` : ''}
            <div class="tool-result tool-waiting">Executing...</div>
        `;

        el._toolStartTime = Date.now();
        container.appendChild(el);
        forceScrollToBottom();
        return el;
    }

    /**
     * Update a tool progress element with the result
     */
    function finalizeToolResult(toolEl, result, ok) {
        if (!toolEl) return;

        const duration = Date.now() - (toolEl._toolStartTime || Date.now());

        // Update spinner to status
        const spinner = toolEl.querySelector('.tool-spinner');
        if (spinner) {
            spinner.className = ok ? 'tool-status tool-ok' : 'tool-status tool-fail';
            spinner.textContent = ok ? '✓' : '✗';
            spinner.setAttribute('role', '');
            spinner.removeAttribute('aria-label');
        }

        // Update result
        const resultEl = toolEl.querySelector('.tool-result');
        if (resultEl) {
            resultEl.className = 'tool-result';
            resultEl.classList.remove('tool-waiting');
            const resultStr = typeof result === 'string' ? result : JSON.stringify(result, null, 2);

            if (resultStr.length > COLLAPSE_THRESHOLD) {
                resultEl.innerHTML = '';
                const shortSpan = document.createElement('div');
                shortSpan.className = 'tool-result-short';
                shortSpan.textContent = resultStr.substring(0, COLLAPSE_THRESHOLD) + '...';

                const fullSpan = document.createElement('div');
                fullSpan.className = 'tool-result-full';
                fullSpan.style.display = 'none';
                fullSpan.textContent = resultStr;

                const toggleBtn = document.createElement('button');
                toggleBtn.className = 'tool-toggle-btn';
                toggleBtn.textContent = 'Show more';
                toggleBtn.addEventListener('click', () => {
                    const expanded = fullSpan.style.display !== 'none';
                    fullSpan.style.display = expanded ? 'none' : '';
                    toggleBtn.textContent = expanded ? 'Show more' : 'Show less';
                });

                resultEl.appendChild(shortSpan);
                resultEl.appendChild(fullSpan);
                resultEl.appendChild(toggleBtn);
            } else {
                resultEl.textContent = resultStr;
            }
        }

        // Add duration to header
        const header = toolEl.querySelector('.tool-header');
        if (header && duration) {
            const durEl = document.createElement('span');
            durEl.className = 'tool-duration';
            durEl.textContent = formatDuration(duration);
            header.appendChild(durEl);
        }

        // Add timestamp
        const timeEl = document.createElement('div');
        timeEl.className = 'chat-time';
        timeEl.textContent = formatTime(new Date());
        toolEl.appendChild(timeEl);

        // Copy button for tool result
        if (result) {
            const resultStr = typeof result === 'string' ? result : JSON.stringify(result, null, 2);
            if (resultStr.trim()) {
                toolEl.appendChild(createMsgCopyBtn(resultStr));
            }
        }

        toolEl.classList.remove('tool-progress');
        bindCopyButtons(toolEl);
    }

    // ── Streaming Message Display ──────────────────────────────────

    /**
     * Create a new streaming message element and start accumulating text
     * @returns {HTMLElement} the message element
     */
    function startStreamingMessage() {
        const container = document.getElementById('chat-messages');
        if (!container) return null;

        const el = document.createElement('div');
        el.className = 'chat-msg assistant streaming';
        el.id = msgId();

        const badge = document.createElement('div');
        badge.className = 'chat-role-badge';
        badge.textContent = '🤖 Assistant';
        el.appendChild(badge);

        const contentEl = document.createElement('div');
        contentEl.className = 'chat-content chat-streaming';
        el.appendChild(contentEl);

        container.appendChild(el);
        currentStreamEl = el;
        streamBuffer = '';

        forceScrollToBottom();
        return el;
    }

    /**
     * Append a chunk of text to the current streaming message
     */
    function appendStreamChunk(chunk) {
        if (!currentStreamEl || !chunk) return;

        streamBuffer += chunk;

        const contentEl = currentStreamEl.querySelector('.chat-content');
        if (contentEl) {
            contentEl.innerHTML = renderMarkdownLite(streamBuffer);
            bindCopyButtons(contentEl);
            scrollToBottom();
        }

        // Throttled context ring update during streaming
        if (!appendStreamChunk._ringTimer) {
            appendStreamChunk._ringTimer = setTimeout(() => {
                updateContextRing();
                appendStreamChunk._ringTimer = null;
            }, 500);
        }
    }

    /**
     * Start or append to the reasoning/thinking block.
     * Creates a collapsible "💭 Thinking..." section in the chat.
     */
    function appendReasoningChunk(chunk) {
        if (!chunk) return;
        reasoningBuffer += chunk;

        const container = document.getElementById('chat-messages');
        if (!container) return;

        if (!reasoningEl) {
            // Create reasoning wrapper element
            reasoningEl = document.createElement('div');
            reasoningEl.className = 'chat-msg reasoning-block';

            const header = document.createElement('div');
            header.className = 'reasoning-header';
            header.innerHTML = '<span class="reasoning-icon">💭</span><span class="reasoning-title">Thinking...</span><span class="reasoning-toggle">▾</span>';

            const body = document.createElement('div');
            body.className = 'reasoning-body';
            body.style.display = 'block';

            reasoningEl.appendChild(header);
            reasoningEl.appendChild(body);

            // Toggle collapse/expand
            header.addEventListener('click', () => {
                const isHidden = body.style.display === 'none';
                body.style.display = isHidden ? 'block' : 'none';
                reasoningEl.querySelector('.reasoning-toggle').textContent = isHidden ? '▾' : '▸';
            });

            container.appendChild(reasoningEl);
            reasoningVisible = true;
        }

        // Update content (throttled innerHTML for performance)
        const body = reasoningEl.querySelector('.reasoning-body');
        if (body) {
            body.textContent = reasoningBuffer;
        }
        scrollToBottom();
    }

    /**
     * Finalize the reasoning block (model finished thinking, now producing answer)
     */
    function finalizeReasoning() {
        if (!reasoningEl) return;
        const header = reasoningEl.querySelector('.reasoning-title');
        if (header) {
            // Show token count or just "Thought for Xs"
            header.textContent = `Thought ${reasoningBuffer.length} chars`;
        }
        // Auto-collapse after finalizing (keep it tidy)
        const body = reasoningEl.querySelector('.reasoning-body');
        const toggle = reasoningEl.querySelector('.reasoning-toggle');
        if (body) body.style.display = 'none';
        if (toggle) toggle.textContent = '▸';
        reasoningEl = null;
        reasoningBuffer = '';
        reasoningVisible = false;
    }

    /**
     * Finalize the current streaming message
     */
    function finalizeStreamMessage() {
        if (!currentStreamEl) return;

        const contentEl = currentStreamEl.querySelector('.chat-content');
        if (contentEl) {
            // Final render of accumulated text
            contentEl.innerHTML = renderMarkdownLite(streamBuffer);
            contentEl.classList.remove('chat-streaming');
            bindCopyButtons(contentEl);
        }

        currentStreamEl.classList.remove('streaming');

        // Add timestamp
        const timeEl = document.createElement('div');
        timeEl.className = 'chat-time';
        timeEl.textContent = formatTime(new Date());
        currentStreamEl.appendChild(timeEl);

        // Copy button for streamed message
        if (streamBuffer && streamBuffer.trim()) {
            currentStreamEl.appendChild(createMsgCopyBtn(streamBuffer));
        }

        // Cache the message
        messages.push({
            role: 'assistant',
            content: streamBuffer,
            time: new Date()
        });

        const el = currentStreamEl;
        currentStreamEl = null;
        streamBuffer = '';

        forceScrollToBottom();
        return el;
    }

    // ── Stop Button ────────────────────────────────────────────────

    /**
     * Create and show the stop button during generation
     * @returns {HTMLElement} the stop button
     */
    function showStopButton() {
        hideStopButton();

        const inputArea = document.getElementById('chat-input-area');
        if (!inputArea) return null;

        const btn = document.createElement('button');
        btn.id = 'chat-stop';
        btn.className = 'chat-stop-btn';
        btn.textContent = '⏹ Stop';
        btn.addEventListener('click', abortGeneration);

        inputArea.insertBefore(btn, inputArea.firstChild);
        return btn;
    }

    /**
     * Hide the stop button
     */
    function hideStopButton() {
        const btn = document.getElementById('chat-stop');
        if (btn) btn.remove();
    }

    /**
     * Abort the current SSE stream and request backend cancellation
     */
    function abortGeneration() {
        if (currentAbortController) {
            currentAbortController.abort();
            currentAbortController = null;
        }
        // Also tell the backend to cancel the running agent loop
        fetch('/api/chat/task/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' } })
            .catch(() => {}); // fire-and-forget, ignore errors
    }

    // ── Send / Processing State ─────────────────────────────────────

    function setProcessing(processing) {
        isProcessing = processing;

        const sendBtn = document.getElementById('chat-send');
        const input = document.getElementById('chat-input');
        const statusEl = document.getElementById('chat-execute-status');

        if (sendBtn) {
            if (processing) {
                // Show send button as "Queue" mode during processing
                sendBtn.disabled = false;
                sendBtn.textContent = '排队';
                sendBtn.style.display = '';
            } else {
                sendBtn.disabled = false;
                sendBtn.textContent = '发送';
                sendBtn.style.display = '';
            }
        }

        if (input) {
            // Always allow input — user can queue messages while AI is processing
            input.disabled = false;
            if (!processing) {
                input.focus();
            }
        }

        if (processing) {
            showStopButton();
            if (statusEl) statusEl.textContent = '';
            // Update pending queue badge
            updatePendingBadge();
        } else {
            hideStopButton();
            hideTyping();
            // Process pending messages after a short delay
            if (pendingMessages.length > 0) {
                setTimeout(() => processPendingQueue(), 300);
            }
        }
    }

    function setExecuteStatus(text) {
        const el = document.getElementById('chat-execute-status');
        if (el) {
            el.textContent = text || '';
        }
    }

    // ── Pending Message Queue ──────────────────────────────────────

    /**
     * Add a message to the pending queue (persisted in localStorage).
     * Called when user sends a message while AI is processing.
     */
    function addToPendingQueue(text) {
        if (!text || !text.trim()) return;
        const entry = {
            text: text.trim(),
            convId: currentConvId,
            timestamp: Date.now()
        };
        pendingMessages.push(entry);
        savePendingQueue();
        updatePendingBadge();
        const preview = text.trim().length > 30 ? text.trim().substring(0, 30) + '...' : text.trim();
        showToast(`已加入排队第${pendingMessages.length}条: ${preview}`, 'info');
    }

    /**
     * Save pending queue to localStorage for crash recovery / page refresh.
     */
    function savePendingQueue() {
        try {
            if (pendingMessages.length > 0) {
                localStorage.setItem(PENDING_QUEUE_KEY, JSON.stringify(pendingMessages));
            } else {
                localStorage.removeItem(PENDING_QUEUE_KEY);
            }
        } catch (e) { /* ignore storage errors */ }
    }

    /**
     * Load pending queue from localStorage (called on init / page refresh).
     */
    function loadPendingQueue() {
        try {
            const saved = localStorage.getItem(PENDING_QUEUE_KEY);
            if (saved) {
                pendingMessages = JSON.parse(saved);
                if (!Array.isArray(pendingMessages)) pendingMessages = [];
            }
        } catch (e) {
            pendingMessages = [];
        }
        updatePendingBadge();
    }

    /**
     * Update the pending queue display above the input box.
     * Shows each queued message with its content, order number, and delete button.
     */
    function updatePendingBadge() {
        // Update badge on the send button
        const sendBtn = document.getElementById('chat-send');
        if (sendBtn && isProcessing) {
            if (pendingMessages.length > 0) {
                sendBtn.textContent = `排队(${pendingMessages.length})`;
            } else {
                sendBtn.textContent = '排队';
            }
        }

        // Update or create the pending queue container above the textarea
        let container = document.getElementById('chat-pending-queue');
        if (pendingMessages.length > 0) {
            if (!container) {
                container = document.createElement('div');
                container.id = 'chat-pending-queue';
                // Insert after chat-execute-status, before chat-input
                const inputEl = document.getElementById('chat-input');
                if (inputEl && inputEl.parentNode) {
                    inputEl.parentNode.insertBefore(container, inputEl);
                }
            }

            // Build queue list HTML
            let html = '<div class="pending-queue-header">';
            html += `<span class="pending-queue-title">⏳ 排队消息 (${pendingMessages.length})</span>`;
            html += `<button class="pending-queue-clear" onclick="ChatModule.clearPendingQueue()">全部清除</button>`;
            html += '</div>';
            html += '<div class="pending-queue-list">';
            pendingMessages.forEach((entry, idx) => {
                const preview = entry.text.length > 60 ? entry.text.substring(0, 60) + '...' : entry.text;
                const escapedText = preview.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
                const fullText = entry.text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
                html += `<div class="pending-queue-item" title="${fullText}">`;
                html += `<span class="pending-queue-num">${idx + 1}</span>`;
                html += `<span class="pending-queue-text">${escapedText}</span>`;
                html += `<button class="pending-queue-del" data-idx="${idx}" title="删除此条">✕</button>`;
                html += '</div>';
            });
            html += '</div>';

            container.innerHTML = html;
            container.style.display = 'block';

            // Bind delete buttons
            container.querySelectorAll('.pending-queue-del').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    const idx = parseInt(btn.dataset.idx, 10);
                    if (!isNaN(idx) && idx >= 0 && idx < pendingMessages.length) {
                        pendingMessages.splice(idx, 1);
                        savePendingQueue();
                        updatePendingBadge();
                    }
                });
            });
        } else if (container) {
            container.style.display = 'none';
        }
    }

    /**
     * Process the pending queue: send the first message and keep the rest queued.
     * Called automatically when setProcessing(false) detects pending messages.
     */
    async function processPendingQueue() {
        if (pendingMessages.length === 0) return;
        if (isProcessing) return; // safety check

        // Take the first message from the queue
        const entry = pendingMessages.shift();
        savePendingQueue();
        updatePendingBadge();

        // Restore conversation id if saved with the entry
        if (entry.convId && !currentConvId) {
            currentConvId = entry.convId;
        }

        // Add a separator showing this was a queued message
        const preview = entry.text.length > 40 ? entry.text.substring(0, 40) + '...' : entry.text;
        addMessage('system', `📤 发送排队消息: ${preview}`);

        // Send it
        await sendMessage(entry.text);
    }

    // ── API: Load History ──────────────────────────────────────────

    async function loadHistory() {
        // First check localStorage backup (for crash/refresh recovery)
        const backup = restoreBackup();
        if (backup && backup.messages && backup.messages.length > 0) {
            // Restore from backup — it has more recent data than backend
            if (backup.convId) {
                currentConvId = backup.convId;
            }
            renderMessages(backup.messages);
            return backup.messages;
        }

        // No backup — load from backend
        try {
            const resp = await fetch('/api/chat/history');
            if (!resp.ok) throw new Error(`Failed to load history: ${resp.statusText}`);

            const data = await resp.json();
            const msgs = data.messages || [];
            // Restore currentConvId from backend so subsequent messages
            // continue the same conversation instead of creating a new one
            if (data.conv_id && !currentConvId) {
                currentConvId = data.conv_id;
            }
            renderMessages(msgs);
            return msgs;
        } catch (err) {
            console.warn('ChatManager: loadHistory error:', err.message);
            renderMessages([]);
            return [];
        }
    }

    // ── Task Status & Reconnection ────────────────────────────────

    /**
     * Check if a task is running on the backend, and if so, reconnect to it.
     * Called on init / page load.
     */
    async function checkAndRecoverTask() {
        try {
            const resp = await fetch('/api/chat/task/status');
            if (!resp.ok) return;

            const data = await resp.json();
            if (data.running) {
                // A task is running on the backend, show reconnect notification
                const elapsed = Math.round(data.elapsed || 0);
                const elapsedStr = elapsed >= 60
                    ? `${Math.floor(elapsed / 60)}m ${elapsed % 60}s`
                    : `${elapsed}s`;
                addMessage('system', `🔄 A task is still running (started ${elapsedStr} ago). Reconnecting...`);
                showToast('Reconnecting to running task...', 'info', 3000);
                await reconnectTask(data.conv_id);
            }
        } catch (err) {
            console.warn('ChatManager: checkAndRecoverTask error:', err.message);
        }
    }

    /**
     * Reconnect to a running task by subscribing to /api/chat/task/stream.
     * Replays buffered events then receives live events.
     */
    async function reconnectTask(convId) {
        if (isProcessing) return;

        isReconnecting = true;
        isProcessing = true;

        // Restore conversation id if provided
        if (convId) {
            currentConvId = convId;
        }

        setProcessing(true);
        hideTurnIndicator();

        streamingStartTime = Date.now();
        iterationCount = 0;
        let currentToolEl = null;
        let lastToolName = null;
        let hasError = false;

        // Start periodic backup during reconnection streaming
        startBackupTimer();

        currentAbortController = new AbortController();
        const signal = currentAbortController.signal;
        let lastToolArgs = {};

        try {
            const resp = await fetch('/api/chat/task/stream', { signal });

            if (!resp.ok) {
                if (resp.status === 404) {
                    // Task already finished — clean up UI state
                    isReconnecting = false;
                    setProcessing(false);
                    return;
                }
                throw new Error(`Reconnect failed: ${resp.status} ${resp.statusText}`);
            }

            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            let caughtUp = false;

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });

                const lines = buffer.split('\n');
                buffer = lines.pop() || '';

                for (const line of lines) {
                    if (!line.startsWith('data: ')) continue;
                    const rawData = line.substring(6).trim();
                    if (!rawData || rawData === '[DONE]') continue;

                    let parsed;
                    try {
                        parsed = JSON.parse(rawData);
                    } catch (_) {
                        continue;
                    }

                    const eventType = parsed.type || '';

                    if (eventType === 'keepalive') {
                        continue;
                    } else if (eventType === 'reconnected') {
                        // Skip — this is just a signal
                        continue;
                    }

                    // Once we see the first non-buffer event after buffered data, we're caught up
                    if (!caughtUp && eventType !== 'thinking') {
                        caughtUp = true;
                    }

                    if (eventType === 'text') {
                        hideTyping();
                        if (!currentStreamEl) {
                            startStreamingMessage();
                        }
                        appendStreamChunk(parsed.content || parsed.text || '');
                    } else if (eventType === 'tool_start') {
                        hideTyping();
                        if (currentStreamEl && streamBuffer) {
                            finalizeStreamMessage();
                        }
                        currentToolEl = showToolProgress(
                            parsed.tool || parsed.name || 'unknown',
                            parsed.args
                        );
                        lastToolName = parsed.tool || parsed.name || null;
                        lastToolArgs = parsed.args || {};
                        setExecuteStatus(`Running ${parsed.tool || parsed.name || 'tool'}...`);
                    } else if (eventType === 'tool_result') {
                        const ok = parsed.ok !== false && parsed.error === undefined;
                        const toolResultStr = parsed.result || parsed.output || parsed.error || '';
                        finalizeToolResult(
                            currentToolEl,
                            toolResultStr,
                            ok
                        );
                        // Update todo panel if todo_write was called
                        if (lastToolName === 'todo_write' && lastToolArgs && lastToolArgs.todos) {
                            createOrUpdateTodoPanel(lastToolArgs.todos);
                        }
                        // Save tool result to messages[] so backup includes it
                        messages.push({
                            role: 'tool',
                            tool: lastToolName || 'unknown',
                            name: lastToolName || 'unknown',
                            content: toolResultStr,
                            ok: ok,
                            args: lastToolArgs || {},
                            time: new Date().toISOString()
                        });
                        iterationCount++;
                        updateTurnIndicator(iterationCount, parsed.max_iterations || 0);
                        setExecuteStatus(`Turn ${iterationCount}${parsed.max_iterations ? '/' + parsed.max_iterations : ''}`);
                        currentToolEl = null;
                        if (window.FileManager && lastToolName) {
                            const fileTools = ['write_file', 'edit_file', 'create_directory', 'delete_path', 'install_package'];
                            if (fileTools.includes(lastToolName)) {
                                window.FileManager.refresh();
                            }
                        }
                        if (window.DebuggerUI && lastToolName) {
                            const debugTools = ['debug_start', 'debug_stop', 'debug_set_breakpoints',
                                'debug_continue', 'debug_step', 'debug_inspect', 'debug_evaluate', 'debug_stack',
                                'browser_navigate', 'browser_evaluate', 'browser_inspect', 'browser_query_all',
                                'browser_click', 'browser_input', 'browser_console', 'browser_page_info', 'server_logs'];
                            if (debugTools.includes(lastToolName)) {
                                try {
                                    document.dispatchEvent(new CustomEvent('debug:ai_activity', {
                                        detail: {
                                            tool: lastToolName,
                                            args: parsed.args || {},
                                            result: (parsed.result || parsed.output || '') || '',
                                        }
                                    }));
                                } catch(e) {}
                            }
                        }
                        lastToolName = null;
                        lastToolArgs = {};
                        forceScrollToBottom();
                    } else if (eventType === 'thinking') {
                        setExecuteStatus(parsed.message || parsed.text || parsed.content || 'Thinking...');
                        hideTyping();
                        showTyping();
                    } else if (eventType === 'reasoning') {
                        hideTyping();
                        setExecuteStatus('💭 Reasoning...');
                        appendReasoningChunk(parsed.content || '');
                    } else if (eventType === 'reasoning_end') {
                        finalizeReasoning();
                        setExecuteStatus('Generating...');
                    } else if (eventType === 'cancelled') {
                        hideTyping();
                        finalizeReasoning();
                        setExecuteStatus('已取消');
                        if (currentStreamEl && streamBuffer) {
                            finalizeStreamMessage();
                        }
                        addMessage('system', '⏹ 任务已停止');
                        if (window.notifyAndroid) window.notifyAndroid('MusIDE', 'AI 任务已停止', 'warning', 3000);
                    } else if (eventType === 'error') {
                        hasError = true;
                        hideTyping();
                        finalizeReasoning();
                        setExecuteStatus('');
                        if (currentStreamEl && streamBuffer) {
                            finalizeStreamMessage();
                        }
                        const errMsg = parsed.content || parsed.message || parsed.error || rawData;
                        addMessage('error', errMsg, { retryable: true });
                        if (window.notifyAndroid) window.notifyAndroid('MusIDE Error', errMsg.substring(0, 200), 'error', 8000);
                    } else if (eventType === 'done') {
                        hideTyping();
                        let finalizedEl = null;
                        if (currentStreamEl && streamBuffer) {
                            finalizedEl = finalizeStreamMessage();
                        }
                        // Task completed — backend saved history, clear local backup
                        clearBackup();
                        const totalDuration = Date.now() - streamingStartTime;
                        const tokensUsed = estimateTokens(streamBuffer);
                        let summary = `Completed in ${formatDuration(totalDuration)}`;
                        if (parsed.iterations) {
                            summary += ` · ${parsed.iterations} iteration(s)`;
                        }
                        summary += ` · ~${tokensUsed} tokens`;
                        setExecuteStatus(summary);
                        playCompletionSound();
                        showToast('✅ Task completed successfully!', 'success', 3000);
                        if (window.notifyAndroid) window.notifyAndroid('MusIDE', `✅ Task done ${summary}`, 'success', 5000);
                    }
                }
            }

        } catch (err) {
            if (err.name === 'AbortError') {
                hideTyping();
                if (currentStreamEl && streamBuffer) {
                    finalizeStreamMessage();
                }
                addMessage('system', 'Task reconnection stopped.');
            } else {
                hideTyping();
                if (currentStreamEl && streamBuffer) {
                    finalizeStreamMessage();
                }
                addMessage('error', err.message, { retryable: true });
                hasError = true;
            }
        } finally {
            currentAbortController = null;
            isReconnecting = false;
            hideTurnIndicator();
            hideTaskActivityBadge();
            stopTaskStatusPolling();
            autoResizeInput();

            // Stop backup timer — backend has saved the history on completion
            stopBackupTimer();

            // setProcessing(false) handles: sendBtn, input, hideStopButton, hideTyping, pending queue
            setProcessing(false);
        }
    }

    // ── Task Activity Badge (on #btn-chat) ────────────────────────

    /**
     * Show an animated badge on #btn-chat to indicate a running task
     * when the sidebar is closed.
     */
    function showTaskActivityBadge() {
        if (taskActivityBadge) return;

        // Inject the CSS for the badge animation
        if (!document.getElementById('task-badge-style')) {
            const style = document.createElement('style');
            style.id = 'task-badge-style';
            style.textContent = `
                .task-activity-badge {
                    position: absolute;
                    top: 4px;
                    right: 4px;
                    width: 10px;
                    height: 10px;
                    border-radius: 50%;
                    background: #22c55e;
                    border: 2px solid var(--bg-primary, #fff);
                    animation: task-badge-pulse 1.5s ease-in-out infinite;
                    z-index: 10;
                    pointer-events: none;
                }
                @keyframes task-badge-pulse {
                    0%, 100% { opacity: 1; transform: scale(1); }
                    50% { opacity: 0.5; transform: scale(1.3); }
                }
            `;
            document.head.appendChild(style);
        }

        const chatBtn = document.getElementById('btn-chat');
        if (!chatBtn) return;

        // Make sure the button is positioned relatively
        const btnStyle = window.getComputedStyle(chatBtn);
        if (btnStyle.position === 'static') {
            chatBtn.style.position = 'relative';
        }

        taskActivityBadge = document.createElement('span');
        taskActivityBadge.className = 'task-activity-badge';
        chatBtn.appendChild(taskActivityBadge);
    }

    function hideTaskActivityBadge() {
        if (taskActivityBadge) {
            taskActivityBadge.remove();
            taskActivityBadge = null;
        }
    }

    /**
     * Start polling /api/chat/task/status periodically to show/hide
     * the activity badge and detect task completion.
     */
    function startTaskStatusPolling() {
        stopTaskStatusPolling();
        taskStatusInterval = setInterval(async () => {
            try {
                const resp = await fetch('/api/chat/task/status');
                if (!resp.ok) {
                    hideTaskActivityBadge();
                    stopTaskStatusPolling();
                    return;
                }
                const data = await resp.json();
                if (data.running) {
                    // Only show badge if sidebar is closed
                    const sidebar = document.getElementById('sidebar-right');
                    const isOpen = sidebar && (sidebar.classList.contains('open') || sidebar.style.display !== 'none');
                    if (!isOpen && !isProcessing) {
                        showTaskActivityBadge();
                    } else {
                        hideTaskActivityBadge();
                    }
                } else {
                    hideTaskActivityBadge();
                    stopTaskStatusPolling();
                }
            } catch (_) {
                // Ignore errors in polling
            }
        }, 5000);
    }

    function stopTaskStatusPolling() {
        if (taskStatusInterval) {
            clearInterval(taskStatusInterval);
            taskStatusInterval = null;
        }
    }

    // ── API: Send Message (SSE Streaming) ──────────────────────────

    /**
     * Send a message to the chat API using SSE streaming
     * @param {string} [text] - message text (defaults to input field value)
     */
    async function sendMessage(text) {
        const input = document.getElementById('chat-input');
        const message = text || (input ? input.value : '').trim();

        if (!message) {
            showToast('请输入消息', 'warning');
            return;
        }

        // If AI is currently processing, queue the message instead
        if (isProcessing) {
            addToPendingQueue(message);
            // Clear input
            if (input) {
                input.value = '';
                localStorage.removeItem('muside_chat_input');
            }
            autoResizeInput();
            return;
        }

        // Ensure we have a conversation id
        if (!currentConvId) {
            currentConvId = 'conv_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
        }

        // Clear input
        if (input) {
            input.value = '';
            localStorage.removeItem('muside_chat_input');
        }
        autoResizeInput();
        lastUserMessage = message;

        // Add user message to display
        addMessage('user', message);
        setProcessing(true);
        hideTurnIndicator();

        // Plan mode: prepend plan instruction to message
        let actualMessage = message;
        if (chatMode === 'plan') {
            actualMessage = PLAN_MODE_PROMPT + message;
        }

        streamingStartTime = Date.now();
        iterationCount = 0;
        let currentToolEl = null;
        let lastToolName = null;
        let lastToolArgs = {};
        let hasError = false;

        // Start periodic backup during streaming
        startBackupTimer();

        // Create abort controller
        currentAbortController = new AbortController();
        const signal = currentAbortController.signal;

        try {
            const reqUrl = '/api/chat/send/stream';
            const modelSelect = document.getElementById('chat-model-select');
            const reqBody = { message: actualMessage, conv_id: currentConvId };
            if (modelSelect && modelSelect.value !== '') {
                reqBody.model_index = parseInt(modelSelect.value);
            }

            const resp = await fetch(reqUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(reqBody),
                signal
            });

            if (!resp.ok) {
                // Handle 409: a task is already running on the backend
                if (resp.status === 409) {
                    addMessage('system', 'A task is already running. Waiting for it to finish...');
                    showToast('A task is already running', 'warning');
                    // Start polling so the user can see when it's done
                    startTaskStatusPolling();
                    setProcessing(false);
                    return;
                }
                const errBody = await resp.text().catch(() => '');
                // Build detailed error info
                const detail = [
                    `Status: ${resp.status} ${resp.statusText}`,
                    `Type: ${resp.type}`,
                    `Redirected: ${resp.redirected}`,
                    `URL: ${resp.url}`,
                    `Request: POST ${reqUrl}`,
                ].join('\n');
                const fullMsg = errBody ? `${detail}\n\n${errBody}` : detail;
                throw new Error(fullMsg);
            }

            // Read the SSE stream
            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            sseConnectionAlive = true;  // Mark SSE as connected

            while (true) {
                const { done, value } = await reader.read();
                if (done) {
                    sseConnectionAlive = false;
                    break;
                }

                buffer += decoder.decode(value, { stream: true });

                // Parse SSE events from buffer
                const lines = buffer.split('\n');
                buffer = lines.pop() || ''; // keep incomplete line in buffer

                for (const line of lines) {
                    if (!line.startsWith('data: ')) continue;
                    const rawData = line.substring(6).trim();
                    if (!rawData || rawData === '[DONE]') continue;

                    // Parse JSON and use 'type' field to determine event type
                    let parsed;
                    try {
                        parsed = JSON.parse(rawData);
                    } catch (_) {
                        continue;
                    }

                    const eventType = parsed.type || '';

                    if (eventType === 'text') {
                        // Text chunk from assistant
                        hideTyping();
                        if (!currentStreamEl) {
                            startStreamingMessage();
                        }
                        appendStreamChunk(parsed.content || parsed.text || '');
                    } else if (eventType === 'tool_start') {
                        // Tool execution starting
                        hideTyping();
                        if (currentStreamEl && streamBuffer) {
                            finalizeStreamMessage();
                        }
                        currentToolEl = showToolProgress(
                            parsed.tool || parsed.name || 'unknown',
                            parsed.args
                        );
                        lastToolName = parsed.tool || parsed.name || null;
                        lastToolArgs = parsed.args || {};
                        setExecuteStatus(`Running ${parsed.tool || parsed.name || 'tool'}...`);
                    } else if (eventType === 'tool_result') {
                        // Tool execution completed
                        const ok = parsed.ok !== false && parsed.error === undefined;
                        const toolResultStr = parsed.result || parsed.output || parsed.error || '';
                        finalizeToolResult(
                            currentToolEl,
                            toolResultStr,
                            ok
                        );
                        // Update todo panel if todo_write was called
                        if (lastToolName === 'todo_write' && lastToolArgs && lastToolArgs.todos) {
                            createOrUpdateTodoPanel(lastToolArgs.todos);
                        }
                        // Save tool result to messages[] so backup includes it
                        messages.push({
                            role: 'tool',
                            tool: lastToolName || 'unknown',
                            name: lastToolName || 'unknown',
                            content: toolResultStr,
                            ok: ok,
                            args: lastToolArgs || {},
                            time: new Date().toISOString()
                        });
                        iterationCount++;
                        updateTurnIndicator(iterationCount, parsed.max_iterations || 0);
                        setExecuteStatus(`Turn ${iterationCount}${parsed.max_iterations ? '/' + parsed.max_iterations : ''}`);
                        currentToolEl = null;
                        // Refresh file tree after file-related tool calls
                        if (window.FileManager && lastToolName) {
                            const fileTools = ['write_file', 'edit_file', 'create_directory', 'delete_path', 'install_package'];
                            if (fileTools.includes(lastToolName)) {
                                window.FileManager.refresh();
                            }
                        }
                        // Dispatch AI debug activity event for debugger UI
                        if (window.DebuggerUI && lastToolName) {
                            const debugTools = ['debug_start', 'debug_stop', 'debug_set_breakpoints',
                                'debug_continue', 'debug_step', 'debug_inspect', 'debug_evaluate', 'debug_stack',
                                'browser_navigate', 'browser_evaluate', 'browser_inspect', 'browser_query_all',
                                'browser_click', 'browser_input', 'browser_console', 'browser_page_info', 'server_logs'];
                            if (debugTools.includes(lastToolName)) {
                                try {
                                    document.dispatchEvent(new CustomEvent('debug:ai_activity', {
                                        detail: {
                                            tool: lastToolName,
                                            args: parsed.args || {},
                                            result: (parsed.result || parsed.output || '') || '',
                                        }
                                    }));
                                } catch(e) {}
                            }
                        }
                        lastToolName = null;
                        lastToolArgs = {};
                        forceScrollToBottom();
                    } else if (eventType === 'thinking') {
                        // Status / thinking message
                        setExecuteStatus(parsed.message || parsed.text || parsed.content || 'Thinking...');
                        hideTyping();
                        showTyping();
                    } else if (eventType === 'reasoning') {
                        // Model reasoning/thinking content (DeepSeek-R1, QwQ, etc.)
                        hideTyping();
                        setExecuteStatus('💭 Reasoning...');
                        appendReasoningChunk(parsed.content || '');
                    } else if (eventType === 'reasoning_end') {
                        // Model finished reasoning, now producing answer
                        finalizeReasoning();
                        setExecuteStatus('Generating...');
                    } else if (eventType === 'cancelled') {
                        hideTyping();
                        finalizeReasoning();
                        setExecuteStatus('已取消');
                        if (currentStreamEl && streamBuffer) {
                            finalizeStreamMessage();
                        }
                        addMessage('system', '⏹ 任务已停止');
                    } else if (eventType === 'error') {
                        // Error occurred
                        hasError = true;
                        hideTyping();
                        finalizeReasoning();
                        setExecuteStatus('');  // clear retry status
                        if (currentStreamEl && streamBuffer) {
                            finalizeStreamMessage();
                        }
                        const errMsg = parsed.content || parsed.message || parsed.error || rawData;
                        addMessage('error', errMsg, { retryable: true });
                        showToast('Chat error: ' + errMsg, 'error');
                        // Also log to console error tab
                        if (window.DebugManager && window.DebugManager.addError) {
                            window.DebugManager.addError('LLM Error: ' + errMsg, 'chat');
                        }
                    } else if (eventType === 'done') {
                        // Generation complete
                        hideTyping();
                        let finalizedEl = null;
                        if (currentStreamEl && streamBuffer) {
                            finalizedEl = finalizeStreamMessage();
                        }
                        const totalDuration = Date.now() - streamingStartTime;
                        const tokensUsed = estimateTokens(streamBuffer);
                        let summary = `Completed in ${formatDuration(totalDuration)}`;
                        if (parsed.iterations) {
                            summary += ` · ${parsed.iterations} iteration(s)`;
                        }
                        summary += ` · ~${tokensUsed} tokens`;
                        setExecuteStatus(summary);
                        
                        // Play completion sound and show notification
                        playCompletionSound();
                        showToast('✅ Task completed successfully!', 'success', 3000);

                        // Task completed — backend saved history, clear local backup
                        clearBackup();
                        
                        // In plan mode, inject action buttons
                        if (chatMode === 'plan' && finalizedEl && streamBuffer) {
                            lastPlanMsgEl = finalizedEl;
                            planContent = streamBuffer;
                            injectPlanActions(finalizedEl, streamBuffer);
                        }
                    }
                }
            }

        } catch (err) {
            sseConnectionAlive = false;
            if (err.name === 'AbortError') {
                // User aborted
                hideTyping();
                if (currentStreamEl && streamBuffer) {
                    finalizeStreamMessage();
                }
                addMessage('system', 'Generation stopped by user.');
                showToast('Generation stopped', 'info');
            } else if (document.hidden) {
                // SSE connection dropped while page was in background — mark it so we
                // can auto-reconnect when the page becomes visible again
                sseConnectionLostWhileHidden = true;
                console.warn('ChatManager: SSE connection lost while page was hidden, will reconnect on restore');
            } else {
                hideTyping();
                if (currentStreamEl && streamBuffer) {
                    finalizeStreamMessage();
                }
                addMessage('error', err.message, { retryable: true });
                showToast('Chat error: ' + err.message, 'error');
                hasError = true;
            }
            // On error/abort, do a final backup so messages survive refresh
            backupMessages();
        } finally {
            // Only clean up processing state if we are NOT going to reconnect later
            // (i.e. if SSE didn't die while hidden)
            if (!sseConnectionLostWhileHidden) {
                currentAbortController = null;
                hideTurnIndicator();
                autoResizeInput();

                // Stop the periodic backup timer
                stopBackupTimer();

                // setProcessing(false) handles: isProcessing, sendBtn, input, hideStopButton, hideTyping, pending queue
                setProcessing(false);
            }
        }
    }

    // ── API: Clear History ─────────────────────────────────────────

    async function clearHistory() {
        try {
            const resp = await fetch('/api/chat/clear', { method: 'POST' });
            if (!resp.ok) throw new Error(`Failed to clear: ${resp.statusText}`);
            await resp.json();

            messages = [];
            lastUserMessage = null;
            renderMessages([]);
            updateContextRing();
            // Clear todo panel when clearing history
            hideTodoPanel();
            showToast('Chat history cleared', 'success');
        } catch (err) {
            showToast('Error clearing chat: ' + err.message, 'error');
        }
    }

    // ── API: New Chat ───────────────────────────────────────────────

    async function newChat() {
        if (isProcessing) {
            showToast('请等待当前任务完成', 'warning');
            return;
        }
        // Reset conversation
        currentConvId = null;
        messages = [];
        lastUserMessage = null;
        pendingMessages = [];
        savePendingQueue();
        updatePendingBadge();
        renderMessages([]);
        clearBackup();
        updateContextRing();
        // Clear todo panel for new conversation
        hideTodoPanel();

        // Focus input
        const input = document.getElementById('chat-input');
        if (input) {
            input.value = '';
            input.focus();
        }
        showToast('New conversation started', 'success');
    }

    // ── Re-Send Last Message ───────────────────────────────────────

    async function resendLastMessage() {
        if (isProcessing) return;
        if (!lastUserMessage) {
            showToast('No previous message to resend', 'warning');
            return;
        }
        await sendMessage(lastUserMessage);
    }

    // ── Retry from Error (continue from failure point) ────────────

    /**
     * Retry a failed task by continuing from where it left off.
     * Unlike resendLastMessage() which restarts from scratch, this sends
     * a retry flag with conv_id so the backend resumes the agent loop
     * using the already-saved conversation history (which includes all
     * tool calls and results from before the error).
     */
    async function retryFromError() {
        if (isProcessing) return;

        if (!currentConvId) {
            // No conversation ID — can't resume, fall back to resend
            if (lastUserMessage) {
                await sendMessage(lastUserMessage);
            } else {
                showToast('No task to retry', 'warning');
            }
            return;
        }

        // Ensure convId exists
        if (!currentConvId) {
            currentConvId = 'conv_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
        }

        setProcessing(true);
        hideTurnIndicator();

        streamingStartTime = Date.now();
        iterationCount = 0;
        let currentToolEl = null;
        let lastToolName = null;
        let lastToolArgs = {};
        let hasError = false;

        // Start periodic backup during streaming
        startBackupTimer();

        // Create abort controller
        currentAbortController = new AbortController();
        const signal = currentAbortController.signal;

        try {
            const reqUrl = '/api/chat/send/stream';
            const modelSelect = document.getElementById('chat-model-select');
            const reqBody = {
                message: lastUserMessage || '',
                conv_id: currentConvId,
                retry: true  // Tell backend to continue from saved state
            };
            if (modelSelect && modelSelect.value !== '') {
                reqBody.model_index = parseInt(modelSelect.value);
            }

            addMessage('system', '🔄 Retrying from where it left off...');

            const resp = await fetch(reqUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(reqBody),
                signal
            });

            if (!resp.ok) {
                if (resp.status === 409) {
                    addMessage('system', 'A task is already running. Waiting...');
                    showToast('A task is already running', 'warning');
                    startTaskStatusPolling();
                    setProcessing(false);
                    return;
                }
                const errBody = await resp.text().catch(() => '');
                const detail = `Status: ${resp.status} ${resp.statusText}\n${errBody}`;
                throw new Error(detail);
            }

            // Read the SSE stream
            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            sseConnectionAlive = true;  // Mark SSE as connected

            while (true) {
                const { done, value } = await reader.read();
                if (done) {
                    sseConnectionAlive = false;
                    break;
                }

                buffer += decoder.decode(value, { stream: true });

                const lines = buffer.split('\n');
                buffer = lines.pop() || '';

                for (const line of lines) {
                    if (!line.startsWith('data: ')) continue;
                    const rawData = line.substring(6).trim();
                    if (!rawData || rawData === '[DONE]') continue;

                    let parsed;
                    try {
                        parsed = JSON.parse(rawData);
                    } catch (_) {
                        continue;
                    }

                    const eventType = parsed.type || '';

                    if (eventType === 'text') {
                        hideTyping();
                        if (!currentStreamEl) {
                            startStreamingMessage();
                        }
                        appendStreamChunk(parsed.content || parsed.text || '');
                    } else if (eventType === 'tool_start') {
                        hideTyping();
                        if (currentStreamEl && streamBuffer) {
                            finalizeStreamMessage();
                        }
                        currentToolEl = showToolProgress(
                            parsed.tool || parsed.name || 'unknown',
                            parsed.args
                        );
                        lastToolName = parsed.tool || parsed.name || null;
                        lastToolArgs = parsed.args || {};
                        setExecuteStatus(`Running ${parsed.tool || parsed.name || 'tool'}...`);
                    } else if (eventType === 'tool_result') {
                        const ok = parsed.ok !== false && parsed.error === undefined;
                        const toolResultStr = parsed.result || parsed.output || parsed.error || '';
                        finalizeToolResult(
                            currentToolEl,
                            toolResultStr,
                            ok
                        );
                        // Update todo panel if todo_write was called
                        if (lastToolName === 'todo_write' && lastToolArgs && lastToolArgs.todos) {
                            createOrUpdateTodoPanel(lastToolArgs.todos);
                        }
                        // Save tool result to messages[] so backup includes it
                        messages.push({
                            role: 'tool',
                            tool: lastToolName || 'unknown',
                            name: lastToolName || 'unknown',
                            content: toolResultStr,
                            ok: ok,
                            args: lastToolArgs || {},
                            time: new Date().toISOString()
                        });
                        iterationCount++;
                        updateTurnIndicator(iterationCount, parsed.max_iterations || 0);
                        setExecuteStatus(`Turn ${iterationCount}${parsed.max_iterations ? '/' + parsed.max_iterations : ''}`);
                        currentToolEl = null;
                        if (window.FileManager && lastToolName) {
                            const fileTools = ['write_file', 'edit_file', 'create_directory', 'delete_path', 'install_package'];
                            if (fileTools.includes(lastToolName)) {
                                window.FileManager.refresh();
                            }
                        }
                        if (window.DebuggerUI && lastToolName) {
                            const debugTools = ['debug_start', 'debug_stop', 'debug_set_breakpoints',
                                'debug_continue', 'debug_step', 'debug_inspect', 'debug_evaluate', 'debug_stack',
                                'browser_navigate', 'browser_evaluate', 'browser_inspect', 'browser_query_all',
                                'browser_click', 'browser_input', 'browser_console', 'browser_page_info', 'server_logs'];
                            if (debugTools.includes(lastToolName)) {
                                try {
                                    document.dispatchEvent(new CustomEvent('debug:ai_activity', {
                                        detail: {
                                            tool: lastToolName,
                                            args: parsed.args || {},
                                            result: (parsed.result || parsed.output || '') || '',
                                        }
                                    }));
                                } catch(e) {}
                            }
                        }
                        lastToolName = null;
                        lastToolArgs = {};
                        forceScrollToBottom();
                    } else if (eventType === 'thinking') {
                        setExecuteStatus(parsed.message || parsed.text || parsed.content || 'Thinking...');
                        hideTyping();
                        showTyping();
                    } else if (eventType === 'reasoning') {
                        hideTyping();
                        setExecuteStatus('💭 Reasoning...');
                        appendReasoningChunk(parsed.content || '');
                    } else if (eventType === 'reasoning_end') {
                        finalizeReasoning();
                        setExecuteStatus('Generating...');
                    } else if (eventType === 'cancelled') {
                        hideTyping();
                        finalizeReasoning();
                        setExecuteStatus('已取消');
                        if (currentStreamEl && streamBuffer) {
                            finalizeStreamMessage();
                        }
                        addMessage('system', '⏹ 任务已停止');
                    } else if (eventType === 'error') {
                        hasError = true;
                        hideTyping();
                        finalizeReasoning();
                        setExecuteStatus('');
                        if (currentStreamEl && streamBuffer) {
                            finalizeStreamMessage();
                        }
                        const errMsg = parsed.content || parsed.message || parsed.error || rawData;
                        addMessage('error', errMsg, { retryable: true });
                        showToast('Chat error: ' + errMsg, 'error');
                    } else if (eventType === 'done') {
                        hideTyping();
                        let finalizedEl = null;
                        if (currentStreamEl && streamBuffer) {
                            finalizedEl = finalizeStreamMessage();
                        }
                        const totalDuration = Date.now() - streamingStartTime;
                        const tokensUsed = estimateTokens(streamBuffer);
                        let summary = `Completed in ${formatDuration(totalDuration)}`;
                        if (parsed.iterations) {
                            summary += ` · ${parsed.iterations} iteration(s)`;
                        }
                        summary += ` · ~${tokensUsed} tokens`;
                        setExecuteStatus(summary);
                        playCompletionSound();
                        showToast('✅ Task completed successfully!', 'success', 3000);

                        // Task completed — clear local backup
                        clearBackup();

                        if (chatMode === 'plan' && finalizedEl && streamBuffer) {
                            lastPlanMsgEl = finalizedEl;
                            planContent = streamBuffer;
                            injectPlanActions(finalizedEl, streamBuffer);
                        }
                    }
                }
            }

        } catch (err) {
            sseConnectionAlive = false;
            if (err.name === 'AbortError') {
                hideTyping();
                if (currentStreamEl && streamBuffer) {
                    finalizeStreamMessage();
                }
                addMessage('system', 'Retry stopped by user.');
                showToast('Retry stopped', 'info');
            } else if (document.hidden) {
                // SSE died while page hidden — mark for reconnection on restore
                sseConnectionLostWhileHidden = true;
                console.warn('ChatManager: Retry SSE lost while page hidden, will reconnect on restore');
            } else {
                hideTyping();
                if (currentStreamEl && streamBuffer) {
                    finalizeStreamMessage();
                }
                addMessage('error', err.message, { retryable: true });
                showToast('Retry error: ' + err.message, 'error');
                hasError = true;
            }
            backupMessages();
        } finally {
            if (!sseConnectionLostWhileHidden) {
                currentAbortController = null;
                hideTurnIndicator();
                autoResizeInput();
                stopBackupTimer();

                // setProcessing(false) handles: isProcessing, sendBtn, input, hideStopButton, hideTyping, pending queue
                setProcessing(false);
            }
        }
    }

    // ── LLM Settings ───────────────────────────────────────────────

    async function loadLLMConfig() {
        try {
            const resp = await fetch('/api/llm/config');
            if (!resp.ok) throw new Error(`Failed to load config: ${resp.statusText}`);
            return await resp.json();
        } catch (err) {
            console.warn('ChatManager: loadLLMConfig error:', err.message);
            return null;
        }
    }

    // ── Model Selector ─────────────────────────────────────────────

    async function refreshModelSelector() {
        const select = document.getElementById('chat-model-select');
        if (!select) return;
        const config = await loadLLMConfig();
        if (!config) return;
        const models = config.models || [];
        const savedModel = localStorage.getItem('muside_chat_model');
        const currentVal = select.value;
        select.innerHTML = '';
        for (let i = 0; i < models.length; i++) {
            const m = models[i];
            const opt = document.createElement('option');
            opt.value = i;
            opt.textContent = m.name || m.model || ('Model ' + (i + 1));
            opt.disabled = !m.enabled;
            if (opt.disabled) opt.textContent += ' (disabled)';
            select.appendChild(opt);
        }
        // Restore saved selection, then previous selection, then first enabled
        if (savedModel !== null && select.querySelector(`option[value="${savedModel}"]`)) {
            select.value = savedModel;
        } else if (currentVal !== '' && select.querySelector(`option[value="${currentVal}"]`)) {
            select.value = currentVal;
        } else {
            const firstEnabled = select.querySelector('option:not([disabled])');
            if (firstEnabled) select.value = firstEnabled.value;
        }

        // Persist model selection on change
        if (!select._changeBound) {
            select._changeBound = true;
            select.addEventListener('change', () => {
                localStorage.setItem('muside_chat_model', select.value);
                updateContextRing();
            });
        }
        // Store model configs for context ring calculation
        select._modelConfigs = models;
        updateContextRing();
    }

    async function saveLLMConfig(config) {
        try {
            const resp = await fetch('/api/llm/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(config)
            });
            if (!resp.ok) throw new Error(`Failed to save config: ${resp.statusText}`);
            return await resp.json();
        } catch (err) {
            showToast('Error saving LLM config: ' + err.message, 'error');
            return null;
        }
    }

    async function showSettingsDialog() {
        try {
        removeSettingsDialog();

        const config = await loadLLMConfig() || {};
        const models = config.models || [];

        const overlay = document.createElement('div');
        overlay.className = 'chat-settings-overlay';
        overlay.id = 'chat-settings-overlay';

        // Store working models data
        let workingModels = JSON.parse(JSON.stringify(models));
        // Ensure api_key_masked is available from server response
        for (const m of workingModels) {
            const serverModel = models.find(sm => sm.name === m.name);
            if (serverModel) m.api_key_masked = serverModel.api_key_masked || '';
        }

        overlay.innerHTML = `
            <div class="chat-settings-dialog" style="max-width:520px">
                <div class="chat-settings-header">
                    <span>⚙️ LLM 模型配置</span>
                    <button class="chat-settings-close" title="Close">✕</button>
                </div>
                <div class="chat-settings-body" id="llm-settings-body">
                    <div class="llm-models-list" id="llm-models-list"></div>
                    <button class="llm-add-model-btn" id="llm-add-model-btn">+ 添加模型</button>
                    <label class="full-width" style="margin-top:4px">
                        <span>全局 System Prompt</span>
                        <textarea id="llm-system-prompt" rows="3" placeholder="You are a helpful coding assistant...">${escapeHTML(config.system_prompt || '')}</textarea>
                    </label>
                </div>
                <div class="chat-settings-footer">
                    <button class="btn-cancel" id="llm-settings-cancel">Cancel</button>
                    <button class="btn-confirm" id="llm-settings-save">Save</button>
                </div>
            </div>
        `;

        document.body.appendChild(overlay);
        settingsDialogEl = overlay;
        injectSettingsStyles();

        // Render model cards
        function renderModelList() {
            const list = overlay.querySelector('#llm-models-list');
            list.innerHTML = '';
            workingModels.forEach((m, idx) => {
                const card = document.createElement('div');
                card.className = 'llm-model-card' + (m.enabled ? ' llm-model-enabled' : '');
                card.innerHTML = `
                    <div class="llm-model-card-header">
                        <div class="llm-model-toggle-area">
                            <button class="llm-enable-btn ${m.enabled ? 'active' : ''}" data-idx="${idx}" title="${m.enabled ? '点击禁用' : '点击启用'}">
                                ${m.enabled ? '✅' : '⬜'}
                            </button>
                            <input class="llm-model-name" data-idx="${idx}" value="${escapeAttr(m.name || '')}" placeholder="模型名称">
                        </div>
                        <div class="llm-model-card-actions">
                            <button class="llm-test-model-btn" data-idx="${idx}" title="测试此模型">🔗</button>
                            <button class="llm-del-model-btn" data-idx="${idx}" title="删除此模型">🗑</button>
                        </div>
                    </div>
                    <div class="llm-model-card-body" style="display:${m.enabled ? '' : 'none'}">
                        <div class="llm-model-fields">
                            <label><span>Provider</span>
                                <select class="llm-provider" data-idx="${idx}">
                                    <option value="openai"${m.provider === 'openai' ? ' selected' : ''}>OpenAI</option>
                                    <option value="anthropic"${m.provider === 'anthropic' ? ' selected' : ''}>Anthropic</option>
                                    <option value="ollama"${m.provider === 'ollama' ? ' selected' : ''}>Ollama</option>
                                    <option value="modelscope"${m.provider === 'modelscope' ? ' selected' : ''}>ModelScope</option>
                                    <option value="custom"${m.provider === 'custom' ? ' selected' : ''}>Custom</option>
                                </select>
                            </label>
                            <label><span>API Type</span>
                                <select class="llm-api-type" data-idx="${idx}">
                                    <option value="openai"${(m.api_type || 'openai') === 'openai' ? ' selected' : ''}>OpenAI Compatible</option>
                                    <option value="azure"${m.api_type === 'azure' ? ' selected' : ''}>Azure OpenAI</option>
                                    <option value="ollama"${m.api_type === 'ollama' ? ' selected' : ''}>Ollama</option>
                                    <option value="custom"${m.api_type === 'custom' ? ' selected' : ''}>Custom</option>
                                </select>
                            </label>
                            <label><span>API Key</span>
                                <input type="password" class="llm-api-key" data-idx="${idx}" placeholder="sk-..." value="${escapeAttr(m.api_key || '')}">
                                ${m.api_key_masked ? `<span class="hint">Current: ${escapeHTML(m.api_key_masked)}</span>` : ''}
                            </label>
                            <label><span>API Base URL</span>
                                <input type="text" class="llm-api-base" data-idx="${idx}" placeholder="https://api.openai.com/v1" value="${escapeAttr(m.api_base || '')}">
                            </label>
                            <label><span>Model</span>
                                <input type="text" class="llm-model" data-idx="${idx}" placeholder="gpt-4o-mini" value="${escapeAttr(m.model || '')}">
                            </label>
                            <div class="llm-model-params">
                                <label><span>Temperature</span>
                                    <input type="number" class="llm-temperature" data-idx="${idx}" min="0" max="2" step="0.1" value="${m.temperature !== undefined ? m.temperature : '0.7'}">
                                </label>
                                <label><span>Max Tokens</span>
                                    <input type="number" class="llm-max-tokens" data-idx="${idx}" min="256" max="200000" step="256" value="${m.max_tokens || '100000'}">
                                </label>
                                <label><span>最大上下文</span>
                                    <input type="number" class="llm-max-context" data-idx="${idx}" min="1024" max="2000000" step="1024" value="${m.max_context || '128000'}">
                                </label>
                                <label class="llm-reasoning-label"><span>推理模式</span>
                                    <input type="checkbox" class="llm-reasoning" data-idx="${idx}" ${m.reasoning !== false ? 'checked' : ''}>
                                </label>
                            </div>
                        </div>
                    </div>
                `;
                list.appendChild(card);
            });

            // Bind events
            list.querySelectorAll('.llm-enable-btn').forEach(btn => {
                btn.addEventListener('click', () => {
                    const idx = parseInt(btn.dataset.idx);
                    workingModels[idx].enabled = !workingModels[idx].enabled;
                    renderModelList();
                });
            });
            list.querySelectorAll('.llm-del-model-btn').forEach(btn => {
                btn.addEventListener('click', () => {
                    const idx = parseInt(btn.dataset.idx);
                    workingModels.splice(idx, 1);
                    renderModelList();
                });
            });
            list.querySelectorAll('.llm-test-model-btn').forEach(btn => {
                btn.addEventListener('click', async () => {
                    const idx = parseInt(btn.dataset.idx);
                    btn.textContent = '⏳';
                    btn.disabled = true;
                    try {
                        // Sync latest field values before testing
                        syncFieldsFromDOM();
                        // Save current models first so test endpoint can read them
                        const saveConfig = { models: workingModels, system_prompt: overlay.querySelector('#llm-system-prompt').value.trim() };
                        await saveLLMConfig(saveConfig);
                        const resp = await fetch('/api/llm/test', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ model_index: idx })
                        });
                        let data;
                        try { data = await resp.json(); } catch {
                            const text = await resp.text().catch(() => '');
                            throw new Error(text ? `Server error: ${text.substring(0, 200)}` : 'Invalid server response');
                        }
                        if (data.ok) {
                            let msg = `✅ ${workingModels[idx].name} 连接成功: ${data.model || ''}`;
                            if (data.tokens) msg += ` (${data.tokens} tokens)`;
                            if (data.reply) msg += `\n💬 ${data.reply}`;
                            if (data.warning) msg += `\n⚠️ ${data.warning}`;
                            showToast(msg, data.warning ? 'warning' : 'success', 5000);
                        } else {
                            showToast(`❌ ${workingModels[idx].name} 连接失败: ${data.error || 'Unknown'}`, 'error', 5000);
                        }
                    } catch (err) {
                        showToast('❌ ' + err.message, 'error');
                    } finally {
                        btn.textContent = '🔗';
                        btn.disabled = false;
                    }
                });
            });
            // Bind field changes
            list.querySelectorAll('.llm-model-name').forEach(input => {
                input.addEventListener('change', () => { workingModels[parseInt(input.dataset.idx)].name = input.value.trim(); });
            });
            list.querySelectorAll('.llm-provider').forEach(sel => {
                sel.addEventListener('change', () => { workingModels[parseInt(sel.dataset.idx)].provider = sel.value; });
            });
            list.querySelectorAll('.llm-api-type').forEach(sel => {
                sel.addEventListener('change', () => { workingModels[parseInt(sel.dataset.idx)].api_type = sel.value; });
            });
            list.querySelectorAll('.llm-api-key').forEach(input => {
                input.addEventListener('change', () => { workingModels[parseInt(input.dataset.idx)].api_key = input.value; });
            });
            list.querySelectorAll('.llm-api-base').forEach(input => {
                input.addEventListener('change', () => { workingModels[parseInt(input.dataset.idx)].api_base = input.value.trim(); });
            });
            list.querySelectorAll('.llm-model').forEach(input => {
                input.addEventListener('change', () => { workingModels[parseInt(input.dataset.idx)].model = input.value.trim(); });
            });
            list.querySelectorAll('.llm-temperature').forEach(input => {
                input.addEventListener('change', () => { workingModels[parseInt(input.dataset.idx)].temperature = parseFloat(input.value) || 0.7; });
            });
            list.querySelectorAll('.llm-max-tokens').forEach(input => {
                input.addEventListener('change', () => { workingModels[parseInt(input.dataset.idx)].max_tokens = parseInt(input.value, 10) || 100000; });
            });
            list.querySelectorAll('.llm-max-context').forEach(input => {
                input.addEventListener('change', () => { workingModels[parseInt(input.dataset.idx)].max_context = parseInt(input.value, 10) || 128000; });
            });
            list.querySelectorAll('.llm-reasoning').forEach(cb => {
                cb.addEventListener('change', () => { workingModels[parseInt(cb.dataset.idx)].reasoning = cb.checked; });
            });
        }

        // Sync all DOM field values into workingModels (call before save/test)
        function syncFieldsFromDOM() {
            overlay.querySelectorAll('.llm-model-name').forEach(input => {
                workingModels[parseInt(input.dataset.idx)].name = input.value.trim();
            });
            overlay.querySelectorAll('.llm-provider').forEach(sel => {
                workingModels[parseInt(sel.dataset.idx)].provider = sel.value;
            });
            overlay.querySelectorAll('.llm-api-type').forEach(sel => {
                workingModels[parseInt(sel.dataset.idx)].api_type = sel.value;
            });
            overlay.querySelectorAll('.llm-api-key').forEach(input => {
                workingModels[parseInt(input.dataset.idx)].api_key = input.value;
            });
            overlay.querySelectorAll('.llm-api-base').forEach(input => {
                workingModels[parseInt(input.dataset.idx)].api_base = input.value.trim();
            });
            overlay.querySelectorAll('.llm-model').forEach(input => {
                workingModels[parseInt(input.dataset.idx)].model = input.value.trim();
            });
            overlay.querySelectorAll('.llm-temperature').forEach(input => {
                workingModels[parseInt(input.dataset.idx)].temperature = parseFloat(input.value) || 0.7;
            });
            overlay.querySelectorAll('.llm-max-tokens').forEach(input => {
                workingModels[parseInt(input.dataset.idx)].max_tokens = parseInt(input.value, 10) || 100000;
            });
            overlay.querySelectorAll('.llm-max-context').forEach(input => {
                workingModels[parseInt(input.dataset.idx)].max_context = parseInt(input.value, 10) || 128000;
            });
            overlay.querySelectorAll('.llm-reasoning').forEach(cb => {
                workingModels[parseInt(cb.dataset.idx)].reasoning = cb.checked;
            });
        }

        renderModelList();

        // Add model button
        overlay.querySelector('#llm-add-model-btn').addEventListener('click', () => {
            workingModels.push({
                name: '新模型',
                provider: 'openai',
                api_type: 'openai',
                api_key: '',
                api_base: '',
                model: '',
                enabled: false,
                temperature: 0.7,
                max_tokens: 100000,
                max_context: 128000,
                reasoning: true,
            });
            renderModelList();
        });

        // Close / Cancel / Save
        overlay.querySelector('.chat-settings-close').addEventListener('click', removeSettingsDialog);
        overlay.querySelector('#llm-settings-cancel').addEventListener('click', removeSettingsDialog);
        overlay.querySelector('#llm-settings-save').addEventListener('click', async () => {
            // Sync latest field values from DOM
            syncFieldsFromDOM();

            const newConfig = {
                models: workingModels,
                system_prompt: overlay.querySelector('#llm-system-prompt').value.trim()
            };
            const result = await saveLLMConfig(newConfig);
            if (result) {
                const enabledCount = workingModels.filter(m => m.enabled).length;
                showToast(`已保存 ${workingModels.length} 个模型配置 (${enabledCount} 个启用)`, 'success');
                removeSettingsDialog();
                refreshModelSelector();
            }
        });

        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) removeSettingsDialog();
        });

        const firstInput = overlay.querySelector('input, select, textarea');
        if (firstInput) setTimeout(() => firstInput.focus(), 100);
        } catch (err) {
            console.error('ChatManager: showSettingsDialog error:', err);
            throw err;
        }
    }

    function removeSettingsDialog() {
        if (settingsDialogEl) {
            settingsDialogEl.remove();
            settingsDialogEl = null;
        }
    }

    // ── Inject Styles ──────────────────────────────────────────────

    let _settingsStylesInjected = false;
    // IMPORTANT: This function injects ALL chat UI styles (not just settings),
    // including todo panel, tool messages, reasoning blocks, markdown, etc.
    // It MUST be called early (in init()) so styles are available before any UI is rendered.
    function injectSettingsStyles() {
        if (_settingsStylesInjected) return;
        _settingsStylesInjected = true;

        const style = document.createElement('style');
        style.textContent = `
            /* ── Settings Dialog ── */
            .chat-settings-overlay {
                position: fixed;
                inset: 0;
                background: rgba(0, 0, 0, 0.6);
                z-index: 500;
                display: flex;
                align-items: center;
                justify-content: center;
                padding: 16px;
                animation: fadeIn 0.2s ease;
            }
            @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }

            .chat-settings-dialog {
                background: var(--bg-surface);
                border: 1px solid var(--border);
                border-radius: var(--radius);
                width: 100%;
                max-width: 380px;
                max-height: 85vh;
                display: flex;
                flex-direction: column;
                box-shadow: 0 16px 48px rgba(0,0,0,0.5);
                overflow: hidden;
            }
            .chat-settings-header {
                display: flex;
                align-items: center;
                justify-content: space-between;
                padding: 14px 16px 8px;
                font-weight: 600;
                font-size: 15px;
                color: var(--text-primary);
                flex-shrink: 0;
            }
            .chat-settings-close {
                width: 28px;
                height: 28px;
                border: none;
                background: none;
                color: var(--text-muted);
                font-size: 16px;
                cursor: pointer;
                border-radius: var(--radius-sm);
                display: flex;
                align-items: center;
                justify-content: center;
            }
            .chat-settings-close:active { background: var(--bg-hover); }
            .chat-settings-body {
                padding: 8px 16px 14px;
                overflow-y: auto;
                flex: 1;
                display: flex;
                flex-direction: column;
                gap: 10px;
            }
            .chat-settings-body label {
                display: flex;
                flex-direction: column;
                gap: 4px;
                font-size: 12px;
                color: var(--text-secondary);
            }
            .chat-settings-body label.full-width { flex: 1; }
            .chat-settings-body label span {
                font-weight: 500;
                color: var(--text-primary);
                font-size: 12px;
            }
            .chat-settings-body label .hint {
                font-size: 10px;
                color: var(--text-muted);
                font-style: italic;
            }
            .chat-settings-body input,
            .chat-settings-body select,
            .chat-settings-body textarea {
                width: 100%;
                padding: 8px 10px;
                border: 1px solid var(--border);
                background: var(--bg-tertiary);
                color: var(--text-primary);
                border-radius: var(--radius-sm);
                font-size: 13px;
                font-family: var(--font-mono);
            }
            .chat-settings-body input:focus,
            .chat-settings-body select:focus,
            .chat-settings-body textarea:focus {
                border-color: var(--accent);
                outline: none;
            }
            .chat-settings-body textarea {
                resize: vertical;
                min-height: 80px;
            }
            .chat-settings-footer {
                display: flex;
                justify-content: flex-end;
                gap: 8px;
                padding: 8px 16px 14px;
                flex-shrink: 0;
            }
            .chat-settings-footer button {
                padding: 8px 16px;
                border: none;
                border-radius: var(--radius-sm);
                cursor: pointer;
                font-size: 13px;
                font-weight: 500;
            }
            .chat-settings-footer .btn-cancel {
                background: var(--bg-hover);
                color: var(--text-secondary);
            }
            .chat-settings-footer .btn-cancel:active { background: var(--bg-active); }
            .chat-settings-footer .btn-confirm {
                background: var(--accent);
                color: var(--bg-primary);
            }
            .chat-settings-footer .btn-confirm:active { background: var(--accent-hover); }

            /* ── Multi-Model Card Styles ── */
            .llm-models-list {
                display: flex;
                flex-direction: column;
                gap: 8px;
            }
            .llm-model-card {
                border: 1px solid var(--border);
                border-radius: var(--radius-sm);
                background: var(--bg-tertiary);
                overflow: hidden;
                transition: border-color 0.15s;
            }
            .llm-model-enabled {
                border-color: var(--accent);
                border-width: 1.5px;
            }
            .llm-model-card-header {
                display: flex;
                align-items: center;
                justify-content: space-between;
                padding: 8px 10px;
                gap: 8px;
            }
            .llm-model-toggle-area {
                display: flex;
                align-items: center;
                gap: 8px;
                flex: 1;
                min-width: 0;
            }
            .llm-enable-btn {
                border: none;
                background: none;
                cursor: pointer;
                font-size: 16px;
                padding: 0;
                line-height: 1;
                flex-shrink: 0;
            }
            .llm-model-name {
                flex: 1;
                min-width: 0;
                border: none;
                background: transparent;
                color: var(--text-primary);
                font-size: 13px;
                font-weight: 600;
                padding: 2px 4px;
                border-bottom: 1px solid transparent;
                font-family: var(--font-mono);
            }
            .llm-model-name:focus {
                outline: none;
                border-bottom-color: var(--accent);
            }
            .llm-model-card-actions {
                display: flex;
                gap: 4px;
                flex-shrink: 0;
            }
            .llm-model-card-actions button {
                border: none;
                background: none;
                cursor: pointer;
                font-size: 14px;
                padding: 2px 4px;
                border-radius: var(--radius-sm);
                opacity: 0.6;
            }
            .llm-model-card-actions button:hover { opacity: 1; }
            .llm-model-card-actions button:active { background: var(--bg-hover); }
            .llm-model-card-body {
                padding: 0 10px 10px;
            }
            .llm-model-fields {
                display: flex;
                flex-direction: column;
                gap: 8px;
            }
            .llm-model-params {
                display: flex;
                gap: 8px;
                flex-wrap: wrap;
            }
            .llm-model-params label {
                flex: 1;
                min-width: 120px;
            }
            .llm-reasoning-label {
                display: flex;
                align-items: center;
                gap: 8px;
            }
            .llm-reasoning-label > span:first-child {
                white-space: nowrap;
                font-size: 11px;
            }
            .llm-reasoning-label input[type="checkbox"] {
                appearance: none;
                -webkit-appearance: none;
                width: 16px;
                height: 16px;
                border: 2px solid var(--border);
                border-radius: 3px;
                flex-shrink: 0;
                cursor: pointer;
                position: relative;
                transition: 0.15s;
            }
            .llm-reasoning-label input[type="checkbox"]:checked {
                background: var(--accent);
                border-color: var(--accent);
            }
            .llm-reasoning-label input[type="checkbox"]:checked::after {
                content: '';
                position: absolute;
                left: 4px;
                top: 1px;
                width: 5px;
                height: 9px;
                border: solid white;
                border-width: 0 2px 2px 0;
                transform: rotate(45deg);
            }
            .llm-add-model-btn {
                width: 100%;
                padding: 8px;
                border: 1px dashed var(--border);
                border-radius: var(--radius-sm);
                background: none;
                color: var(--text-secondary);
                font-size: 12px;
                cursor: pointer;
                font-family: var(--font-mono);
            }
            .llm-add-model-btn:hover { background: var(--bg-hover); color: var(--text-primary); }
            .llm-add-model-btn:active { background: var(--bg-active); }
            .llm-test-model-btn:disabled { opacity: 0.4; }

            /* ── Code Block Wrapper ── */
            .code-block-wrapper {
                position: relative;
                margin: 6px 0;
                border-radius: var(--radius-sm);
                overflow: hidden;
            }
            .code-block-wrapper pre {
                margin: 0 !important;
                border-radius: var(--radius-sm) !important;
            }
            .code-block-wrapper .code-lang {
                position: absolute;
                top: 4px;
                left: 8px;
                font-size: 10px;
                color: var(--text-muted);
                font-family: var(--font-mono);
                z-index: 1;
                pointer-events: none;
            }
            .code-copy-btn {
                position: absolute;
                top: 4px;
                right: 4px;
                border: none;
                background: var(--bg-active);
                color: var(--text-secondary);
                font-size: 12px;
                padding: 2px 6px;
                border-radius: var(--radius-sm);
                cursor: pointer;
                opacity: 0.7;
                z-index: 1;
                line-height: 1;
            }
            .code-copy-btn:hover { opacity: 1; }
            .code-copy-btn:active { transform: scale(0.9); }

            /* ── Message Copy Button ── */
            .msg-copy-btn {
                display: inline-flex;
                align-items: center;
                gap: 3px;
                border: none;
                background: transparent;
                color: var(--text-muted);
                font-size: 11px;
                padding: 2px 6px;
                border-radius: var(--radius-sm);
                cursor: pointer;
                margin-top: 4px;
                opacity: 0.6;
                transition: opacity 0.15s;
            }
            .msg-copy-btn:hover { opacity: 1; color: var(--text-secondary); }
            .msg-copy-btn:active { transform: scale(0.95); }

            /* ── Tool Messages ── */
            .tool-header {
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 8px;
            }
            .tool-name {
                font-weight: 500;
                font-size: 12px;
            }
            .tool-status {
                font-size: 12px;
                font-weight: bold;
            }
            .tool-ok { color: var(--green); }
            .tool-fail { color: var(--red); }
            .tool-duration {
                font-size: 10px;
                color: var(--text-muted);
                font-family: var(--font-mono);
            }
            .tool-spinner {
                animation: spin 1s linear infinite;
                font-size: 14px;
            }
            @keyframes spin {
                from { transform: rotate(0deg); }
                to { transform: rotate(360deg); }
            }
            .tool-args {
                font-family: var(--font-mono);
                font-size: 11px;
                color: var(--text-muted);
                margin-top: 4px;
                padding: 4px 6px;
                background: rgba(0,0,0,0.2);
                border-radius: var(--radius-sm);
                word-break: break-all;
                cursor: default;
            }
            .tool-args.collapsed {
                max-height: 40px;
                overflow: hidden;
                position: relative;
            }
            .tool-args.expanded {
                max-height: none;
            }
            .tool-args.collapsed::after {
                content: '...';
                position: absolute;
                bottom: 0;
                right: 4px;
                background: linear-gradient(transparent, rgba(0,0,0,0.3));
                padding-left: 20px;
            }
            .tool-result {
                font-family: var(--font-mono);
                font-size: 11px;
                color: var(--text-secondary);
                margin-top: 4px;
                padding: 6px 8px;
                background: rgba(0,0,0,0.15);
                border-radius: var(--radius-sm);
                white-space: pre-wrap;
                word-break: break-word;
                max-height: 300px;
                overflow-y: auto;
            }
            .tool-result.tool-waiting {
                color: var(--text-muted);
                font-style: italic;
            }
            .tool-toggle-btn {
                display: inline-block;
                margin-top: 4px;
                padding: 2px 8px;
                border: none;
                background: var(--bg-hover);
                color: var(--accent);
                font-size: 11px;
                cursor: pointer;
                border-radius: var(--radius-sm);
            }
            .tool-toggle-btn:hover { background: var(--bg-active); }
            .tool-toggle-btn:active { transform: scale(0.95); }
            .tool-progress {
                border-left: 2px solid var(--accent);
            }

            /* ── Reasoning / Thinking Block ── */
            .reasoning-block {
                padding: 4px 10px;
                margin: 2px 0;
            }
            .reasoning-header {
                display: flex;
                align-items: center;
                gap: 6px;
                padding: 4px 6px;
                cursor: pointer;
                border-radius: var(--radius-sm);
                user-select: none;
                -webkit-user-select: none;
                transition: background 0.15s;
            }
            .reasoning-header:hover {
                background: var(--bg-hover);
            }
            .reasoning-header:active {
                background: var(--bg-active);
            }
            .reasoning-icon {
                font-size: 13px;
                flex-shrink: 0;
            }
            .reasoning-title {
                font-size: 11px;
                color: var(--text-muted);
                flex: 1;
            }
            .reasoning-toggle {
                font-size: 11px;
                color: var(--text-muted);
                flex-shrink: 0;
                width: 14px;
                text-align: center;
            }
            .reasoning-body {
                padding: 6px 8px 6px 24px;
                font-family: var(--font-mono);
                font-size: 11px;
                color: var(--text-secondary);
                line-height: 1.5;
                white-space: pre-wrap;
                word-break: break-word;
                max-height: 400px;
                overflow-y: auto;
                -webkit-overflow-scrolling: touch;
            }

            /* ── Todo Panel ── */
            .todo-panel {
                background: var(--surface-0);
                border: 1px solid var(--border-subtle);
                border-radius: var(--radius);
                margin: 6px 8px;
                overflow: hidden;
                font-size: 12px;
            }
            .todo-panel-header {
                display: flex;
                flex-direction: column;
                gap: 4px;
                padding: 8px 10px;
                user-select: none;
                -webkit-user-select: none;
                background: var(--surface-1);
                border-bottom: 1px solid var(--border-subtle);
            }
            .todo-panel-header-row {
                display: flex;
                align-items: center;
                justify-content: space-between;
                cursor: pointer;
            }
            .todo-toggle-btn {
                background: none;
                border: 1px solid var(--border-subtle);
                border-radius: 4px;
                color: var(--text-muted);
                font-size: 12px;
                cursor: pointer;
                padding: 2px 6px;
                line-height: 1;
                transition: background 0.15s, color 0.15s;
            }
            .todo-toggle-btn:hover {
                background: var(--surface-2);
                color: var(--text-primary);
            }
            .todo-panel-title {
                font-weight: 600;
                font-size: 12px;
                color: var(--text-primary);
            }
            .todo-panel-progress {
                font-size: 11px;
                color: var(--text-muted);
                font-family: var(--font-mono);
            }
            .todo-panel-bar {
                height: 4px;
                background: var(--surface-2);
                border-radius: 2px;
                overflow: hidden;
            }
            .todo-panel-bar-fill {
                height: 100%;
                background: var(--accent);
                border-radius: 2px;
                transition: width 0.3s ease;
            }
            .todo-panel-body {
                padding: 6px 10px;
                max-height: 300px;
                overflow-y: auto;
                -webkit-overflow-scrolling: touch;
                transition: max-height 0.25s ease, padding 0.25s ease;
            }
            .todo-panel-body.collapsed {
                max-height: 0 !important;
                padding-top: 0 !important;
                padding-bottom: 0 !important;
                overflow: hidden;
            }
            .todo-item {
                padding: 4px 2px;
                line-height: 1.5;
                border-bottom: 1px solid var(--border-subtle);
                color: var(--text-secondary);
                font-size: 12px;
            }
            .todo-item:last-child {
                border-bottom: none;
            }
            .todo-item.todo-completed {
                opacity: 0.6;
                text-decoration: line-through;
            }
            .todo-item.todo-inprogress {
                color: var(--accent);
                font-weight: 500;
            }

            /* ── Chat Role Badge ── */
            .chat-role-badge {
                font-size: 10px;
                color: var(--mauve);
                margin-bottom: 4px;
                font-weight: 500;
            }

            /* ── Turn Indicator ── */
            .chat-turn-indicator {
                font-size: 10px;
                color: var(--text-muted);
                text-align: center;
                padding: 4px 8px;
                font-family: var(--font-mono);
            }

            /* ── Stop Button ── */
            .chat-stop-btn {
                width: 100%;
                padding: 8px 0;
                border: 1px solid var(--red);
                background: rgba(255, 59, 48, 0.1);
                color: var(--red);
                font-size: 13px;
                font-weight: 600;
                cursor: pointer;
                border-radius: var(--radius-sm);
                margin-bottom: 6px;
                transition: background 0.15s ease;
            }
            .chat-stop-btn:hover { background: rgba(255, 59, 48, 0.2); }
            .chat-stop-btn:active { background: rgba(255, 59, 48, 0.3); transform: scale(0.98); }

            /* ── Retry Button ── */
            .chat-retry-btn {
                display: inline-flex;
                align-items: center;
                gap: 4px;
                border: none;
                background: none;
                color: var(--accent);
                font-size: 12px;
                cursor: pointer;
                padding: 4px 8px;
                border-radius: var(--radius-sm);
                margin-top: 6px;
            }
            .chat-retry-btn:hover { background: var(--bg-hover); }
            .chat-retry-btn:active { background: var(--bg-active); transform: scale(0.95); }

            /* ── System Message ── */
            .chat-system-msg {
                font-size: 12px;
                color: var(--text-muted);
                font-style: italic;
            }

            /* ── Streaming ── */
            .chat-streaming {
                min-height: 1em;
            }
            .chat-streaming::after {
                content: '▊';
                animation: blink 0.8s steps(2) infinite;
                color: var(--accent);
                font-size: 0.9em;
            }
            @keyframes blink {
                0%, 100% { opacity: 1; }
                50% { opacity: 0; }
            }

            /* ── Markdown headings ── */
            .md-h1 { font-size: 16px; display: block; margin: 8px 0 4px; }
            .md-h2 { font-size: 14px; display: block; margin: 6px 0 3px; }
            .md-h3 { font-size: 13px; display: block; margin: 4px 0 2px; }

            /* ── Markdown links ── */
            .chat-content a,
            .chat-msg a {
                color: var(--accent);
                text-decoration: underline;
                text-underline-offset: 2px;
            }
            .chat-content a:hover,
            .chat-msg a:hover {
                opacity: 0.85;
            }

            /* ── Markdown blockquote ── */
            blockquote {
                border-left: 3px solid var(--accent);
                margin: 6px 0;
                padding: 4px 10px;
                color: var(--text-muted);
                font-style: italic;
            }

            /* ── Markdown hr ── */
            hr {
                border: none;
                border-top: 1px solid var(--border);
                margin: 8px 0;
            }

            /* ── Re-send button ── */
            .chat-resend-btn {
                display: inline-flex;
                align-items: center;
                gap: 4px;
                border: none;
                background: none;
                color: var(--text-muted);
                font-size: 11px;
                cursor: pointer;
                padding: 2px 6px;
                border-radius: var(--radius-sm);
                margin-top: 4px;
            }
            .chat-resend-btn:hover { color: var(--accent); }
            .chat-resend-btn:active { background: var(--bg-hover); }

            /* ── Mode Toggle ── */
            .chat-mode-toggle {
                display: flex;
                align-items: center;
                gap: 6px;
                margin: 4px 0;
                flex-shrink: 0;
            }
            .mode-label {
                color: var(--text-muted);
                font-size: 11px;
                font-weight: 500;
                white-space: nowrap;
            }
            .mode-select {
                padding: 3px 8px;
                border: 1px solid var(--border);
                border-radius: 6px;
                background: var(--bg-tertiary);
                color: var(--text-primary);
                font-size: 11px;
                font-weight: 500;
                cursor: pointer;
                outline: none;
                transition: border-color 0.2s;
            }
            .mode-select:focus {
                border-color: var(--accent);
            }

            /* ── Plan Actions ── */
            .plan-actions {
                display: flex;
                gap: 8px;
                margin-top: 8px;
                flex-wrap: wrap;
            }
            .plan-btn {
                padding: 6px 14px;
                border: none;
                border-radius: var(--radius-sm);
                font-size: 12px;
                font-weight: 500;
                cursor: pointer;
                transition: all 0.15s ease;
                white-space: nowrap;
            }
            .plan-btn:active { transform: scale(0.96); }
            .plan-btn-edit {
                background: var(--bg-hover);
                color: var(--text-secondary);
                border: 1px solid var(--border);
            }
            .plan-btn-edit:hover { background: var(--bg-active); color: var(--text-primary); }
            .plan-btn-approve {
                background: var(--green);
                color: #fff;
            }
            .plan-btn-approve:hover { opacity: 0.85; }
            .plan-btn-save {
                background: var(--accent);
                color: var(--bg-primary);
            }
            .plan-btn-save:hover { opacity: 0.85; }
            .plan-btn-cancel {
                background: var(--bg-hover);
                color: var(--text-muted);
                border: 1px solid var(--border);
            }
            .plan-btn-cancel:hover { background: var(--bg-active); }

            /* ── Plan Editor ── */
            .plan-edit-area {
                margin-top: 8px;
            }
            .plan-textarea {
                width: 100%;
                min-height: 150px;
                padding: 8px 10px;
                border: 1px solid var(--border);
                background: var(--bg-tertiary);
                color: var(--text-primary);
                border-radius: var(--radius-sm);
                font-size: 12px;
                font-family: var(--font-mono);
                resize: vertical;
                line-height: 1.5;
                box-sizing: border-box;
            }
            .plan-textarea:focus {
                border-color: var(--accent);
                outline: none;
            }
            .plan-editor-btns {
                display: flex;
                gap: 8px;
                margin-top: 6px;
            }

            /* ── Conversation History Dialog ── */
            .conv-empty {
                text-align: center;
                color: var(--text-muted);
                padding: 40px 16px;
                font-size: 13px;
            }
            .conv-item {
                display: flex;
                align-items: center;
                justify-content: space-between;
                padding: 10px 12px;
                border-bottom: 1px solid var(--border);
                gap: 8px;
            }
            .conv-item:last-child { border-bottom: none; }
            .conv-info {
                flex: 1;
                min-width: 0;
            }
            .conv-title {
                font-size: 13px;
                font-weight: 500;
                color: var(--text-primary);
                white-space: nowrap;
                overflow: hidden;
                text-overflow: ellipsis;
            }
            .conv-meta {
                font-size: 11px;
                color: var(--text-muted);
                margin-top: 2px;
            }
            .conv-actions {
                display: flex;
                gap: 4px;
                flex-shrink: 0;
            }
            .conv-actions button {
                border: none;
                background: none;
                cursor: pointer;
                font-size: 14px;
                padding: 4px 6px;
                border-radius: var(--radius-sm);
                opacity: 0.7;
            }
            .conv-actions button:hover { opacity: 1; }
            .conv-actions button:active { background: var(--bg-hover); }
            .conv-delete-btn:hover { color: var(--red); }
        `;
        document.head.appendChild(style);
    }

    // ── Input Auto-Resize ──────────────────────────────────────────

    function autoResizeInput() {
        const input = document.getElementById('chat-input');
        if (!input) return;
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    }

    // ── Plan Mode Actions ──────────────────────────────────────────

    /**
     * Inject plan action buttons (Edit Plan / Approve & Execute) below a plan message
     */
    function injectPlanActions(msgEl, planMarkdown) {
        // Remove any existing plan actions
        const existing = msgEl.querySelector('.plan-actions');
        if (existing) existing.remove();

        // Also remove any existing textarea for editing
        const existingTa = msgEl.querySelector('.plan-edit-area');
        if (existingTa) existingTa.remove();

        const actionsDiv = document.createElement('div');
        actionsDiv.className = 'plan-actions';

        const editBtn = document.createElement('button');
        editBtn.className = 'plan-btn plan-btn-edit';
        editBtn.textContent = '✏️ 修改计划';
        editBtn.addEventListener('click', () => {
            showPlanEditor(msgEl, planContent);
        });

        const approveBtn = document.createElement('button');
        approveBtn.className = 'plan-btn plan-btn-approve';
        approveBtn.textContent = '✅ 批准并执行';
        approveBtn.addEventListener('click', () => {
            // Get the current plan content (may have been edited)
            const editTa = msgEl.querySelector('.plan-edit-area textarea');
            const currentPlan = editTa ? editTa.value : planContent;
            // Remove plan action buttons
            const actions = msgEl.querySelector('.plan-actions');
            if (actions) actions.remove();
            // Remove editor if present
            const editor = msgEl.querySelector('.plan-edit-area');
            if (editor) editor.remove();
            // Switch to execute mode and send the plan
            chatMode = 'execute';
            localStorage.setItem('muside_chat_mode', chatMode);
            updateModeToggleUI();
            addMessage('system', '📋 计划已批准，开始执行...');
            sendMessage('Please execute the following plan:\n\n' + currentPlan);
        });

        actionsDiv.appendChild(editBtn);
        actionsDiv.appendChild(approveBtn);

        // Insert before the timestamp
        const timeEl = msgEl.querySelector('.chat-time');
        if (timeEl) {
            msgEl.insertBefore(actionsDiv, timeEl);
        } else {
            msgEl.appendChild(actionsDiv);
        }

        forceScrollToBottom();
    }

    /**
     * Show a textarea editor for the plan content inside the message element
     */
    function showPlanEditor(msgEl, markdown) {
        // Remove existing editor if any
        const existingTa = msgEl.querySelector('.plan-edit-area');
        if (existingTa) existingTa.remove();

        // Hide the rendered content temporarily
        const contentEl = msgEl.querySelector('.chat-content');
        if (contentEl) contentEl.style.display = 'none';

        // Hide action buttons
        const actions = msgEl.querySelector('.plan-actions');
        if (actions) actions.style.display = 'none';

        const editorDiv = document.createElement('div');
        editorDiv.className = 'plan-edit-area';

        const textarea = document.createElement('textarea');
        textarea.className = 'plan-textarea';
        textarea.value = markdown || '';
        textarea.placeholder = 'Edit the plan...';
        textarea.rows = 12;

        const editorBtns = document.createElement('div');
        editorBtns.className = 'plan-editor-btns';

        const saveBtn = document.createElement('button');
        saveBtn.className = 'plan-btn plan-btn-save';
        saveBtn.textContent = '💾 保存修改';
        saveBtn.addEventListener('click', () => {
            planContent = textarea.value;
            // Update the rendered content
            if (contentEl) {
                contentEl.innerHTML = renderMarkdownLite(planContent);
                contentEl.style.display = '';
                bindCopyButtons(contentEl);
            }
            editorDiv.remove();
            // Show actions again
            if (actions) actions.style.display = '';
            // Re-inject plan actions with updated content
            injectPlanActions(msgEl, planContent);
        });

        const cancelBtn = document.createElement('button');
        cancelBtn.className = 'plan-btn plan-btn-cancel';
        cancelBtn.textContent = '❌ 取消';
        cancelBtn.addEventListener('click', () => {
            if (contentEl) contentEl.style.display = '';
            editorDiv.remove();
            if (actions) actions.style.display = '';
        });

        editorBtns.appendChild(saveBtn);
        editorBtns.appendChild(cancelBtn);

        editorDiv.appendChild(textarea);
        editorDiv.appendChild(editorBtns);

        // Insert before the actions
        if (actions) {
            msgEl.insertBefore(editorDiv, actions);
        } else {
            const timeEl = msgEl.querySelector('.chat-time');
            if (timeEl) {
                msgEl.insertBefore(editorDiv, timeEl);
            } else {
                msgEl.appendChild(editorDiv);
            }
        }

        forceScrollToBottom();
    }

    // ── Conversation Management ────────────────────────────────────

    let historyDialogEl = null;

    async function loadConversation(convId) {
        try {
            const resp = await fetch(`/api/conversations/${convId}`);
            if (!resp.ok) throw new Error(`Failed to load conversation: ${resp.statusText}`);
            const conv = await resp.json();
            currentConvId = conv.id;
            messages = conv.messages || [];
            renderMessages(messages);
            clearBackup(); // Clear any stale backup when loading a saved conversation
            updateContextRing();
            showToast('已加载会话: ' + truncate(conv.title || 'New Chat', 30), 'success');
        } catch (err) {
            showToast('加载会话失败: ' + err.message, 'error');
        }
    }

    async function deleteConversation(convId) {
        try {
            const resp = await fetch(`/api/conversations/${convId}`, { method: 'DELETE' });
            if (!resp.ok) throw new Error(`Failed: ${resp.statusText}`);
            await resp.json();
            // If deleted the current conversation, clear
            if (currentConvId === convId) {
                currentConvId = null;
                messages = [];
                renderMessages([]);
            }
            showToast('会话已删除', 'success');
            // Refresh the list if dialog is open
            if (historyDialogEl) {
                historyDialogEl.remove();
                historyDialogEl = null;
                showHistoryDialog();
            }
        } catch (err) {
            showToast('删除失败: ' + err.message, 'error');
        }
    }

    async function showHistoryDialog() {
        if (historyDialogEl) {
            historyDialogEl.remove();
            historyDialogEl = null;
            return;
        }
        try {
            const resp = await fetch('/api/conversations');
            const data = await resp.json();
            const convs = data.conversations || [];

            const overlay = document.createElement('div');
            overlay.className = 'chat-settings-overlay';
            overlay.id = 'chat-history-overlay';

            let listHTML = '';
            if (convs.length === 0) {
                listHTML = '<div class="conv-empty">暂无历史会话</div>';
            } else {
                for (const c of convs) {
                    const dateStr = c.updated_at ? formatTime(c.updated_at) : '';
                    listHTML += `
                        <div class="conv-item" data-id="${escapeAttr(c.id)}">
                            <div class="conv-info">
                                <div class="conv-title">${escapeHTML(c.title || 'New Chat')}</div>
                                <div class="conv-meta">${dateStr} · ${c.message_count} 条消息</div>
                            </div>
                            <div class="conv-actions">
                                <button class="conv-continue-btn" data-id="${escapeAttr(c.id)}" title="继续此会话">▶</button>
                                <button class="conv-delete-btn" data-id="${escapeAttr(c.id)}" title="删除此会话">🗑</button>
                            </div>
                        </div>`;
                }
            }

            overlay.innerHTML = `
                <div class="chat-settings-dialog" style="max-width:420px">
                    <div class="chat-settings-header">
                        <span>📋 历史会话</span>
                        <button class="chat-settings-close" title="Close">✕</button>
                    </div>
                    <div class="chat-settings-body" id="conv-list-body" style="max-height:60vh">
                        ${listHTML}
                    </div>
                </div>
            `;

            document.body.appendChild(overlay);
            historyDialogEl = overlay;
            injectSettingsStyles();

            // Bind close
            overlay.querySelector('.chat-settings-close').addEventListener('click', () => {
                overlay.remove();
                historyDialogEl = null;
            });
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) {
                    overlay.remove();
                    historyDialogEl = null;
                }
            });

            // Bind continue buttons
            overlay.querySelectorAll('.conv-continue-btn').forEach(btn => {
                btn.addEventListener('click', async () => {
                    const id = btn.dataset.id;
                    overlay.remove();
                    historyDialogEl = null;
                    await loadConversation(id);
                });
            });

            // Bind delete buttons
            overlay.querySelectorAll('.conv-delete-btn').forEach(btn => {
                btn.addEventListener('click', () => {
                    const id = btn.dataset.id;
                    deleteConversation(id);
                });
            });

        } catch (err) {
            showToast('加载历史会话失败: ' + err.message, 'error');
        }
    }

    // ── Wire Up Events ─────────────────────────────────────────────

    function wireEvents() {
        // Send button
        const sendBtn = document.getElementById('chat-send');
        if (sendBtn) {
            sendBtn.addEventListener('click', (e) => {
                e.preventDefault();
                sendMessage();
            });
        }

        // Chat input
        const input = document.getElementById('chat-input');
        if (input) {
            // Restore persisted input text
            const savedInput = localStorage.getItem('muside_chat_input');
            if (savedInput) {
                input.value = savedInput;
                autoResizeInput();
            }
            input.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
                    e.preventDefault();
                    sendMessage();
                }
            });
            input.addEventListener('input', () => {
                autoResizeInput();
                // Persist input text for crash/recovery
                const val = input.value;
                if (val.trim()) {
                    localStorage.setItem('muside_chat_input', val);
                } else {
                    localStorage.removeItem('muside_chat_input');
                }
            });
        }

        // New chat button
        const newChatBtn = document.getElementById('chat-new-chat');
        if (newChatBtn) {
            newChatBtn.addEventListener('click', (e) => {
                e.preventDefault();
                newChat();
            });
        }

        // History button (replaces clear button)
        const historyBtn = document.getElementById('chat-history-btn');
        if (historyBtn) {
            historyBtn.addEventListener('click', (e) => {
                e.preventDefault();
                showHistoryDialog();
            });
        }

        // ── Chat Search ──────────────────────────────────────────────
        const searchBtn = document.getElementById('chat-search-btn');
        const searchBar = document.getElementById('chat-search-bar');
        const searchInput = document.getElementById('chat-search-input');
        const searchCount = document.getElementById('chat-search-count');
        const searchPrev = document.getElementById('chat-search-prev');
        const searchNext = document.getElementById('chat-search-next');
        const searchClose = document.getElementById('chat-search-close');

        if (searchBtn) {
            searchBtn.addEventListener('click', (e) => {
                e.preventDefault();
                toggleSearchBar();
            });
        }

        // Settings button (LLM settings dialog)
        const settingsBtn = document.getElementById('chat-settings-btn');
        if (settingsBtn) {
            settingsBtn.addEventListener('click', (e) => {
                e.preventDefault();
                showSettingsDialog().catch(err => {
                    console.error('ChatManager: settings dialog error:', err);
                });
            });
        }

        function toggleSearchBar() {
            if (searchVisible) {
                closeSearch();
            } else {
                openSearch();
            }
        }

        function openSearch() {
            searchVisible = true;
            searchBar.classList.remove('hidden');
            searchInput.value = '';
            searchCount.textContent = '';
            searchPrev.disabled = true;
            searchNext.disabled = true;
            clearHighlights();
            searchMatches = [];          // reset stale entries
            searchCurrentIndex = -1;
            searchInput.focus();
        }

        function closeSearch() {
            searchVisible = false;
            searchBar.classList.add('hidden');
            clearHighlights();
            searchMatches = [];
            searchCurrentIndex = -1;
        }

        if (searchClose) {
            searchClose.addEventListener('click', (e) => {
                e.preventDefault();
                closeSearch();
            });
        }

        // Search on Enter key
        if (searchInput) {
            searchInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    if (e.shiftKey) {
                        // Shift+Enter → previous match
                        navigateSearch(-1);
                    } else {
                        // Enter → perform search (first time) or next match
                        if (searchMatches.length === 0) {
                            doSearch(searchInput.value);
                        } else {
                            navigateSearch(1);
                        }
                    }
                } else if (e.key === 'Escape') {
                    e.preventDefault();
                    closeSearch();
                }
            });
            // Also search on input change (debounced for live feedback)
            let _searchDebounce = null;
            searchInput.addEventListener('input', () => {
                clearTimeout(_searchDebounce);
                _searchDebounce = setTimeout(() => {
                    if (searchInput.value.trim()) {
                        doSearch(searchInput.value);
                    } else {
                        clearHighlights();
                        searchMatches = [];
                        searchCurrentIndex = -1;
                        searchCount.textContent = '';
                        searchPrev.disabled = true;
                        searchNext.disabled = true;
                    }
                }, 300);
            });
        }

        if (searchPrev) {
            searchPrev.addEventListener('click', () => navigateSearch(-1));
        }
        if (searchNext) {
            searchNext.addEventListener('click', () => navigateSearch(1));
        }

        function doSearch(query) {
            query = query.trim();
            if (!query) {
                clearHighlights();
                searchMatches = [];
                searchCurrentIndex = -1;
                searchCount.textContent = '';
                searchPrev.disabled = true;
                searchNext.disabled = true;
                return;
            }

            clearHighlights();
            searchMatches = [];
            searchCurrentIndex = -1;

            const container = document.getElementById('chat-messages');
            if (!container) return;

            // Walk all text nodes within chat-messages and highlight matches
            const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
                acceptNode(node) {
                    // Skip text inside search bar, input, or script/style tags
                    const parent = node.parentElement;
                    if (!parent) return NodeFilter.FILTER_REJECT;
                    if (parent.closest('#chat-search-bar') || parent.closest('input') ||
                        parent.closest('script') || parent.closest('style')) {
                        return NodeFilter.FILTER_REJECT;
                    }
                    if (node.textContent.length === 0) return NodeFilter.FILTER_REJECT;
                    return NodeFilter.FILTER_ACCEPT;
                }
            });

            const allTextNodes = [];
            while (walker.nextNode()) allTextNodes.push(walker.currentNode);

            // Case-insensitive search
            const queryLower = query.toLowerCase();

            for (const textNode of allTextNodes) {
                const text = textNode.textContent;
                const textLower = text.toLowerCase();
                let idx = textLower.indexOf(queryLower);
                if (idx === -1) continue;

                // Save original text for restore
                _originalTextNodes.push({ node: textNode, text: text });

                // Build new fragment with highlighted spans
                const frag = document.createDocumentFragment();
                let lastIdx = 0;
                while (idx !== -1) {
                    // Text before match
                    if (idx > lastIdx) {
                        frag.appendChild(document.createTextNode(text.substring(lastIdx, idx)));
                    }
                    // Highlighted match
                    const mark = document.createElement('mark');
                    mark.className = 'chat-search-match';
                    mark.textContent = text.substring(idx, idx + query.length);
                    frag.appendChild(mark);
                    searchMatches.push({ el: mark, node: textNode });

                    lastIdx = idx + query.length;
                    idx = textLower.indexOf(queryLower, lastIdx);
                }
                // Remaining text after last match
                if (lastIdx < text.length) {
                    frag.appendChild(document.createTextNode(text.substring(lastIdx)));
                }
                textNode.parentNode.replaceChild(frag, textNode);
            }

            // Update count display
            if (searchMatches.length > 0) {
                searchCurrentIndex = 0;
                searchMatches[0].el.classList.add('current');
                updateSearchCount();
                scrollToMatch(0);
            } else {
                searchCount.textContent = '0/0';
                searchPrev.disabled = true;
                searchNext.disabled = true;
            }
        }

        function clearHighlights() {
            // Remove all highlight marks from DOM and restore plain text
            const container = document.getElementById('chat-messages');
            if (!container) return;
            try {
                const marks = container.querySelectorAll('.chat-search-match');
                for (const mark of marks) {
                    if (!mark.parentNode) continue; // already detached (e.g., by streaming update)
                    const text = document.createTextNode(mark.textContent);
                    mark.parentNode.replaceChild(text, mark);
                }
                // Normalize: merge adjacent text nodes back together
                container.normalize();
            } catch (e) {
                console.warn('[chat-search] clearHighlights error:', e);
            }
            _originalTextNodes = [];
        }

        function navigateSearch(direction) {
            if (searchMatches.length === 0) return;

            // Remove current highlight
            if (searchCurrentIndex >= 0 && searchCurrentIndex < searchMatches.length) {
                searchMatches[searchCurrentIndex].el.classList.remove('current');
            }

            // Calculate new index with wrapping
            searchCurrentIndex += direction;
            if (searchCurrentIndex >= searchMatches.length) searchCurrentIndex = 0;
            if (searchCurrentIndex < 0) searchCurrentIndex = searchMatches.length - 1;

            // Highlight new current
            searchMatches[searchCurrentIndex].el.classList.add('current');
            updateSearchCount();
            scrollToMatch(searchCurrentIndex);
        }

        function updateSearchCount() {
            const total = searchMatches.length;
            const current = searchCurrentIndex + 1;
            searchCount.textContent = total > 0 ? `${current}/${total}` : '';
            searchPrev.disabled = total <= 1;
            searchNext.disabled = total <= 1;
        }

        function scrollToMatch(index) {
            if (index < 0 || index >= searchMatches.length) return;
            const el = searchMatches[index].el;
            // Temporarily disable auto-scroll
            const prevAutoScroll = autoScrollEnabled;
            autoScrollEnabled = false;
            el.scrollIntoView({ behavior: 'smooth', block: 'center' });
            // Restore auto-scroll after a short delay
            setTimeout(() => { autoScrollEnabled = prevAutoScroll; }, 1000);
        }

        // Mode toggle
        wireModeToggle();

        // Model selector — init on load and refresh after settings save
        refreshModelSelector();

        // Delegate click events on chat messages
        const msgContainer = document.getElementById('chat-messages');
        if (msgContainer) {
            msgContainer.addEventListener('click', (e) => {
                if (e.target.classList.contains('code-copy-btn')) return;
                if (e.target.closest('.chat-resend-btn')) {
                    resendLastMessage();
                }
                if (e.target.closest('.chat-retry-btn')) {
                    // Handled by event listener on the button itself
                }
                // Toggle tool args expand/collapse
                if (e.target.closest('.tool-args.collapsed')) {
                    e.target.closest('.tool-args').classList.toggle('collapsed');
                    e.target.closest('.tool-args').classList.toggle('expanded');
                }
            });
        }

        // Initialize auto-scroll behavior
        initAutoScroll();
    }

    function wireModeToggle() {
        const header = document.getElementById('sidebar-right-header');
        if (!header) return;
        if (header.querySelector('.chat-mode-toggle')) return;

        // Find the title element and insert toggle after it
        const titleEl = header.querySelector('h3, .sidebar-title');
        if (!titleEl) return;

        const wrapper = document.createElement('div');
        wrapper.className = 'chat-mode-toggle';
        wrapper.id = 'chat-mode-toggle';

        const label = document.createElement('span');
        label.className = 'mode-label';
        label.textContent = '';

        const select = document.createElement('select');
        select.id = 'chat-mode-select';
        select.className = 'mode-select';

        const planOpt = document.createElement('option');
        planOpt.value = 'plan';
        planOpt.textContent = '📋 计划';
        if (chatMode === 'plan') planOpt.selected = true;

        const execOpt = document.createElement('option');
        execOpt.value = 'execute';
        execOpt.textContent = '⚡ 执行';
        if (chatMode === 'execute') execOpt.selected = true;

        select.appendChild(planOpt);
        select.appendChild(execOpt);

        select.addEventListener('change', () => {
            chatMode = select.value;
            localStorage.setItem('muside_chat_mode', chatMode);
        });

        wrapper.appendChild(label);
        wrapper.appendChild(select);

        // Insert after the title
        titleEl.parentNode.insertBefore(wrapper, titleEl.nextSibling);
    }

    function updateModeToggleUI() {
        const select = document.getElementById('chat-mode-select');
        if (select) select.value = chatMode;
    }

    // ── Page Visibility: Auto-reconnect SSE after restore ─────────

    /**
     * When the page is restored from background (tab/window becomes visible),
     * check if the SSE connection was lost while hidden. If so, reconnect
     * to the running task using the buffered event replay endpoint.
     */
    document.addEventListener('visibilitychange', async () => {
        if (document.visibilityState !== 'visible') return;

        // Case 1: SSE died while page was hidden — reconnect immediately
        if (sseConnectionLostWhileHidden) {
            sseConnectionLostWhileHidden = false;
            console.log('ChatManager: Page restored, SSE was lost — reconnecting...');

            // Clean up stale state from the dead SSE
            currentAbortController = null;
            stopBackupTimer();
            hideTyping();

            // Check if the backend task is still running
            try {
                const resp = await fetch('/api/chat/task/status');
                if (!resp.ok) {
                    // Backend task is gone — reload history instead
                    setProcessing(false);
                    hideTurnIndicator();
                    autoResizeInput();
                    await loadHistory();
                    showToast('Connection restored — task may have completed', 'info');
                    return;
                }
                const data = await resp.json();
                if (data.running) {
                    // Task still running — reconnect and resume streaming
                    addMessage('system', '🔄 Reconnected to running task...');
                    showToast('Reconnected to running task', 'info', 3000);
                    // Reset streaming state for clean reconnection
                    currentStreamEl = null;
                    streamBuffer = '';
                    await reconnectTask(data.conv_id);
                } else {
                    // Task already finished — reload history
                    setProcessing(false);
                    hideTurnIndicator();
                    autoResizeInput();
                    await loadHistory();
                    showToast('Connection restored — task completed while away', 'info', 3000);
                }
            } catch (err) {
                console.warn('ChatManager: visibilitychange reconnect error:', err.message);
                setProcessing(false);
                hideTurnIndicator();
                autoResizeInput();
                await loadHistory();
            }
            return;
        }

        // Case 2: SSE is supposedly alive, but let's verify the connection is still working.
        // If the page was hidden for a long time, the browser may have silently killed
        // the connection without triggering an error (especially on mobile).
        if (sseConnectionAlive && isProcessing && !isReconnecting) {
            // The SSE reader loop might be stuck. We can't easily probe it,
            // so we check the backend task status and reconcile.
            try {
                const resp = await fetch('/api/chat/task/status');
                if (resp.ok) {
                    const data = await resp.json();
                    if (data.running) {
                        // Backend is still running. Check if we've been receiving events
                        // by looking at whether any recent streaming updates exist.
                        // If the connection is truly dead, the reader.read() will
                        // eventually throw and we'll catch it in the catch block above.
                        // For now, just log for debugging.
                        console.log('ChatManager: Page restored, SSE appears alive, backend task running');
                    } else {
                        // Backend task finished but our SSE never got the 'done' event.
                        // Clean up and reload history.
                        console.log('ChatManager: Page restored, backend task completed but SSE missed it');
                        sseConnectionAlive = false;
                        if (currentStreamEl && streamBuffer) {
                            finalizeStreamMessage();
                        }
                        currentAbortController = null;
                        setProcessing(false);
                        hideTurnIndicator();
                        stopBackupTimer();
                        autoResizeInput();
                        clearBackup();
                        await loadHistory();
                        showToast('Task completed while away', 'info', 3000);
                    }
                }
            } catch (err) {
                console.warn('ChatManager: visibilitychange status check error:', err.message);
            }
        }
    });

    // ── Initialize ─────────────────────────────────────────────────

    async function init() {
        injectSettingsStyles();  // Ensure all chat UI styles (todo panel, messages, tools, etc.) are available
        wireEvents();
        await loadHistory();
        autoResizeInput();
        // Load pending message queue from localStorage (crash/refresh recovery)
        loadPendingQueue();
        // Check for a running task and reconnect if needed
        await checkAndRecoverTask();
        // Start polling for task status (for the activity badge)
        startTaskStatusPolling();
        // If not processing and there are pending messages, process them
        if (!isProcessing && pendingMessages.length > 0) {
            setTimeout(() => processPendingQueue(), 500);
        }
        console.log('ChatManager: initialized (SSE streaming enabled)');
    }

    // Auto-init when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // ── Public API ─────────────────────────────────────────────────
    return {
        renderMessages,
        addMessage,
        sendMessage,
        clearHistory,
        newChat,
        loadHistory,
        scrollToBottom: forceScrollToBottom,
        showSettingsDialog,
        removeSettingsDialog,
        loadLLMConfig,
        saveLLMConfig,
        resendLastMessage,
        showTyping,
        hideTyping,
        setProcessing,
        setExecuteStatus,
        renderMarkdownLite,
        bindCopyButtons,
        autoResizeInput,
        abortGeneration,
        injectPlanActions,
        showSettingsDialog,
        showPlanEditor,
        showHistoryDialog,
        loadConversation,

        // Getters
        get isProcessing() { return isProcessing; },
        get messages() { return messages.slice(); },
        get lastUserMessage() { return lastUserMessage; },
        get currentConvId() { return currentConvId; },
        get pendingMessages() { return pendingMessages.slice(); },

        // Pending queue management
        addToPendingQueue,
        clearPendingQueue: () => { pendingMessages = []; savePendingQueue(); updatePendingBadge(); },
        processPendingQueue,

        // Mode control
        get chatMode() { return chatMode; },
        set chatMode(mode) { chatMode = mode; updateModeToggleUI(); },
    };
})();

window.ChatManager = ChatManager;
