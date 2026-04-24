/**
 * SearchManager - Global search and replace across all files for MusIDE
 * Works with Flask backend on port 1239
 */
const SearchManager = (() => {
    'use strict';

    // ── State ──────────────────────────────────────────────────────
    let lastResults = [];
    let lastQuery = '';
    let lastOptions = {};
    let isSearching = false;

    // Local reference to global toast (must use window. in strict mode)
    const showToast = window.showToast || function(msg) { console.warn('[Search]', msg); };

    // ── Helpers ────────────────────────────────────────────────────

    /**
     * Escape HTML entities for safe insertion
     */
    function escapeHTML(str) {
        const div = document.createElement('div');
        div.textContent = str || '';
        return div.innerHTML;
    }

    /**
     * Escape string for use in HTML attributes
     */
    function escapeAttr(str) {
        return (str || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    /**
     * Highlight matching text within a string using <mark> tags
     * @param {string} text - the line content
     * @param {string} query - the search query (plain text)
     * @param {object} options - search options (case_sensitive)
     * @returns {string} HTML string with <mark> around matches
     */
    function highlightMatch(text, query, options) {
        if (!query) return escapeHTML(text);

        const flags = options && options.case_sensitive ? 'g' : 'gi';
        let pattern;

        try {
            if (options && options.use_regex) {
                pattern = new RegExp(`(${query})`, flags);
            } else {
                // Escape regex special characters for plain text matching
                const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                pattern = new RegExp(`(${escaped})`, flags);
            }
        } catch (err) {
            // If regex is invalid, fall back to plain highlighting
            return escapeHTML(text);
        }

        const parts = text.split(pattern);
        if (parts.length <= 1) return escapeHTML(text);

        return parts.map(part => {
            if (pattern.test(part)) {
                pattern.lastIndex = 0; // Reset lastIndex after test
                return `<mark>${escapeHTML(part)}</mark>`;
            }
            pattern.lastIndex = 0;
            return escapeHTML(part);
        }).join('');
    }

    /**
     * Get the short file name from a full path
     */
    function shortName(path) {
        const idx = path.lastIndexOf('/');
        return idx >= 0 ? path.substring(idx + 1) : path;
    }

    // ── API: Search ────────────────────────────────────────────────

    /**
     * Perform a global search across all files
     * @param {string} query - search query string
     * @param {object} [options] - optional search parameters
     * @param {string} [options.pattern] - file glob pattern to filter files
     * @param {string} [options.file_pattern] - alternative file filter pattern
     * @param {boolean} [options.case_sensitive=false] - case sensitive search
     * @param {boolean} [options.use_regex=false] - treat query as regex
     * @param {number} [options.max_results=100] - maximum number of results
     * @returns {Promise<object>} search results
     */
    async function search(query, options = {}) {
        if (!query || !query.trim()) {
            showToast('Enter a search query', 'warning');
            return { results: [], total_matches: 0, files_searched: 0 };
        }

        query = query.trim();
        isSearching = true;
        lastQuery = query;
        lastOptions = { ...options };

        // Show loading state
        const resultsEl = document.getElementById('search-results');
        if (resultsEl) {
            // Show search scope info
            const scopeLabel = getScopeLabel();
            resultsEl.innerHTML = `<div class="search-summary">Searching${scopeLabel}...</div>`;
        }

        try {
            const body = {
                query,
                case_sensitive: !!options.case_sensitive,
                use_regex: !!options.use_regex,
                max_results: options.max_results || 100
            };

            // Support both pattern and file_pattern keys
            if (options.pattern) body.pattern = options.pattern;
            if (options.file_pattern) body.file_pattern = options.file_pattern;

            const resp = await fetch('/api/search', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
            });

            if (!resp.ok) throw new Error(`Search failed: ${resp.statusText}`);

            const data = await resp.json();

            // Normalize response format
            lastResults = Array.isArray(data.results) ? data.results : (data.matches || []);

            renderResults(lastResults, data);

            return data;
        } catch (err) {
            showToast(`Search error: ${err.message}`, 'error');
            if (resultsEl) {
                resultsEl.innerHTML = '<div class="search-summary">Search failed</div>';
            }
            return { results: [], total_matches: 0, files_searched: 0 };
        } finally {
            isSearching = false;
        }
    }

    // ── API: Replace ───────────────────────────────────────────────

    /**
     * Replace search matches in a single file
     * @param {string} searchText - the text to find
     * @param {string} replaceText - the replacement text
     * @param {string} filePath - path of the file to modify
     * @param {object} [options] - optional parameters (case_sensitive, use_regex)
     * @returns {Promise<object>} result with count of replacements
     */
    async function replaceInFile(searchText, replaceText, filePath, options = {}) {
        if (!searchText) {
            showToast('Enter search text to replace', 'warning');
            return { replacements: 0 };
        }

        try {
            const resp = await fetch('/api/search/replace', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    search: searchText,
                    replace: replaceText,
                    file_path: filePath,
                    case_sensitive: !!options.case_sensitive,
                    use_regex: !!options.use_regex
                })
            });

            if (!resp.ok) throw new Error(`Replace failed: ${resp.statusText}`);

            const data = await resp.json();
            const count = data.replacements || data.count || 0;

            showToast(`Replaced ${count} occurrence${count !== 1 ? 's' : ''} in ${shortName(filePath)}`, 'success');

            // Re-run search to refresh results
            if (lastQuery) {
                await search(lastQuery, lastOptions);
            }

            return data;
        } catch (err) {
            showToast(`Replace error: ${err.message}`, 'error');
            return { replacements: 0 };
        }
    }

    // ── Rendering ──────────────────────────────────────────────────

    /**
     * Render search results grouped by file
     * @param {Array} results - array of match objects { file, line, column, text, ... }
     * @param {object} [meta] - optional metadata (total_matches, files_searched)
     */
    function renderResults(results, meta) {
        const container = document.getElementById('search-results');
        if (!container) return;

        if (!results || results.length === 0) {
            container.innerHTML = '<div class="search-summary">No results found</div>';
            return;
        }

        // Group results by file
        const grouped = {};
        for (const match of results) {
            const file = match.file || match.file_path || match.path || 'unknown';
            if (!grouped[file]) grouped[file] = [];
            grouped[file].push(match);
        }

        const fileCount = Object.keys(grouped).length;
        const totalMatches = results.length;
        const filesSearched = meta && meta.files_searched ? meta.files_searched : '';

        // Summary line
        let html = `<div class="search-summary">Found ${totalMatches} result${totalMatches !== 1 ? 's' : ''} in ${fileCount} file${fileCount !== 1 ? 's' : ''}`;
        if (filesSearched) {
            html += ` (searched ${filesSearched} files)`;
        }
        html += '</div>';

        // Show search scope if in a project
        const scopeLabel = getScopeLabel();
        if (scopeLabel) {
            html += `<div class="search-summary" style="font-size:11px;color:var(--text-muted);">搜索范围: ${scopeLabel}</div>`;
        }

        // Render each file group
        for (const [filePath, matches] of Object.entries(grouped)) {
            html += `<div class="search-result-file" data-file="${escapeAttr(filePath)}">`;
            html += `<span class="search-result-file-icon">📄</span>`;
            html += `<span class="search-result-file-name">${escapeHTML(shortName(filePath))}</span>`;
            html += `<span class="search-result-file-path"> ${escapeHTML(filePath)}</span>`;
            html += `<span class="search-result-file-count">${matches.length}</span>`;
            html += '</div>';

            for (const match of matches) {
                const line = match.line || match.line_number || match.linenumber || 0;
                const col = match.column || match.col || 0;
                const text = match.text || match.line_content || match.content || '';
                const matchStart = match.match_start || match.start || 0;
                const matchEnd = match.match_end || match.end || 0;

                html += `<div class="search-result-item" data-file="${escapeAttr(filePath)}" data-line="${line}" data-col="${col}">`;
                html += `<span class="search-result-line">${line}</span>`;
                html += `<span class="search-result-text">${highlightMatch(text, lastQuery, lastOptions)}</span>`;
                html += '</div>';
            }
        }

        container.innerHTML = html;
        bindResultEvents(container);
    }

    /**
     * Bind click events on rendered search results
     */
    function bindResultEvents(container) {
        // Click on file header to open the first result in that file
        container.querySelectorAll('.search-result-file').forEach(fileEl => {
            fileEl.addEventListener('click', () => {
                const filePath = fileEl.dataset.file;
                const firstItem = container.querySelector(`.search-result-item[data-file="${CSS.escape(filePath)}"]`);
                const line = firstItem ? parseInt(firstItem.dataset.line, 10) : 1;
                openResult(filePath, line);
            });
        });

        // Click on individual result line to jump to it
        container.querySelectorAll('.search-result-item').forEach(item => {
            item.addEventListener('click', () => {
                const filePath = item.dataset.file;
                const line = parseInt(item.dataset.line, 10);
                const col = parseInt(item.dataset.col, 10);
                openResult(filePath, line, col);
            });

            // Long-press context menu for replace option
            let timer = null;
            item.addEventListener('touchstart', (e) => {
                timer = setTimeout(() => {
                    e.preventDefault();
                    showResultContextMenu(e.touches[0].clientX, e.touches[0].clientY, item.dataset.file, item.dataset.line);
                }, 500);
            }, { passive: false });
            item.addEventListener('touchend', () => clearTimeout(timer));
            item.addEventListener('touchmove', () => clearTimeout(timer));
            item.addEventListener('contextmenu', (e) => {
                e.preventDefault();
                showResultContextMenu(e.clientX, e.clientY, item.dataset.file, item.dataset.line);
            });
        });
    }

    /**
     * Open a search result in the editor
     */
    function openResult(filePath, line, col) {
        if (window.EditorManager && typeof window.EditorManager.openFileAtLine === 'function') {
            window.EditorManager.openFileAtLine(filePath, line, col);
        } else if (window.EditorManager && typeof window.EditorManager.openFile === 'function') {
            window.EditorManager.openFile(filePath);
        } else if (window.FileManager) {
            window.FileManager.openFile(filePath).then(() => {
                // After opening, try to jump to line if editor supports it
                if (window.EditorManager && typeof window.EditorManager.gotoLine === 'function') {
                    window.EditorManager.gotoLine(line, col);
                }
            });
        } else {
            showToast('Unable to open file — no editor available', 'warning');
        }
    }

    // ── Context Menu ───────────────────────────────────────────────

    /**
     * Show context menu for a search result (replace option)
     */
    function showResultContextMenu(x, y, filePath, line) {
        removeResultContextMenu();

        const menu = document.createElement('div');
        menu.className = 'context-menu visible';
        menu.style.left = `${Math.min(x, window.innerWidth - 220)}px`;
        menu.style.top = `${Math.min(y, window.innerHeight - 180)}px`;

        const items = [
            { label: 'Open File', action: () => openResult(filePath, parseInt(line, 10)) },
            { label: 'Replace in File...', action: () => promptReplace(filePath) },
            { label: 'Copy File Path', action: () => copyToClipboard(filePath) }
        ];

        menu.innerHTML = items.map(item =>
            `<button class="context-menu-item">${escapeHTML(item.label)}</button>`
        ).join('');

        const buttons = menu.querySelectorAll('.context-menu-item');
        items.forEach((item, i) => {
            buttons[i].addEventListener('click', () => {
                item.action();
                removeResultContextMenu();
            });
        });

        document.body.appendChild(menu);

        setTimeout(() => {
            document.addEventListener('click', dismissResultContextMenu, { once: true });
            document.addEventListener('touchstart', dismissResultContextMenu, { once: true });
        }, 10);
    }

    function dismissResultContextMenu(e) {
        if (!e.target.closest('.context-menu')) {
            removeResultContextMenu();
        }
    }

    function removeResultContextMenu() {
        document.querySelectorAll('.context-menu').forEach(m => m.remove());
    }

    /**
     * Prompt user for replace text and perform replace in a file
     */
    async function promptReplace(filePath) {
        if (!lastQuery) {
            showToast('No previous search to replace', 'warning');
            return;
        }

        let replaceText = '';
        if (window.showPromptDialog) {
            replaceText = await new Promise(resolve => {
                window.showPromptDialog(
                    'Replace',
                    `Replace "${lastQuery}" with:`,
                    '',
                    resolve
                );
            });
        } else {
            replaceText = window.prompt(`Replace "${lastQuery}" with:`, '');
        }

        if (replaceText === null) return; // Cancelled

        await replaceInFile(lastQuery, replaceText, filePath, lastOptions);
    }

    /**
     * Copy text to clipboard
     */
    async function copyToClipboard(text) {
        try {
            await navigator.clipboard.writeText(text);
            showToast('Copied to clipboard', 'info');
        } catch (err) {
            // Fallback for older browsers
            const textarea = document.createElement('textarea');
            textarea.value = text;
            textarea.style.position = 'fixed';
            textarea.style.left = '-9999px';
            document.body.appendChild(textarea);
            textarea.select();
            document.execCommand('copy');
            document.body.removeChild(textarea);
            showToast('Copied to clipboard', 'info');
        }
    }

    // ── UI Helpers ─────────────────────────────────────────────────

    /**
     * Read current search options from the UI inputs
     */
    function getOptionsFromUI() {
        return {
            case_sensitive: document.getElementById('search-case') ? document.getElementById('search-case').checked : false,
            use_regex: document.getElementById('search-regex') ? document.getElementById('search-regex').checked : false,
            file_pattern: document.getElementById('search-file-pattern') ? document.getElementById('search-file-pattern').value.trim() : ''
        };
    }

    /**
     * Get the current search query from the input
     */
    function getQueryFromUI() {
        const input = document.getElementById('search-input');
        return input ? input.value : '';
    }

    /**
     * Get a human-readable label for the current search scope
     */
    function getScopeLabel() {
        if (window.ProjectManager) {
            const proj = window.ProjectManager.getCurrentProject();
            if (proj && proj.name) {
                return proj.name;
            }
        }
        return '';
    }

    /**
     * Focus the search input
     */
    function focusInput() {
        const input = document.getElementById('search-input');
        if (input) input.focus();
    }

    /**
     * Clear search results
     */
    function clearResults() {
        lastResults = [];
        lastQuery = '';
        const container = document.getElementById('search-results');
        if (container) container.innerHTML = '';
        const input = document.getElementById('search-input');
        if (input) input.value = '';
    }

    // ── Wire Up ────────────────────────────────────────────────────

    function wireEvents() {
        // Search button click
        const searchBtn = document.getElementById('search-btn');
        if (searchBtn) {
            searchBtn.addEventListener('click', (e) => {
                e.preventDefault();
                const query = getQueryFromUI();
                const options = getOptionsFromUI();
                search(query, options);
            });
        }

        // Search input — Enter key triggers search
        const searchInput = document.getElementById('search-input');
        if (searchInput) {
            searchInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    const query = searchInput.value;
                    const options = getOptionsFromUI();
                    search(query, options);
                }
                // Escape clears results
                if (e.key === 'Escape') {
                    searchInput.value = '';
                    clearResults();
                    searchInput.blur();
                }
            });
        }

        // Checkbox toggles — re-search if there's an existing query
        ['search-case', 'search-regex', 'search-file-pattern'].forEach(id => {
            const el = document.getElementById(id);
            if (!el) return;

            const eventType = el.tagName === 'INPUT' ? 'change' : 'input';
            el.addEventListener(eventType, () => {
                // Re-run search with updated options if there was a previous query
                if (lastQuery) {
                    const options = getOptionsFromUI();
                    search(lastQuery, options);
                }
            });
        });
    }

    // ── Initialize ─────────────────────────────────────────────────

    function init() {
        wireEvents();
    }

    // Auto-init when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // ── Public API ─────────────────────────────────────────────────
    return {
        search,
        replaceInFile,
        renderResults,
        clearResults,
        focusInput,

        // Getters
        get lastResults() { return lastResults; },
        get lastQuery() { return lastQuery; },
        get lastOptions() { return lastOptions; },
        get isSearching() { return isSearching; },

        // Utilities exposed for other modules
        highlightMatch,
        getOptionsFromUI,
        getQueryFromUI
    };
})();

// Also expose as window.SearchManager for external access
window.SearchManager = SearchManager;
