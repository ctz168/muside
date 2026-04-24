/**
 * FileManager - File tree and file operations for MusIDE
 * Works with Flask backend on port 1239
 */
const FileManager = (() => {
    'use strict';

    // ── State ──────────────────────────────────────────────────────
    let currentPath = '';  // '' = workspace root
    let currentFilePath = null;
    let currentFileName = null;
    let fileCache = {};           // path -> { content, modified }
    let longPressTimer = null;
    let navigationHistory = [];
    let historyIndex = -1;
    let isNavigating = false;
    let projectRoot = null;      // project root path (relative to workspace), null = no project

    // ── Persistence ────────────────────────────────────────────────
    const STORAGE_KEY = 'muside_files';

    function saveState() {
        try {
            const state = {
                currentPath,
                currentFilePath,
                projectRoot
            };
            localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
        } catch (_e) {}
    }

    function loadSavedState() {
        try {
            const raw = localStorage.getItem(STORAGE_KEY);
            if (!raw) return null;
            return JSON.parse(raw);
        } catch (_e) {
            return null;
        }
    }

    // ── Helpers ────────────────────────────────────────────────────

    /**
     * Normalize a path — strip trailing slash unless root
     */
    function normalizePath(p) {
        // '' means workspace root
        if (!p || p === '/workspace' || p === '/') return '';
        // Normalize backslashes to forward slashes (Windows compatibility)
        p = p.replace(/\\/g, '/');
        if (p.endsWith('/')) p = p.slice(0, -1);
        // Strip /workspace prefix if present
        p = p.replace(/^\/workspace\/?/, '');
        // Strip any remaining leading slash (e.g. breadcrumb sends '/myrepo')
        if (p.startsWith('/')) p = p.substring(1);
        return p;
    }

    /**
     * Get parent directory of a path
     */
    function parentPath(p) {
        if (!p || p === '/') return '';  // already at root
        const idx = p.lastIndexOf('/');
        return idx <= 0 ? '' : p.substring(0, idx);
    }

    /**
     * Constrain a path to stay within the project boundary.
     * When a project is open, navigation must not go above the project root.
     * Returns the constrained path.
     */
    function constrainToProject(path) {
        if (!projectRoot) return path;  // no project, no constraint
        const normalized = normalizePath(path);
        if (!normalized || normalized === projectRoot) return projectRoot;
        if (normalized.startsWith(projectRoot + '/')) return normalized;
        // Path is above or outside the project — redirect to project root
        return projectRoot;
    }

    /**
     * Join path segments
     */
    function joinPath(base, name) {
        if (!base) return name;
        return base + '/' + name;
    }

    /**
     * Get file extension
     */
    function getExtension(filename) {
        const i = filename.lastIndexOf('.');
        return i > 0 ? filename.substring(i + 1).toLowerCase() : '';
    }

    /**
     * Check if a path looks like a directory
     */
    function isDirectory(item) {
        return item.type === 'directory' || item.isdir || item.is_dir;
    }

    /**
     * Push a path onto navigation history (only when user navigates, not back/forward)
     */
    function pushHistory(path) {
        if (isNavigating) return;
        // Trim forward history when a new navigation happens
        navigationHistory = navigationHistory.slice(0, historyIndex + 1);
        navigationHistory.push(path);
        historyIndex = navigationHistory.length - 1;
    }

    /**
     * Navigate back
     */
    function navigateBack() {
        if (historyIndex > 0) {
            historyIndex--;
            isNavigating = true;
            // Constrain to project boundary when navigating history
            const targetPath = constrainToProject(navigationHistory[historyIndex]);
            loadFileList(targetPath);
            isNavigating = false;
        }
    }

    /**
     * Navigate forward
     */
    function navigateForward() {
        if (historyIndex < navigationHistory.length - 1) {
            historyIndex++;
            isNavigating = true;
            // Constrain to project boundary when navigating history
            const targetPath = constrainToProject(navigationHistory[historyIndex]);
            loadFileList(targetPath);
            isNavigating = false;
        }
    }

    // ── API Calls ──────────────────────────────────────────────────

    /**
     * Fetch the file list for a given directory path.
     * When a project is open, constrains navigation to within the project directory.
     */
    async function loadFileList(path) {
        // Enforce project boundary before making any request
        const effectivePath = constrainToProject(normalizePath(path));
        currentPath = effectivePath;

        // '' = workspace root (server needs no path param)
        // 'myrepo' = subdirectory (server needs 'myrepo')
        let param = '';
        if (effectivePath) param = `?path=${encodeURIComponent(effectivePath)}`;
        saveState();
        updateBreadcrumb(effectivePath);

        try {
            const resp = await fetch(`/api/files/list${param}`);
            if (!resp.ok) {
                const errData = await resp.json().catch(() => ({}));
                throw new Error(errData.error || `Failed to list files: ${resp.statusText}`);
            }
            const data = await resp.json();

            // Update project root from server response
            if (data.project !== undefined) {
                projectRoot = data.project || null;
            }

            // If server redirected the path (project boundary enforcement),
            // sync our currentPath to the server's returned path
            if (data.path !== undefined && data.path !== effectivePath) {
                currentPath = normalizePath(data.path);
                saveState();
                updateBreadcrumb(currentPath);
            }

            renderFileTree(data.items || data || [], currentPath);
            return data;
        } catch (err) {
            safeToast(`Error loading files: ${err.message}`, 'error');
            return [];
        }
    }

    /**
     * Open a file and set its content in the editor
     */
    async function openFile(path) {
        try {
            // Convert absolute path to relative path for server API
            const relPath = path.replace(/^\/workspace\/?/, '');
            const resp = await fetch(`/api/files/read?path=${encodeURIComponent(relPath)}`);
            if (!resp.ok) throw new Error(`Failed to open file: ${resp.statusText}`);
            const data = await resp.json();
            const content = data.content !== undefined ? data.content : '';

            currentFilePath = path;
            currentFileName = path.split('/').pop();
            fileCache[path] = { content, modified: false };
            saveState();

            // Open in tab system
            if (window.EditorManager && typeof window.EditorManager.openTab === 'function') {
                window.EditorManager.openTab(path, content, path);
            } else if (window.EditorManager && typeof window.EditorManager.setContent === 'function') {
                // Fallback for older versions
                window.EditorManager.setContent(content, path);
            }

            // Update active state in tree
            document.querySelectorAll('.file-item.active').forEach(el => el.classList.remove('active'));
            const activeEl = document.querySelector(`.file-item[data-path="${CSS.escape(path)}"]`);
            if (activeEl) activeEl.classList.add('active');

            // Notify debugger module to update breakpoint gutter markers
            document.dispatchEvent(new CustomEvent('file:opened', { detail: { path: path, filePath: path } }));

            safeToast(`Opened ${currentFileName}`, 'info');
        } catch (err) {
            safeToast(`Error opening file: ${err.message}`, 'error');
        }
    }

    /**
     * Save the current file (overwrite)
     */
    async function saveFile(silent) {
        // If no file is open, treat as Save As
        if (!currentFilePath) {
            return saveAs();
        }

        let content = '';
        if (window.EditorManager && typeof window.EditorManager.getContent === 'function') {
            content = window.EditorManager.getContent();
        } else {
            if (!silent) safeToast('Editor not available', 'error');
            return;
        }

        try {
            const relPath = currentFilePath ? currentFilePath.replace(/^\/workspace\/?/, '') : '';
            const resp = await fetch('/api/files/save', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    path: relPath,
                    content: content
                })
            });
            if (!resp.ok) throw new Error(`Failed to save file: ${resp.statusText}`);
            const data = await resp.json();

            fileCache[currentFilePath] = { content, modified: false };
            if (window.EditorManager && typeof window.EditorManager.markClean === 'function') {
                window.EditorManager.markClean();
            }
            if (!silent) safeToast(`Saved ${currentFileName}`, 'success');

            // Auto-refresh git status to reflect changes
            if (window.GitManager && typeof window.GitManager.refreshStatus === 'function') {
                window.GitManager.refreshStatus().catch(() => {});
            }

            return data;
        } catch (err) {
            if (!silent) safeToast(`Error saving file: ${err.message}`, 'error');
            console.warn('[FileManager] Error saving file:', err.message);
        }
    }

    /**
     * Save file to a new path (Save As)
     */
    async function saveAs(newPath) {
        if (!newPath) {
            // Show dialog
            newPath = await promptDialog('Save As', 'Enter new file path:', currentFilePath || '/workspace/newfile.txt');
            if (!newPath) return; // cancelled
        }

        newPath = normalizePath(newPath);
        const relPath = newPath.replace(/^\/workspace\/?/, '');

        let content = '';
        if (window.EditorManager && typeof window.EditorManager.getContent === 'function') {
            content = window.EditorManager.getContent();
        } else {
            safeToast('Editor not available', 'error');
            return;
        }

        try {
            const resp = await fetch('/api/files/save', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    path: relPath,
                    content: content
                })
            });
            if (!resp.ok) throw new Error(`Failed to save file: ${resp.statusText}`);
            const data = await resp.json();

            currentFilePath = newPath;
            currentFileName = newPath.split('/').pop();
            fileCache[newPath] = { content, modified: false };

            if (window.EditorManager && typeof window.EditorManager.setContent === 'function') {
                window.EditorManager.setContent(content, newPath);
            }

            safeToast(`Saved as ${currentFileName}`, 'success');
            await loadFileList(currentPath);
            return data;
        } catch (err) {
            safeToast(`Error saving file: ${err.message}`, 'error');
        }
    }

    /**
     * Create a new file via dialog
     */
    async function createFile() {
        const name = await promptDialog('New File', 'Enter file name:', 'untitled.txt');
        if (!name) return;

        const path = joinPath(currentPath, name);
        const relPath = path.replace(/^\/workspace\/?/, '');

        try {
            const resp = await fetch('/api/files/create', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: relPath, type: 'file' })
            });
            if (!resp.ok) {
                const errData = await resp.json().catch(() => ({}));
                throw new Error(errData.error || `Failed to create file: ${resp.statusText}`);
            }

            safeToast(`Created ${name}`, 'success');
            await loadFileList(currentPath);

            // Open the newly created file
            await openFile(path);
        } catch (err) {
            safeToast(`Error creating file: ${err.message}`, 'error');
            console.warn('[FileManager] Error creating file:', err.message);
        }
    }

    /**
     * Create a new folder via dialog
     */
    async function createFolder() {
        const name = await promptDialog('New Folder', 'Enter folder name:', 'new_folder');
        if (!name) return;

        const path = joinPath(currentPath, name);
        const relPath = path.replace(/^\/workspace\/?/, '');

        try {
            const resp = await fetch('/api/files/create', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: relPath, type: 'directory' })
            });
            if (!resp.ok) {
                const errData = await resp.json().catch(() => ({}));
                throw new Error(errData.error || `Failed to create folder: ${resp.statusText}`);
            }

            safeToast(`Created folder ${name}`, 'success');
            await loadFileList(currentPath);
        } catch (err) {
            safeToast(`Error creating folder: ${err.message}`, 'error');
        }
    }

    /**
     * Delete a file or folder with confirmation
     */
    async function deleteFile(path) {
        const name = path.split('/').pop();
        const confirmed = await confirmDialog(`Delete "${name}"?`, 'This action cannot be undone.');
        if (!confirmed) return;

        const relPath = path.replace(/^\/workspace\/?/, '');
        try {
            const resp = await fetch('/api/files/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: relPath })
            });
            if (!resp.ok) throw new Error(`Failed to delete: ${resp.statusText}`);

            // Clear from cache
            delete fileCache[path];

            // If we deleted the currently open file, close the tab
            if (currentFilePath === path) {
                currentFilePath = null;
                currentFileName = null;
                if (window.EditorManager && typeof window.EditorManager.closeTab === 'function') {
                    window.EditorManager.closeTab(path);
                } else if (window.EditorManager && typeof window.EditorManager.setContent === 'function') {
                    window.EditorManager.setContent('', '');
                }
            }

            safeToast(`Deleted ${name}`, 'success');
            await loadFileList(currentPath);
        } catch (err) {
            safeToast(`Error deleting: ${err.message}`, 'error');
        }
    }

    /**
     * Rename a file or folder
     */
    async function renameFile(oldPath, newName) {
        if (!oldPath) {
            if (!currentFilePath) {
                safeToast('No file selected to rename', 'warning');
                return;
            }
            oldPath = currentFilePath;
        }

        if (!newName) {
            const oldName = oldPath.split('/').pop();
            newName = await promptDialog('Rename', 'Enter new name:', oldName);
            if (!newName) return;
        }

        const oldRel = oldPath.replace(/^\/workspace\/?/, '');
        const parentDir = parentPath(oldPath);
        const newRel = parentDir
            ? parentDir.replace(/^\/workspace\/?/, '') + '/' + newName
            : newName;

        try {
            const resp = await fetch('/api/files/rename', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ old_path: oldRel, new_path: newRel })
            });
            if (!resp.ok) throw new Error(`Failed to rename: ${resp.statusText}`);

            const data = await resp.json();

            // Update current file reference if this was the open file
            if (currentFilePath === oldPath) {
                currentFilePath = data.new_path || joinPath(parentPath(oldPath), newName);
                currentFileName = newName;
            }

            // Update cache key
            if (fileCache[oldPath]) {
                const newPath = data.new_path || joinPath(parentPath(oldPath), newName);
                fileCache[newPath] = fileCache[oldPath];
                delete fileCache[oldPath];
            }

            safeToast(`Renamed to ${newName}`, 'success');
            await loadFileList(currentPath);
        } catch (err) {
            safeToast(`Error renaming: ${err.message}`, 'error');
        }
    }

    /**
     * Open / navigate into a directory.
     * Enforces project boundary — cannot navigate above the project root.
     */
    async function openFolder(path) {
        path = constrainToProject(normalizePath(path));
        pushHistory(path);
        await loadFileList(path);
        // Notify other modules (e.g. GitManager) that the directory changed
        document.dispatchEvent(new CustomEvent('filemanager:navigate', { detail: { path } }));
    }

    // ── File Icon Mapping ─────────────────────────────────────────
    function getFileIcon(name) {
        const ext = name.split('.').pop().toLowerCase();
        const iconMap = {
            'py': '🐍', 'js': '📜', 'ts': '📘', 'jsx': '📜', 'tsx': '📘',
            'html': '🌐', 'htm': '🌐',
            'css': '🎨', 'scss': '🎨', 'sass': '🎨', 'less': '🎨',
            'json': '📋', 'jsonc': '📋',
            'md': '📝', 'txt': '📄', 'log': '📄',
            'sh': '⚡', 'bash': '⚡', 'zsh': '⚡', 'fish': '⚡',
            'yaml': '⚙️', 'yml': '⚙️', 'toml': '⚙️', 'cfg': '⚙️', 'ini': '⚙️', 'conf': '⚙️', 'env': '🔒',
            'c': '🔧', 'cpp': '🔧', 'h': '🔧', 'hpp': '🔧', 'cc': '🔧',
            'java': '☕', 'kt': '🟣', 'swift': '🍎',
            'rs': '🦀', 'go': '🐹', 'rb': '💎', 'php': '🐘',
            'sql': '🗃️', 'xml': '📰', 'svg': '🖼️',
            'jpg': '🖼️', 'jpeg': '🖼️', 'png': '🖼️', 'gif': '🖼️', 'webp': '🖼️', 'ico': '🖼️',
            'gitignore': '🚫', 'dockerfile': '🐳', 'makefile': '🔨',
            'lock': '🔒', 'pyc': '🔒',
        };
        // Special filenames
        if (name === 'Dockerfile') return '🐳';
        if (name === 'Makefile') return '🔨';
        if (name === 'README.md') return '📖';
        if (name === 'requirements.txt') return '📦';
        if (name.startsWith('.git')) return '🚫';
        return iconMap[ext] || '📄';
    }

    // ── Rendering ──────────────────────────────────────────────────

    /**
     * Render the file tree from API items
     * @param {Array} items - array of { name, path, type, icon, size }
     * @param {string} basePath - the directory these items belong to
     */
    function renderFileTree(items, basePath) {
        const treeEl = document.getElementById('file-tree');
        if (!treeEl) return;

        // Sort: directories first, then files, alphabetical within each group
        items.sort((a, b) => {
            const aDir = isDirectory(a);
            const bDir = isDirectory(b);
            if (aDir !== bDir) return aDir ? -1 : 1;
            return (a.name || '').localeCompare(b.name || '');
        });

        // Build HTML
        let html = '';

        // Add "go up" button (..)
        // Rules:
        //   - When a project is open: show .. only when NOT at the project root
        //     (the project root is the topmost boundary — user cannot go above it)
        //   - When no project: show .. only when NOT at the workspace root
        const rootBoundary = projectRoot || '';
        const canGoUp = currentPath && currentPath !== rootBoundary;
        if (canGoUp) {
            const parent = parentPath(currentPath);
            html += `
                <div class="file-item directory" data-path="${escapeAttr(parent)}" data-action="go-up">
                    <span class="arrow">&#9664;</span>
                    <span class="icon">📁</span>
                    <span class="name">..</span>
                    <span class="size"></span>
                </div>`;
        }

        for (const item of items) {
            const dir = isDirectory(item);
            const icon = dir ? '📁' : getFileIcon(item.name || '');
            const size = dir ? '' : formatSize(item.size || 0);
            const isActive = item.path === currentFilePath ? ' active' : '';
            const escapedPath = escapeAttr(item.path);
            const escapedName = escapeHTML(item.name || '');

            html += `
                <div class="file-item${dir ? ' directory' : ''}${isActive}" 
                     data-path="${escapedPath}" 
                     data-name="${escapedName}"
                     data-type="${dir ? 'directory' : 'file'}">
                    ${dir ? `<span class="arrow">&#9654;</span>` : '<span class="arrow-spacer"></span>'}
                    <span class="icon">${icon}</span>
                    <span class="name">${escapedName}</span>
                    <span class="size">${size}</span>
                </div>`;
        }

        if (items.length === 0) {
            html += '<div class="file-tree-empty">Empty folder</div>';
        }

        treeEl.innerHTML = html;
        bindFileItemEvents(treeEl);
    }

    /**
     * Update the breadcrumb display
     */
    function updateBreadcrumb(path) {
        const wsEl = document.getElementById('workspace-path');
        if (!wsEl) return;

        const parts = path.split('/').filter(Boolean);
        let html = '';

        // If a project is open, add project root as first breadcrumb
        let startIndex = 0;
        if (projectRoot) {
            const projectParts = projectRoot.split('/').filter(Boolean);
            // Only show parts that are within the project
            const relativeParts = parts.slice(projectParts.length);
            if (relativeParts.length >= 0) {
                // Build breadcrumb starting from project-relative path
                let accumulated = projectRoot;
                for (let i = 0; i <= relativeParts.length; i++) {
                    const segPath = accumulated;
                    const name = i === 0 ? projectParts[projectParts.length - 1] : relativeParts[i - 1];
                    const isLast = i === relativeParts.length;
                    html += `<span class="breadcrumb-segment${isLast ? ' current' : ''}" data-path="${escapeAttr(segPath)}">${escapeHTML(name)}</span>`;
                    if (!isLast) {
                        html += '<span class="breadcrumb-separator"> / </span>';
                    }
                    if (i < relativeParts.length) {
                        accumulated += '/' + relativeParts[i];
                    }
                }
            }
        } else {
            // No project - show full path
            let accumulated = '';
            for (let i = 0; i < parts.length; i++) {
                accumulated += '/' + parts[i];
                const segPath = accumulated;
                const isLast = i === parts.length - 1;

                html += `<span class="breadcrumb-segment${isLast ? ' current' : ''}" data-path="${escapeAttr(segPath)}">${escapeHTML(parts[i])}</span>`;
                if (!isLast) {
                    html += '<span class="breadcrumb-separator"> / </span>';
                }
            }
        }

        wsEl.innerHTML = html;

        // Bind breadcrumb clicks - enforce project boundary
        wsEl.querySelectorAll('.breadcrumb-segment:not(.current)').forEach(seg => {
            seg.addEventListener('click', () => {
                const targetPath = seg.dataset.path;
                // If project is open, don't navigate above project root
                if (projectRoot && !targetPath.startsWith(projectRoot)) {
                    return;
                }
                openFolder(targetPath);
            });
        });
    }

    /**
     * Bind click and long-press events to rendered file items
     */
    function bindFileItemEvents(container) {
        container.querySelectorAll('.file-item').forEach(item => {
            // ── Click handler ──
            item.addEventListener('click', (e) => {
                // Ignore if a context menu is open
                if (document.querySelector('.context-menu.visible')) return;

                const path = item.dataset.path;
                const type = item.dataset.type;
                const action = item.dataset.action;

                if (action === 'go-up') {
                    // Use data-path from the rendered button (already computed correctly)
                    openFolder(path);
                    return;
                }

                if (type === 'directory') {
                    // Toggle expand/collapse arrow
                    const arrow = item.querySelector('.arrow');
                    const isOpen = arrow && arrow.classList.contains('open');

                    if (isOpen) {
                        // Collapse — reload parent
                        arrow.classList.remove('open');
                    } else {
                        if (arrow) arrow.classList.add('open');
                    }

                    openFolder(path);
                } else {
                    openFile(path);
                }
            });

            // ── Long-press (context menu) handler ──
            item.addEventListener('touchstart', (e) => {
                longPressTimer = setTimeout(() => {
                    e.preventDefault();
                    const path = item.dataset.path;
                    const name = item.dataset.name;
                    const type = item.dataset.type;
                    showContextMenu(e.touches[0].clientX, e.touches[0].clientY, path, name, type);
                }, 500);
            }, { passive: false });

            item.addEventListener('touchend', () => {
                clearTimeout(longPressTimer);
            });

            item.addEventListener('touchmove', () => {
                clearTimeout(longPressTimer);
            });

            // Desktop right-click
            item.addEventListener('contextmenu', (e) => {
                e.preventDefault();
                const path = item.dataset.path;
                const name = item.dataset.name;
                const type = item.dataset.type;
                showContextMenu(e.clientX, e.clientY, path, name, type);
            });
        });
    }

    // ── Context Menu ───────────────────────────────────────────────

    /**
     * Show a context menu for a file/folder
     */
    function showContextMenu(x, y, path, name, type) {
        // Remove any existing context menu
        removeContextMenu();

        const menu = document.createElement('div');
        menu.className = 'context-menu visible';
        menu.style.left = `${Math.min(x, window.innerWidth - 200)}px`;
        menu.style.top = `${Math.min(y, window.innerHeight - 250)}px`;

        const items = [];

        if (type === 'file') {
            items.push({ label: 'Open', action: () => openFile(path) });
            items.push({ label: 'Rename', action: () => renameFile(path) });
            items.push({ label: 'Delete', action: () => deleteFile(path), cls: 'danger' });
        } else {
            items.push({ label: 'Open Folder', action: () => openFolder(path) });
            items.push({ label: 'Rename', action: () => renameFile(path) });
            items.push({ label: 'Delete', action: () => deleteFile(path), cls: 'danger' });
        }

        // Git ignore — convert workspace-relative path to git-root-relative path
        if (window.GitManager) {
            let gitRelPath = path;
            const proj = window.ProjectManager && window.ProjectManager.getCurrentProject();
            if (proj && proj.project && gitRelPath.startsWith(proj.project + '/')) {
                gitRelPath = gitRelPath.substring(proj.project.length + 1);
            }
            if (type === 'directory' && !gitRelPath.endsWith('/')) {
                gitRelPath += '/';
            }
            const _gitPath = gitRelPath;
            items.push({ label: '🚫 忽略 Git', action: () => window.GitManager.addToGitignore(_gitPath) });
        }

        items.push({ label: 'New File', action: () => createFileIn(path) });
        items.push({ label: 'New Folder', action: () => createFolderIn(path) });

        for (const item of items) {
            const btn = document.createElement('button');
            btn.className = `context-menu-item${item.cls ? ' ' + item.cls : ''}`;
            btn.textContent = item.label;
            btn.addEventListener('click', () => {
                item.action();
                removeContextMenu();
            });
            menu.appendChild(btn);
        }

        document.body.appendChild(menu);

        // Dismiss on tap/click outside
        setTimeout(() => {
            document.addEventListener('click', dismissContextMenu, { once: true });
            document.addEventListener('touchstart', dismissContextMenu, { once: true });
        }, 10);
    }

    function dismissContextMenu(e) {
        if (!e.target.closest('.context-menu')) {
            removeContextMenu();
        }
    }

    function removeContextMenu() {
        document.querySelectorAll('.context-menu').forEach(m => m.remove());
    }

    // ── Safe Toast Helper ──────────────────────────────────────────
    function safeToast(msg, type) {
        if (window.showToast) window.showToast(msg, type);
        else console.warn('[FileManager]', msg);
    }

    /**
     * Create file in a specific directory (for context menu)
     */
    async function createFileIn(dirPath) {
        const name = await promptDialog('New File', 'Enter file name:', 'untitled.txt');
        if (!name) return;
        const path = joinPath(dirPath, name);
        const relPath = path.replace(/^\/workspace\/?/, '');

        try {
            const resp = await fetch('/api/files/create', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: relPath, type: 'file' })
            });
            if (!resp.ok) {
                const errData = await resp.json().catch(() => ({}));
                throw new Error(errData.error || `Failed to create file: ${resp.statusText}`);
            }
            safeToast(`Created ${name}`, 'success');
            // Refresh the directory where the file was created
            if (currentPath === dirPath) {
                await loadFileList(currentPath);
            } else {
                await loadFileList(dirPath);
            }
        } catch (err) {
            safeToast(`Error creating file: ${err.message}`, 'error');
        }
    }

    /**
     * Create folder in a specific directory (for context menu)
     */
    async function createFolderIn(dirPath) {
        const name = await promptDialog('New Folder', 'Enter folder name:', 'new_folder');
        if (!name) return;
        const path = joinPath(dirPath, name);
        const relPath = path.replace(/^\/workspace\/?/, '');

        try {
            const resp = await fetch('/api/files/create', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: relPath, type: 'directory' })
            });
            if (!resp.ok) {
                const errData = await resp.json().catch(() => ({}));
                throw new Error(errData.error || `Failed to create folder: ${resp.statusText}`);
            }
            safeToast(`Created folder ${name}`, 'success');
            // Refresh the directory where the folder was created
            if (currentPath === dirPath) {
                await loadFileList(currentPath);
            } else {
                await loadFileList(dirPath);
            }
        } catch (err) {
            safeToast(`Error creating folder: ${err.message}`, 'error');
        }
    }

    // ── Utility ────────────────────────────────────────────────────

    function formatSize(bytes) {
        if (bytes === 0) return '0 B';
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
        return (bytes / 1048576).toFixed(1) + ' MB';
    }

    function escapeHTML(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    function escapeAttr(str) {
        return str.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    /**
     * Simple prompt dialog (replaces window.prompt for mobile)
     * Returns a promise that resolves to the entered value or null if cancelled.
     */
    function promptDialog(title, label, defaultValue) {
        return new Promise((resolve) => {
            // If a custom dialog system exists, use it
            if (window.showPromptDialog) {
                window.showPromptDialog(title, label, defaultValue, resolve);
                return;
            }
            // Fallback to native prompt
            const result = window.prompt(`${title}\n${label}`, defaultValue);
            resolve(result);
        });
    }

    /**
     * Simple confirm dialog (replaces window.confirm for mobile)
     * Returns a promise that resolves to true/false.
     */
    function confirmDialog(title, message) {
        return new Promise((resolve) => {
            if (window.showConfirmDialog) {
                window.showConfirmDialog(title, message, resolve);
                return;
            }
            const result = window.confirm(`${title}\n${message}`);
            resolve(result);
        });
    }

    // ── Initialize ─────────────────────────────────────────────────

    async function init() {
        // On startup, check if there's an active project before loading.
        // This prevents the race condition where workspace root briefly shows
        // before ProjectManager.loadProjectInfo() navigates to the project.

        // IMPORTANT: Load saved state FIRST, before any loadFileList() call
        // (which would overwrite saved state via saveState())
        const saved = loadSavedState();

        try {
            const resp = await fetch('/api/project/info');
            if (resp.ok) {
                const data = await resp.json();
                if (data.project) {
                    // Project exists — load project directory directly
                    projectRoot = data.project || null;

                    // Determine what sub-path to show
                    let targetPath = data.project;
                    if (saved && saved.currentPath && saved.projectRoot === projectRoot
                        && saved.currentPath.startsWith(projectRoot)) {
                        // Restore previously viewed sub-directory within project
                        targetPath = saved.currentPath;
                    }

                    currentPath = targetPath;
                    await loadFileList(targetPath);
                    pushHistory(targetPath);

                    // Re-open previously open file
                    if (saved && saved.currentFilePath) {
                        const filePath = saved.currentFilePath;
                        try {
                            const checkResp = await fetch(`/api/files/read?path=${encodeURIComponent(filePath.replace(/^\/workspace\/?/, ''))}`);
                            if (checkResp.ok) {
                                await openFile(filePath);
                            }
                        } catch (_e) {}
                    }
                    return;
                }
            }
        } catch (e) {
            console.warn('[FileManager] Failed to check project on init:', e);
        }

        // No project — try to restore saved state
        const restoredPath = saved ? (saved.currentPath || '') : currentPath;
        loadFileList(restoredPath);
        pushHistory(restoredPath);

        // Re-open saved file if any
        if (saved && saved.currentFilePath && !saved.projectRoot) {
            try {
                const filePath = saved.currentFilePath;
                const checkResp = await fetch(`/api/files/read?path=${encodeURIComponent(filePath.replace(/^\/workspace\/?/, ''))}`);
                if (checkResp.ok) {
                    await openFile(filePath);
                }
            } catch (_e) {}
        }
    }

    // Auto-init when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // Listen for project events
    document.addEventListener('project:opened', (e) => {
        projectRoot = e.detail.project || null;
        saveState();
    });
    document.addEventListener('project:closed', () => {
        projectRoot = null;
        currentPath = '';
        saveState();
    });

    // Listen for workspace changes — reload file list from new root
    document.addEventListener('workspace:changed', () => {
        projectRoot = null;
        currentPath = '';
        saveState();
        loadFileList('');
    });

    // ── Public API ─────────────────────────────────────────────────
    return {
        loadFileList,
        openFile,
        saveFile,
        saveAs,
        createFile,
        createFolder,
        deleteFile,
        renameFile,
        openFolder,
        renderFileTree,
        navigateBack,
        navigateForward,
        refresh: () => loadFileList(currentPath),

        // Getters
        get currentPath() { return currentPath; },
        get currentFilePath() { return currentFilePath; },
        get currentFileName() { return currentFileName; },
        get projectRoot() { return projectRoot; },
        set currentFilePath(v) { currentFilePath = v; },

        // Utilities exposed for other modules
        normalizePath,
        parentPath,
        joinPath,
        pushHistory
    };
})();

// Expose to window for cross-module access (const doesn't create window properties)
window.FileManager = FileManager;
