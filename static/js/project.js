/**
 * ProjectManager - Project management for MusIDE
 * Handles open/close/clone project operations
 */
const ProjectManager = (() => {
    'use strict';

    // ── State ──────────────────────────────────────────────────────
    let currentProject = null;  // { project: 'myrepo', name: 'myrepo' }
    let pickerPath = '';        // current path in folder picker (relative to workspace)
    let currentWorkspace = null; // current workspace path (absolute)
    let wsPickerPath = '';      // current path in workspace picker (absolute, filesystem)

    // ── Workspace Management ───────────────────────────────────────

    /**
     * Load workspace info from server and update UI
     */
    async function loadWorkspaceInfo() {
        try {
            const resp = await fetch('/api/workspace/info');
            if (!resp.ok) return;
            const data = await resp.json();
            currentWorkspace = data.workspace;
            updateWorkspaceInfoBar(data);
        } catch (err) {
            console.warn('[ProjectManager] Failed to load workspace info:', err);
        }
    }

    /**
     * Update the workspace info bar in the UI
     */
    function updateWorkspaceInfoBar(data) {
        const bar = document.getElementById('workspace-info-bar');
        const pathEl = document.getElementById('workspace-current-path');
        if (!bar || !pathEl) return;

        if (data.exists) {
            bar.classList.remove('hidden');
            // Show just the last directory name for brevity, full path on hover
            const short = data.workspace.split('/').filter(Boolean).pop() || data.workspace;
            pathEl.textContent = data.workspace;
            pathEl.title = data.workspace;
        } else {
            bar.classList.remove('hidden');
            pathEl.textContent = data.workspace + ' (不存在)';
            pathEl.style.color = 'var(--error-color, #f44)';
        }
    }

    /**
     * Show the workspace picker dialog (navigable filesystem browser)
     */
    async function showWorkspacePicker() {
        // If there's already a workspace, start browsing from it
        const startPath = currentWorkspace || '/';
        wsPickerPath = '';

        // Hide project info and folder picker, show workspace picker
        const pickerEl = document.getElementById('workspace-picker');
        const infoEl = document.getElementById('project-info');
        const projectPicker = document.getElementById('project-folder-picker');
        if (pickerEl) pickerEl.classList.remove('hidden');
        if (infoEl) infoEl.classList.add('hidden');
        if (projectPicker) projectPicker.classList.add('hidden');

        await browseWorkspace(startPath);
    }

    function hideWorkspacePicker() {
        const pickerEl = document.getElementById('workspace-picker');
        const infoEl = document.getElementById('project-info');
        if (pickerEl) pickerEl.classList.add('hidden');
        if (infoEl) infoEl.classList.remove('hidden');
    }

    /**
     * Browse directories at the given path for workspace selection
     */
    async function browseWorkspace(path) {
        wsPickerPath = path;
        try {
            const resp = await fetch(`/api/workspace/browse?path=${encodeURIComponent(path)}`);
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                throw new Error(err.error || 'Failed to browse');
            }
            const data = await resp.json();

            // Update header path
            const pathEl = document.getElementById('ws-picker-path');
            if (pathEl) pathEl.textContent = data.current_path;

            // Show/hide back button
            const backBtn = document.getElementById('ws-picker-back');
            if (backBtn) backBtn.style.display = data.can_go_up ? '' : 'none';

            // Render folder list
            const listEl = document.getElementById('ws-picker-list');
            if (!listEl) return;

            if (data.folders.length === 0) {
                listEl.innerHTML = '<div style="text-align:center;padding:20px;color:var(--text-muted);font-size:12px;">此目录下没有子文件夹</div>';
                return;
            }

            let html = '';
            for (const folder of data.folders) {
                // Highlight if this is the current workspace
                const isCurrent = currentWorkspace && folder.path === currentWorkspace;
                const activeClass = isCurrent ? ' style="background:var(--accent-color, #e8853d);color:#fff;"' : '';
                html += `
                    <div class="project-folder-item"${activeClass} data-path="${escapeAttr(folder.path)}">
                        <div class="project-folder-info" data-path="${escapeAttr(folder.path)}">
                            <span class="icon">📁</span>
                            <span class="name">${escapeHTML(folder.name)}</span>
                        </div>
                    </div>`;
            }

            listEl.innerHTML = html;

            // Bind click events for folder navigation
            listEl.querySelectorAll('.project-folder-info').forEach(item => {
                const handler = async () => {
                    const itemPath = item.dataset.path;
                    await browseWorkspace(itemPath);
                };
                if (window.bindTouchButton) {
                    window.bindTouchButton(item, handler);
                } else {
                    item.addEventListener('click', handler);
                }
            });
        } catch (err) {
            safeToast('浏览目录失败: ' + err.message, 'error');
        }
    }

    /**
     * Confirm workspace selection
     */
    async function confirmWorkspace() {
        if (!wsPickerPath) return;

        try {
            const resp = await fetch('/api/workspace/set', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: wsPickerPath })
            });
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                throw new Error(err.error || 'Failed to set workspace');
            }

            currentWorkspace = wsPickerPath;
            safeToast(`工作目录已设置为: ${wsPickerPath.split('/').pop()}`, 'success');

            hideWorkspacePicker();

            // Update info bar
            updateWorkspaceInfoBar({ workspace: wsPickerPath, exists: true });

            // If a project is currently open and it's inside the old workspace,
            // we need to close it since the project path may no longer be valid
            if (currentProject) {
                safeToast('工作目录已更改，请重新选择项目', 'info');
                await closeProject();
            }

            // Reload file list to reflect new workspace root
            if (window.FileManager) {
                window.FileManager.loadFileList('');
            }

            // Notify other modules that workspace changed
            document.dispatchEvent(new CustomEvent('workspace:changed', { detail: { workspace: wsPickerPath } }));
        } catch (err) {
            safeToast('设置工作目录失败: ' + err.message, 'error');
        }
    }

    // ── Helpers ────────────────────────────────────────────────────
    function safeToast(msg, type) {
        if (window.showToast) window.showToast(msg, type);
        else console.warn('[ProjectManager]', msg);
    }

    function escapeHTML(str) {
        const div = document.createElement('div');
        div.textContent = str || '';
        return div.innerHTML;
    }

    function escapeAttr(str) {
        return (str || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    // ── API Calls ──────────────────────────────────────────────────

    /**
     * Load current project info from server
     */
    async function loadProjectInfo() {
        try {
            const resp = await fetch('/api/project/info');
            if (!resp.ok) return;
            const data = await resp.json();
            if (data.project) {
                currentProject = data;
                onProjectOpened(data);

                // Navigate FileManager into the project directory
                // Only navigate if FileManager is not already in the project
                if (window.FileManager) {
                    const fmPath = window.FileManager.currentPath || '';
                    if (!fmPath.startsWith(data.project)) {
                        await window.FileManager.loadFileList(data.project);
                    }
                }

                // Refresh git status in the project directory
                if (window.GitManager) {
                    await window.GitManager.refresh();
                }

                // If project is open on startup, switch to files tab
                switchToFilesTab();

                // Auto-detect and activate virtual environment
                autoActivateVenv();
            } else {
                currentProject = null;
                onProjectClosed();
            }
        } catch (err) {
            console.warn('[ProjectManager] Failed to load project info:', err);
        }
    }

    /**
     * Open a project, git init it, and switch to files tab
     */
    async function openProject(projectPath) {
        try {
            const resp = await fetch('/api/project/open', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ project: projectPath })
            });
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                throw new Error(err.error || 'Failed to open project');
            }
            const data = await resp.json();
            currentProject = data;
            safeToast(`项目已打开: ${data.name}`, 'success');
            onProjectOpened(data);

            // Git init the project (safe to call even if already a git repo)
            try {
                await gitInitProject(projectPath);
            } catch (e) {
                console.warn('[ProjectManager] Git init skipped:', e.message);
            }

            // Navigate FileManager into the project directory
            if (window.FileManager) {
                await window.FileManager.loadFileList(projectPath);
            }

            // Refresh git status
            if (window.GitManager) {
                await window.GitManager.refresh();
            }

            // Switch to files tab
            switchToFilesTab();

            // Auto-detect and activate virtual environment
            autoActivateVenv();

            return data;
        } catch (err) {
            safeToast('打开项目失败: ' + err.message, 'error');
        }
    }

    /**
     * Git init a project directory
     */
    async function gitInitProject(projectPath) {
        try {
            const resp = await fetch('/api/git/init', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: projectPath })
            });
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                // It's ok if git init fails (already a git repo)
                console.warn('[ProjectManager] Git init result:', err.error || 'non-ok');
                return;
            }
            const data = await resp.json();
            if (data.note) {
                safeToast(data.note, 'success');
            }
        } catch (err) {
            console.warn('[ProjectManager] Git init error:', err.message);
        }
    }

    /**
     * Close the current project
     */
    async function closeProject() {
        if (!currentProject) return;

        try {
            const resp = await fetch('/api/project/close', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            });
            if (!resp.ok) throw new Error('Failed to close project');

            const projectName = currentProject.name;
            currentProject = null;

            // ── Reverse of openProject: full cleanup ──

            // 1. Clear editor search state (search scope changes)
            if (window.SearchManager) {
                window.SearchManager.clearResults();
            }

            // 2. Clear all open editor tabs (clean slate for workspace)
            if (window.EditorManager) {
                const tabList = window.EditorManager.getTabList();
                for (const tabPath of [...tabList]) {  // copy array since closeTab mutates it
                    window.EditorManager.closeTab(tabPath);
                }
            }

            // 3. Update UI: title, close button, project info panel, hide tabs
            //    This also dispatches project:closed event → FileManager resets projectRoot
            onProjectClosed();

            safeToast(`项目已关闭: ${projectName}`, 'success');

            // 4. Return FileManager to workspace root (undo navigation into project)
            if (window.FileManager) {
                await window.FileManager.loadFileList('');
            }

            // 5. Reset git status (workspace level, no git context)
            if (window.GitManager) {
                await window.GitManager.refresh();
            }

            // 6. Switch to project tab (reverse of switchToFilesTab in openProject)
            switchToProjectTab();
        } catch (err) {
            safeToast('关闭项目失败: ' + err.message, 'error');
        }
    }

    /**
     * Create a new project: folder + venv + git init + open it
     */
    async function newProject() {
        // Ensure workspace is set
        if (!currentWorkspace) {
            safeToast('请先设置工作目录', 'warning');
            return;
        }

        // Show input dialog for project name
        const values = await window.showInputDialog('✨ 新建项目', [
            { name: 'name', label: '项目名称', type: 'text', placeholder: 'my-project' },
        ]);

        if (!values || !values.name) return;

        const projectName = values.name.trim();
        if (!projectName) return;

        // Validate name (no slashes, no dots at start, no spaces)
        if (/[/\\]/.test(projectName)) {
            safeToast('项目名称不能包含路径分隔符', 'error');
            return;
        }
        if (/^\./.test(projectName)) {
            safeToast('项目名称不能以点号开头', 'error');
            return;
        }

        safeToast('正在创建项目...', 'info');

        try {
            // Step 1: Create the folder via API
            const createResp = await fetch('/api/project/create', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: projectName })
            });
            if (!createResp.ok) {
                const err = await createResp.json().catch(() => ({}));
                throw new Error(err.error || '创建文件夹失败');
            }
            const createData = await createResp.json();
            const projectPath = createData.project;

            safeToast(`文件夹已创建: ${projectName}`, 'success');

            // Step 2: Open the project (sets config, updates UI)
            await openProject(projectPath);

            // Step 3: Create virtual environment (.venv inside project dir)
            // Use .venv (standard convention) — never use projectName as venv name,
            // because a venv folder named the same as the project confuses the AI model
            // about which directory is the project root vs the virtual environment.
            safeToast('正在创建虚拟环境，请稍候...', 'info');
            try {
                const venvResp = await fetch('/api/venv/create', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ path: '.venv' })
                });
                if (venvResp.ok) {
                    const venvData = await venvResp.json();
                    if (venvData.already_exists) {
                        safeToast('虚拟环境已存在: .venv', 'info');
                    } else {
                        safeToast('虚拟环境已创建: .venv', 'success');
                    }
                    // venv/create runs synchronously and saves venv_path in config,
                    // so the venv is already "activated" — just update UI
                    updateVenvUI(venvData.venv_path || '.venv');
                    safeToast('虚拟环境已激活', 'success');
                } else {
                    const venvErr = await venvResp.json().catch(() => ({}));
                    safeToast('虚拟环境创建失败: ' + (venvErr.error || '未知错误'), 'warning');
                }
            } catch (venvErr) {
                safeToast('虚拟环境创建失败: ' + venvErr.message, 'warning');
            }

            // Step 4: Refresh directory listing
            if (window.FileManager) {
                await window.FileManager.loadFileList(projectPath);
            }

            // Step 5: Refresh git status
            if (window.GitManager) {
                await window.GitManager.refresh();
            }

            safeToast(`项目「${projectName}」创建完成！`, 'success');
        } catch (err) {
            safeToast('新建项目失败: ' + err.message, 'error');
        }
    }

    /**
     * Clone a project — custom dialog with:
     *   Row 1: OAuth login button
     *   Row 2: Repo dropdown (visible when logged in)
     *   Row 3: Manual URL + token fallback
     */
    async function cloneProject() {
        const result = await showProjectCloneDialog();
        if (!result) return;

        let url = result.url;

        safeToast('正在克隆项目...', 'info');

        try {
            const resp = await fetch('/api/git/clone', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url })
            });
            if (!resp.ok) {
                const errData = await resp.json().catch(() => ({}));
                throw new Error(errData.error || 'Clone failed');
            }
            const data = await resp.json();
            const clonePath = data.path;

            safeToast('克隆成功，正在打开项目...', 'success');
            await openProject(clonePath);
            return data;
        } catch (err) {
            safeToast('克隆项目失败: ' + err.message, 'error');
        }
    }

    /**
     * Build and show the clone dialog:
     *   Row 1: OAuth button (or green status if already authorized)
     *   Row 2: Repo dropdown (appears after OAuth authorization)
     *   Row 3: Manual URL + optional Token
     */
    function showProjectCloneDialog() {
        return new Promise(async (resolve) => {
            // Check auth status
            let savedToken = '';
            let username = '';
            try {
                const cfgResp = await fetch('/api/config');
                if (cfgResp.ok) {
                    const cfg = await cfgResp.json();
                    savedToken = cfg.github_token || '';
                }
                if (savedToken) {
                    const authResp = await fetch('/api/git/github/auth/status');
                    if (authResp.ok) {
                        const authData = await authResp.json();
                        if (authData.authenticated) username = authData.username || '';
                        else savedToken = '';
                    }
                }
            } catch (_e) {}

            const isLoggedIn = !!savedToken;

            // ── Build DOM ──
            const overlay = document.getElementById('dialog-overlay');
            const dialogTitle = document.getElementById('dialog-title');
            const dialogBody = document.getElementById('dialog-body');
            const dialogButtons = document.getElementById('dialog-buttons');

            if (!overlay || !dialogBody) {
                const url = window.prompt('Clone Repository URL:', 'https://github.com/user/repo.git');
                resolve(url ? { url, token: '' } : null);
                return;
            }

            dialogTitle.textContent = '📥 克隆项目';
            dialogBody.innerHTML = '';
            dialogButtons.innerHTML = '';

            const container = document.createElement('div');
            container.style.cssText = 'display:flex;flex-direction:column;gap:14px;';

            // ═══ Row 1: OAuth Login ═══
            const row1 = document.createElement('div');
            row1.style.cssText = 'text-align:center;';

            if (isLoggedIn) {
                const statusDiv = document.createElement('div');
                statusDiv.style.cssText = 'display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:10px 16px;border-radius:8px;background:rgba(76,175,80,0.1);border:1px solid rgba(76,175,80,0.3);';
                const avatar = document.createElement('span');
                avatar.style.cssText = 'width:24px;height:24px;border-radius:50%;background:var(--accent);display:inline-flex;align-items:center;justify-content:center;font-size:12px;color:#fff;font-weight:700;';
                avatar.textContent = (username || 'G')[0].toUpperCase();
                const info = document.createElement('span');
                info.style.cssText = 'font-size:13px;color:var(--text-primary);font-weight:500;';
                info.textContent = `✓ ${escapeHTML(username || '已授权')}`;
                statusDiv.appendChild(avatar);
                statusDiv.appendChild(info);
                row1.appendChild(statusDiv);
            } else {
                const loginBtn = document.createElement('button');
                loginBtn.id = 'project-clone-login-btn';
                loginBtn.textContent = '🔑 GitHub OAuth 授权登录';
                loginBtn.style.cssText = 'width:100%;padding:12px;border:1px solid var(--accent);background:var(--accent);color:#fff;border-radius:8px;font-size:15px;font-weight:600;cursor:pointer;';
                loginBtn.onclick = async () => {
                    overlay.classList.add('hidden');
                    try {
                        // Step 1: Request device code from GitHub
                        const startResp = await fetch('/api/git/github/auth/start', {
                            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({})
                        });
                        const data = await startResp.json();

                        if (data.oauth_unavailable || !data.ok) {
                            // OAuth App not configured — fallback to token input
                            safeToast('OAuth 未配置，请在下方输入 Token 登录', 'info');
                            overlay.classList.remove('hidden');
                            return;
                        }

                        // Step 2: Show Device Flow dialog (auto-opens GitHub authorization page)
                        if (window.GitManager && window.GitManager.showDeviceCodeDialog) {
                            await window.GitManager.showDeviceCodeDialog(data);
                        }

                        // Step 3: Authorization complete — toast + reopen dialog
                        safeToast('✅ 授权成功！正在加载仓库列表...', 'success');
                        showProjectCloneDialog().then(resolve);
                    } catch (_e) {
                        safeToast('授权启动失败，请在下方输入 Token', 'info');
                        overlay.classList.remove('hidden');
                    }
                };
                if (window.bindTouchButton) window.bindTouchButton(loginBtn, () => loginBtn.onclick());
                row1.appendChild(loginBtn);
            }
            container.appendChild(row1);

            // ═══ Row 2: Repo Dropdown ═══
            const row2 = document.createElement('div');
            row2.id = 'project-clone-repo-row';

            if (isLoggedIn) {
                const label2 = document.createElement('label');
                label2.style.cssText = 'display:block;font-size:12px;color:var(--text-muted);margin-bottom:6px;';
                label2.textContent = '选择要克隆的仓库';
                row2.appendChild(label2);

                const selectWrap = document.createElement('div');
                selectWrap.style.cssText = 'position:relative;';

                const repoSelect = document.createElement('select');
                repoSelect.id = 'project-clone-repo-select';
                repoSelect.style.cssText = 'width:100%;padding:10px 12px;border-radius:8px;border:1px solid #4a3f33;background:#2d2620;color:#f5f0eb;font-size:13px;box-sizing:border-box;appearance:none;-webkit-appearance:none;cursor:pointer;';
                const placeholderOpt = document.createElement('option');
                placeholderOpt.value = '';
                placeholderOpt.textContent = '⏳ 加载仓库列表中...';
                placeholderOpt.disabled = true;
                placeholderOpt.selected = true;
                repoSelect.appendChild(placeholderOpt);
                selectWrap.appendChild(repoSelect);

                const chevron = document.createElement('span');
                chevron.style.cssText = 'position:absolute;right:12px;top:50%;transform:translateY(-50%);pointer-events:none;font-size:12px;color:var(--text-muted);';
                chevron.textContent = '▾';
                selectWrap.appendChild(chevron);
                row2.appendChild(selectWrap);

                const descDiv = document.createElement('div');
                descDiv.id = 'project-clone-repo-desc';
                descDiv.style.cssText = 'font-size:11px;color:var(--text-muted);margin-top:4px;min-height:16px;';
                row2.appendChild(descDiv);
            } else {
                row2.style.cssText = 'display:none;';
            }
            container.appendChild(row2);

            // ═══ Divider ═══
            if (isLoggedIn) {
                const divider = document.createElement('div');
                divider.style.cssText = 'display:flex;align-items:center;gap:10px;margin:2px 0;';
                divider.innerHTML = '<span style="flex:1;height:1px;background:var(--border);"></span><span style="font-size:11px;color:var(--text-muted);">或手动输入地址</span><span style="flex:1;height:1px;background:var(--border);"></span>';
                container.appendChild(divider);
            }

            // ═══ Row 3: Manual URL + Token ═══
            const row3 = document.createElement('div');
            const label3 = document.createElement('label');
            label3.style.cssText = 'display:block;font-size:12px;color:var(--text-muted);margin-bottom:6px;';
            label3.textContent = '仓库地址';
            row3.appendChild(label3);

            const urlInput = document.createElement('input');
            urlInput.type = 'text';
            urlInput.id = 'project-clone-url';
            urlInput.placeholder = 'https://github.com/user/repo.git';
            urlInput.autocomplete = 'off';
            urlInput.style.cssText = 'width:100%;padding:10px 12px;border-radius:8px;border:1px solid #4a3f33;background:#2d2620;color:#f5f0eb;font-size:13px;box-sizing:border-box;';
            row3.appendChild(urlInput);

            if (!isLoggedIn) {
                const tokenLabel = document.createElement('label');
                tokenLabel.style.cssText = 'display:block;font-size:12px;color:var(--text-muted);margin-bottom:6px;margin-top:8px;';
                tokenLabel.textContent = 'GitHub Token（私有仓库需要）';
                row3.appendChild(tokenLabel);

                const tokenInput = document.createElement('input');
                tokenInput.type = 'password';
                tokenInput.id = 'project-clone-token';
                tokenInput.placeholder = '公开仓库无需填写';
                tokenInput.style.cssText = 'width:100%;padding:10px 12px;border-radius:8px;border:1px solid #4a3f33;background:#2d2620;color:#f5f0eb;font-size:13px;box-sizing:border-box;';
                row3.appendChild(tokenInput);
            }
            container.appendChild(row3);

            dialogBody.appendChild(container);

            // ── Buttons ──
            const cancelBtn = document.createElement('button');
            cancelBtn.textContent = '取消';
            cancelBtn.className = 'btn-cancel';
            const cloneBtn = document.createElement('button');
            cloneBtn.id = 'project-clone-confirm-btn';
            cloneBtn.textContent = '确认克隆';
            cloneBtn.className = 'btn-confirm';
            dialogButtons.appendChild(cancelBtn);
            dialogButtons.appendChild(cloneBtn);

            // ── State ──
            let resolved = false;
            let reposCache = [];

            function finish(result) {
                if (resolved) return;
                resolved = true;
                overlay.classList.add('hidden');
                resolve(result);
            }

            // ── Load repos if logged in ──
            if (isLoggedIn) {
                (async () => {
                    try {
                        const resp = await fetch('/api/git/github/repos?per_page=100&sort=updated');
                        if (resp.ok) {
                            const data = await resp.json();
                            reposCache = data.repos || [];
                        }
                    } catch (_e) {}

                    const select = document.getElementById('project-clone-repo-select');
                    if (!select) return;

                    select.innerHTML = '';
                    if (reposCache.length === 0) {
                        const opt = document.createElement('option');
                        opt.value = '';
                        opt.textContent = '未找到仓库';
                        opt.disabled = true;
                        select.appendChild(opt);
                    } else {
                        const ph = document.createElement('option');
                        ph.value = '';
                        ph.textContent = `请选择仓库 (${reposCache.length}个)`;
                        ph.disabled = true;
                        ph.selected = true;
                        select.appendChild(ph);

                        reposCache.forEach(repo => {
                            const opt = document.createElement('option');
                            opt.value = repo.clone_url;
                            const icon = repo.private ? '🔒' : '🌐';
                            opt.textContent = `${icon} ${repo.full_name}`;
                            opt.dataset.desc = repo.description || '';
                            select.appendChild(opt);
                        });
                    }

                    select.onchange = function () {
                        const chosen = reposCache.find(r => r.clone_url === select.value);
                        const descEl = document.getElementById('project-clone-repo-desc');
                        const urlEl = document.getElementById('project-clone-url');
                        if (chosen) {
                            if (descEl) descEl.textContent = chosen.description || '';
                            if (urlEl) urlEl.value = chosen.clone_url;
                        } else {
                            if (descEl) descEl.textContent = '';
                        }
                    };
                })();
            }

            // ── Button handlers ──
            cancelBtn.onclick = () => finish(null);
            if (window.bindTouchButton) window.bindTouchButton(cancelBtn, () => cancelBtn.onclick());

            cloneBtn.onclick = () => {
                const urlEl = document.getElementById('project-clone-url');
                const selectEl = document.getElementById('project-clone-repo-select');
                let url = '';

                if (selectEl && selectEl.value) url = selectEl.value;
                if (!url && urlEl) url = urlEl.value.trim();
                if (!url) { safeToast('请选择仓库或输入地址', 'error'); return; }

                // Inject saved token into GitHub clone URLs (needed for private repos)
                if (url.includes('github.com') && !url.includes('@')) {
                    const tokenToUse = savedToken || '';
                    if (!tokenToUse && !isLoggedIn) {
                        // Not logged in — check manual token input
                        const tokenEl = document.getElementById('project-clone-token');
                        const manualToken = tokenEl ? tokenEl.value.trim() : '';
                        if (manualToken) {
                            fetch('/api/config', {
                                method: 'POST', headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ github_token: manualToken })
                            }).catch(() => {});
                            url = url.replace('https://', `https://${manualToken}@`);
                        }
                    } else if (tokenToUse) {
                        url = url.replace('https://', `https://${tokenToUse}@`);
                    }
                }
                finish({ url });
            };
            if (window.bindTouchButton) window.bindTouchButton(cloneBtn, () => cloneBtn.onclick());

            // ── Show ──
            overlay.classList.remove('hidden');
            overlay.onclick = (e) => { if (e.target === overlay) finish(null); };
            overlay.addEventListener('touchend', (e) => {
                if (e.target === overlay) { e.preventDefault(); finish(null); }
            }, { once: true });
        });
    }

    // ── Folder Picker ─────────────────────────────────────────────

    /**
     * Show folder picker for opening a project
     */
    async function showFolderPicker() {
        pickerPath = '';
        const pickerEl = document.getElementById('project-folder-picker');
        const infoEl = document.getElementById('project-info');
        const wsPicker = document.getElementById('workspace-picker');
        if (pickerEl) pickerEl.classList.remove('hidden');
        if (infoEl) infoEl.classList.add('hidden');
        if (wsPicker) wsPicker.classList.add('hidden');
        await loadPickerFolders('');
    }

    function hideFolderPicker() {
        const pickerEl = document.getElementById('project-folder-picker');
        const infoEl = document.getElementById('project-info');
        const wsPicker = document.getElementById('workspace-picker');
        if (pickerEl) pickerEl.classList.add('hidden');
        if (infoEl) infoEl.classList.remove('hidden');
        if (wsPicker) wsPicker.classList.add('hidden');
    }

    async function loadPickerFolders(path) {
        pickerPath = path;
        try {
            const params = path ? `?path=${encodeURIComponent(path)}` : '';
            const resp = await fetch(`/api/project/list_folders${params}`);
            if (!resp.ok) throw new Error('Failed to list folders');
            const data = await resp.json();

            // Update header path display
            const pathEl = document.getElementById('project-picker-path');
            if (pathEl) {
                pathEl.textContent = '/' + (data.current_path || '');
            }

            // Show/hide back button
            const backBtn = document.getElementById('project-picker-back');
            if (backBtn) {
                backBtn.style.display = pickerPath ? '' : 'none';
            }

            // Render folder list with "设为项目" button per entry
            const listEl = document.getElementById('project-picker-list');
            if (!listEl) return;

            if (data.folders.length === 0) {
                listEl.innerHTML = '<div style="text-align:center;padding:20px;color:var(--text-muted);font-size:12px;">此目录下没有文件夹</div>';
                return;
            }

            let html = '';
            for (const folder of data.folders) {
                const gitIcon = folder.has_git ? ' 🔀' : '';
                html += `
                    <div class="project-folder-item" data-path="${escapeAttr(folder.path)}">
                        <div class="project-folder-info" data-path="${escapeAttr(folder.path)}">
                            <span class="icon">📁</span>
                            <span class="name">${escapeHTML(folder.name)}</span>
                            <span class="git-badge">${gitIcon}</span>
                        </div>
                        <button class="project-folder-set-btn" data-path="${escapeAttr(folder.path)}" title="设为项目">设为项目</button>
                    </div>`;
            }

            listEl.innerHTML = html;

            // Bind click events for folder navigation (clicking the folder info area navigates in)
            listEl.querySelectorAll('.project-folder-info').forEach(item => {
                const handler = async () => {
                    const itemPath = item.dataset.path;
                    await loadPickerFolders(itemPath);
                };
                if (window.bindTouchButton) {
                    window.bindTouchButton(item, handler);
                } else {
                    item.addEventListener('click', handler);
                }
            });

            // Bind click events for "设为项目" buttons
            listEl.querySelectorAll('.project-folder-set-btn').forEach(btn => {
                const handler = async (e) => {
                    e.stopPropagation();
                    const itemPath = btn.dataset.path;
                    await openProject(itemPath);
                };
                if (window.bindTouchButton) {
                    window.bindTouchButton(btn, handler);
                } else {
                    btn.addEventListener('click', handler);
                }
            });
        } catch (err) {
            safeToast('加载文件夹失败: ' + err.message, 'error');
        }
    }

    async function pickerGoBack() {
        if (!pickerPath) return;
        const parts = pickerPath.split('/');
        parts.pop();
        const parentPath = parts.join('/');
        await loadPickerFolders(parentPath);
    }



    /**
     * Auto-detect and activate virtual environment for the current project.
     * Checks for common venv directories (.venv, venv, env) in the project root.
     */
    async function autoActivateVenv() {
        try {
            const resp = await fetch('/api/venv/list');
            if (!resp.ok) return;
            const data = await resp.json();
            const venvs = data.venvs || [];
            const current = data.current || '';

            // If already activated, just update UI
            if (current) {
                updateVenvUI(current);
                return;
            }

            // Auto-activate the first found venv
            if (venvs.length > 0) {
                const venv = venvs[0];
                const activateResp = await fetch('/api/venv/activate', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ path: venv.path })
                });
                if (activateResp.ok) {
                    safeToast(`已自动加载虚拟环境: ${venv.name}`, 'success');
                    updateVenvUI(venv.full_path || venv.path);
                }
            }
        } catch (err) {
            console.warn('[ProjectManager] Auto venv detection failed:', err);
        }
    }

    /**
     * Update venv-related UI elements after activation.
     */
    function updateVenvUI(venvPath) {
        // Update the venv info in the debug panel
        const venvInfoEl = document.getElementById('current-venv');
        if (venvInfoEl) {
            const name = venvPath.split(/[/\\]/).pop() || venvPath;
            venvInfoEl.textContent = name;
        }
        // Notify debug panel to refresh packages
        if (window.DebugManager && typeof window.DebugManager.refreshPackages === 'function') {
            window.DebugManager.refreshPackages();
        }
    }

    // ── UI Updates ─────────────────────────────────────────────────

    function onProjectOpened(data) {
        // Update header title
        const titleEl = document.getElementById('project-title');
        if (titleEl) {
            titleEl.textContent = ' - ' + data.name;
            titleEl.classList.remove('hidden');
        }

        // Update close button visibility
        const closeBtn = document.getElementById('btn-close-project');
        if (closeBtn) closeBtn.style.display = '';

        // Update project info panel
        const currentEl = document.getElementById('project-current');
        if (currentEl) {
            currentEl.className = 'project-active';
            const gitStatus = data.has_git ? '🔀 Git 仓库' : '';
            currentEl.innerHTML = `
                <div style="padding:12px;">
                    <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
                        <span style="font-size:20px;">📁</span>
                        <span style="font-size:14px;font-weight:600;color:var(--text-primary);">${escapeHTML(data.name)}</span>
                    </div>
                    <div style="font-size:11px;color:var(--text-muted);font-family:var(--font-mono);word-break:break-all;">${escapeHTML(data.path || data.project)}</div>
                    <div style="font-size:11px;color:var(--text-secondary);margin-top:4px;">${gitStatus}</div>
                </div>`;
        }

        // Show project-only tabs (Git, Debug)
        showProjectTabs(true);

        // Dispatch event for other modules (AI assistant, etc.)
        document.dispatchEvent(new CustomEvent('project:opened', { detail: data }));

        // Hide folder picker if open
        hideFolderPicker();
    }

    function onProjectClosed() {
        // Update header title
        const titleEl = document.getElementById('project-title');
        if (titleEl) {
            titleEl.textContent = '';
            titleEl.classList.add('hidden');
        }

        // Update close button visibility
        const closeBtn = document.getElementById('btn-close-project');
        if (closeBtn) closeBtn.style.display = 'none';

        // Update project info panel
        const currentEl = document.getElementById('project-current');
        if (currentEl) {
            currentEl.className = 'project-no-project';
            currentEl.innerHTML = `
                <div style="text-align:center;padding:30px 12px;">
                    <div style="font-size:36px;margin-bottom:10px;">📁</div>
                    <div style="font-size:13px;color:var(--text-secondary);margin-bottom:6px;">未打开项目</div>
                    <div style="font-size:11px;color:var(--text-muted);">点击「新建」创建项目，或点击「打开」选择文件夹，或点击「克隆」从远程克隆</div>
                </div>`;
        }

        // Hide project-only tabs (Git, Debug) - reverse of onProjectOpened
        showProjectTabs(false);

        // Switch away from hidden tabs if currently active
        switchAwayFromProjectTabs();

        // Dispatch event for other modules
        document.dispatchEvent(new CustomEvent('project:closed'));
    }

    function switchToFilesTab() {
        // Click the files tab
        const filesTab = document.querySelector('#left-tabs .tab[data-tab="files"]');
        if (filesTab) filesTab.click();
    }

    function switchToProjectTab() {
        // Switch to project tab (reverse of switchToFilesTab)
        const projectTab = document.querySelector('#left-tabs .tab[data-tab="project"]');
        if (projectTab) projectTab.click();
    }

    /**
     * Show/hide project-only tabs (Git, Debug)
     * @param {boolean} show - true to show, false to hide
     */
    function showProjectTabs(show) {
        document.querySelectorAll('#left-tabs .tab-project-only').forEach(tab => {
            tab.style.display = show ? '' : 'none';
        });
    }

    /**
     * If currently active tab is a project-only tab (git/debug),
     * switch to the project tab instead.
     */
    function switchAwayFromProjectTabs() {
        const activeTab = document.querySelector('#left-tabs .tab.active');
        if (activeTab && activeTab.classList.contains('tab-project-only')) {
            // Switch to project tab
            switchToProjectTab();
        }
    }

    // ── Wire Up Buttons ────────────────────────────────────────────

    function wireButtons() {
        const setWsBtn = document.getElementById('btn-set-workspace');
        if (setWsBtn) {
            const handler = () => showWorkspacePicker();
            if (window.bindTouchButton) {
                window.bindTouchButton(setWsBtn, handler);
            } else {
                setWsBtn.addEventListener('click', handler);
            }
        }

        const changeWsBtn = document.getElementById('btn-change-workspace');
        if (changeWsBtn) {
            const handler = () => showWorkspacePicker();
            if (window.bindTouchButton) {
                window.bindTouchButton(changeWsBtn, handler);
            } else {
                changeWsBtn.addEventListener('click', handler);
            }
        }

        // Workspace picker buttons
        const wsBackBtn = document.getElementById('ws-picker-back');
        if (wsBackBtn) {
            const handler = async () => {
                if (wsPickerPath && wsPickerPath !== '/') {
                    const parent = wsPickerPath.split('/').slice(0, -1).join('/') || '/';
                    await browseWorkspace(parent);
                }
            };
            if (window.bindTouchButton) {
                window.bindTouchButton(wsBackBtn, handler);
            } else {
                wsBackBtn.addEventListener('click', handler);
            }
        }

        const wsCancelBtn = document.getElementById('ws-picker-cancel');
        if (wsCancelBtn) {
            const handler = () => {
                hideWorkspacePicker();
                // If no workspace set, still show the project info
            };
            if (window.bindTouchButton) {
                window.bindTouchButton(wsCancelBtn, handler);
            } else {
                wsCancelBtn.addEventListener('click', handler);
            }
        }

        const wsConfirmBtn = document.getElementById('ws-picker-confirm');
        if (wsConfirmBtn) {
            const handler = () => confirmWorkspace();
            if (window.bindTouchButton) {
                window.bindTouchButton(wsConfirmBtn, handler);
            } else {
                wsConfirmBtn.addEventListener('click', handler);
            }
        }

        const openBtn = document.getElementById('btn-open-project');
        if (openBtn) {
            const handler = () => showFolderPicker();
            if (window.bindTouchButton) {
                window.bindTouchButton(openBtn, handler);
            } else {
                openBtn.addEventListener('click', handler);
            }
        }

        const newBtn = document.getElementById('btn-new-project');
        if (newBtn) {
            const handler = () => newProject();
            if (window.bindTouchButton) {
                window.bindTouchButton(newBtn, handler);
            } else {
                newBtn.addEventListener('click', handler);
            }
        }

        const cloneBtn = document.getElementById('btn-clone-project');
        if (cloneBtn) {
            const handler = () => cloneProject();
            if (window.bindTouchButton) {
                window.bindTouchButton(cloneBtn, handler);
            } else {
                cloneBtn.addEventListener('click', handler);
            }
        }

        const closeBtn = document.getElementById('btn-close-project');
        if (closeBtn) {
            const handler = () => {
                if (window.showConfirmDialog) {
                    window.showConfirmDialog('关闭项目', '确定要关闭当前项目吗？文件视图将返回工作区。', (confirmed) => {
                        if (confirmed) closeProject();
                    });
                } else {
                    if (confirm('确定要关闭当前项目吗？')) closeProject();
                }
            };
            if (window.bindTouchButton) {
                window.bindTouchButton(closeBtn, handler);
            } else {
                closeBtn.addEventListener('click', handler);
            }
        }

        const backBtn = document.getElementById('project-picker-back');
        if (backBtn) {
            const handler = () => pickerGoBack();
            if (window.bindTouchButton) {
                window.bindTouchButton(backBtn, handler);
            } else {
                backBtn.addEventListener('click', handler);
            }
        }

        // "select" button removed - each folder now has its own "设为项目" button
    }

    // ── Initialize ─────────────────────────────────────────────────

    let _wired = false;
    function ensureWired() {
        if (_wired) return;
        if (window.bindTouchButton) {
            _wired = true;
            wireButtons();
            loadWorkspaceInfo();
            loadProjectInfo();
        } else {
            const check = setInterval(() => {
                if (window.bindTouchButton) {
                    clearInterval(check);
                    _wired = true;
                    wireButtons();
                    loadWorkspaceInfo();
                    loadProjectInfo();
                }
            }, 10);
            setTimeout(() => {
                clearInterval(check);
                if (!_wired) {
                    _wired = true;
                    wireButtons();
                    loadWorkspaceInfo();
                    loadProjectInfo();
                }
            }, 500);
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', ensureWired);
    } else {
        ensureWired();
    }

    // ── Public API ─────────────────────────────────────────────────
    return {
        loadProjectInfo,
        openProject,
        newProject,
        closeProject,
        cloneProject,
        getCurrentProject: () => currentProject,
        loadWorkspaceInfo,
        getCurrentWorkspace: () => currentWorkspace,
    };
})();

window.ProjectManager = ProjectManager;
