/**
 * BrowserInspector - Preview iframe + bridge for AI browser debugging tools.
 *
 * Architecture:
 *   Backend AI tool → create_browser_command() → frontend polls /api/browser/poll
 *   → frontend executes in iframe → frontend POSTs /api/browser/result → tool returns
 *
 * Supports any URL. Same-origin pages (localhost) allow full DOM inspection;
 * cross-origin pages will load if permitted by the server, but DOM access will fail.
 */
const BrowserInspector = (() => {
    'use strict';

    // ── State ──
    let iframe = null;
    let pollTimer = null;
    let bridgeInjected = false;
    let iframeLogs = [];  // console logs captured from iframe
    let pollInterval = 600;  // ms between polls

    // ── Helpers ──
    function escapeHTML(str) {
        const div = document.createElement('div');
        div.textContent = str || '';
        return div.innerHTML;
    }

    function tryIframeWin() {
        try { return iframe && iframe.contentWindow; } catch (e) { return null; }
    }
    function tryIframeDoc() {
        try { return iframe && iframe.contentDocument; } catch (e) { return null; }
    }

    // ── Bridge Injection ──
    /**
     * Inject a console interceptor script into the iframe.
     * Captures console.log/warn/error/info and forwards via postMessage.
     * Also listens for window.error events.
     */
    function injectBridge() {
        const win = tryIframeWin();
        const doc = tryIframeDoc();
        if (!win || !doc) {
            if (window.showToast) window.showToast('无法访问 iframe（跨域或未加载）', 'warning');
            return false;
        }
        try {
            // Remove old bridge if exists
            const old = doc.getElementById('muside-bridge');
            if (old) old.remove();

            const script = doc.createElement('script');
            script.id = 'muside-bridge';
            script.textContent = `(function(){
                var _L=console.log,_W=console.warn,_E=console.error,_I=console.info;
                function _s(t,a){
                    try{
                        var args=[];
                        for(var i=0;i<a.length;i++){
                            var v=a[i];
                            if(v===null) args.push('null');
                            else if(v===undefined) args.push('undefined');
                            else if(typeof v==='object'){
                                try{args.push(JSON.stringify(v,null,2).substring(0,2000));}catch(e){args.push(String(v));}
                            }
                            else args.push(String(v));
                        }
                        window.parent.postMessage({
                            source:'pide-bridge',
                            type:t,
                            text:args.join(' ')
                        },'*');
                    }catch(ex){}
                }
                console.log=function(){_s('log',arguments);_L.apply(console,arguments);};
                console.warn=function(){_s('warn',arguments);_W.apply(console,arguments);};
                console.error=function(){_s('error',arguments);_E.apply(console,arguments);};
                console.info=function(){_s('info',arguments);_I.apply(console,arguments);};
                window.addEventListener('error',function(e){
                    try{
                        window.parent.postMessage({
                            source:'pide-bridge',
                            type:'uncaught',
                            text:e.message+' at '+e.filename+':'+e.lineno+':'+e.colno
                        },'*');
                    }catch(ex){}
                });
                window.addEventListener('unhandledrejection',function(e){
                    try{
                        var r=e.reason;
                        window.parent.postMessage({
                            source:'pide-bridge',
                            type:'promise',
                            text:'Unhandled: '+(r&&r.message?r.message:String(r))
                        },'*');
                    }catch(ex){}
                });
                _I('[MusIDE] Bridge injected successfully');
            })();`;
            doc.head.appendChild(script);
            bridgeInjected = true;
            if (window.showToast) window.showToast('Bridge 已注入', 'success', 1500);
            return true;
        } catch (e) {
            if (window.showToast) window.showToast('注入失败: ' + e.message, 'error');
            return false;
        }
    }

    // ── Listen for bridge messages from iframe ──
    function initBridgeListener() {
        window.addEventListener('message', function (e) {
            if (!e.data || e.data.source !== 'pide-bridge') return;
            const logEntry = {
                type: e.data.type || 'log',
                text: e.data.text || '',
                time: new Date().toLocaleTimeString(),
            };
            iframeLogs.push(logEntry);
            if (iframeLogs.length > 500) iframeLogs.splice(0, iframeLogs.length - 500);

            // Auto-refresh panels if visible
            const logsPanel = document.getElementById('iframe-logs-panel');
            if (logsPanel && logsPanel.style.display !== 'none') {
                renderIframeLogs();
            }
            const consolePanel = document.getElementById('iframe-console-panel');
            if (consolePanel && consolePanel.style.display !== 'none') {
                // Only refresh console panel for error types
                if (logEntry.type === 'error' || logEntry.type === 'uncaught' || logEntry.type === 'promise') {
                    renderConsoleErrors();
                }
            }
        });
    }

    // ── iframe DOM Operations ──

    function evaluate(expression) {
        const win = tryIframeWin();
        if (!win) return { error: '无法访问 iframe（跨域或未加载页面）' };
        try {
            // Wrap expression to auto-return the last value.
            // Strategy: try direct eval first. If undefined, re-run as a return-wrapped
            // function so that const/let/var declarations still produce a return value.
            let result = win.eval(expression);

            if (result === undefined && expression.trim()) {
                // Try wrapping in a function body with return.
                // This converts "const x = 1" into "(function(){ const x = 1; return x })()"
                // We extract the last declared or assigned variable name.
                try {
                    // Match trailing variable declarations: const/let/var name = ...
                    const declMatch = expression.match(/(?:const|let|var)\s+(\w+)\s*=/g);
                    // Match trailing assignment: name = ... (without const/let/var)
                    const assignMatch = expression.match(/(\w+)\s*=[^=]/g);

                    let returnExpr = '';
                    if (declMatch) {
                        // Use the last declared variable name
                        const lastDecl = declMatch[declMatch.length - 1];
                        returnExpr = lastDecl.match(/(?:const|let|var)\s+(\w+)/)[1];
                    } else if (assignMatch) {
                        // Use the last assigned variable name
                        const lastAssign = assignMatch[assignMatch.length - 1];
                        returnExpr = lastAssign.match(/(\w+)/)[1];
                    }

                    if (returnExpr) {
                        // Wrap in IIFE so const/let are scoped properly, then return the variable
                        const wrapped = '(function(){ ' + expression + '; return ' + returnExpr + '; })()';
                        const wrappedResult = win.eval(wrapped);
                        if (wrappedResult !== undefined) {
                            result = wrappedResult;
                        }
                    }
                } catch (e) { /* fallback to original undefined result */ }
            }

            // Serialize result
            if (result === undefined) return { ok: true, result: 'undefined' };
            if (result === null) return { ok: true, result: 'null' };
            if (typeof result === 'object') {
                // For DOM elements, return a detailed summary
                if (result.nodeType) {
                    const tag = result.tagName || result.nodeName || '?';
                    const id = result.id ? '#' + result.id : '';
                    const cls = (typeof result.className === 'string' && result.className)
                        ? '.' + result.className.split(/\s+/).filter(Boolean).join('.') : '';
                    const text = (result.textContent || '').trim().substring(0, 1000);
                    const children = result.children ? result.children.length : 0;
                    const html = (result.outerHTML || '').substring(0, 3000);
                    return {
                        ok: true,
                        result: `<${tag}${id}${cls}> children=${children} text="${text}"\nHTML: ${html}`,
                        truncated: (result.outerHTML || '').length > 3000,
                        fullLength: (result.outerHTML || '').length,
                    };
                }
                // For other objects, try JSON with generous limit
                try {
                    const json = JSON.stringify(result, null, 2);
                    return {
                        ok: true,
                        result: json.substring(0, 10000),
                        truncated: json.length > 10000,
                        fullLength: json.length,
                    };
                } catch (e) {
                    const str = String(result);
                    return {
                        ok: true,
                        result: str.substring(0, 10000),
                        truncated: str.length > 10000,
                        fullLength: str.length,
                    };
                }
            }
            return { ok: true, result: String(result) };
        } catch (e) {
            return { error: e.message };
        }
    }

    function inspectElement(selector) {
        const doc = tryIframeDoc();
        const win = tryIframeWin();
        if (!doc || !win) return { error: '无法访问 iframe' };
        try {
            const el = doc.querySelector(selector);
            if (!el) return { error: '未找到元素: ' + selector };

            const rect = el.getBoundingClientRect();
            const computed = win.getComputedStyle(el);

            // Collect attributes
            const attrs = {};
            for (const attr of el.attributes) {
                attrs[attr.name] = attr.value;
            }

            return {
                ok: true,
                tagName: el.tagName,
                id: el.id || '',
                className: (typeof el.className === 'string' ? el.className : el.className.baseVal) || '',
                href: el.href || '',
                src: el.src || '',
                type: el.type || '',
                name: el.name || '',
                value: el.value !== undefined && el.value !== null ? String(el.value).substring(0, 500) : '',
                placeholder: el.placeholder || '',
                textContent: (el.textContent || '').trim().substring(0, 500),
                innerHTML: (el.innerHTML || '').substring(0, 2000),
                attributes: attrs,
                rect: {
                    top: Math.round(rect.top),
                    left: Math.round(rect.left),
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                    bottom: Math.round(rect.bottom),
                    right: Math.round(rect.right),
                },
                visible: rect.width > 0 && rect.height > 0 && computed.display !== 'none' && computed.visibility !== 'hidden',
                display: computed.display,
                visibility: computed.visibility,
                opacity: computed.opacity,
                zIndex: computed.zIndex,
                color: computed.color,
                backgroundColor: computed.backgroundColor,
                fontSize: computed.fontSize,
                fontWeight: computed.fontWeight,
                childCount: el.children.length,
                parentTag: el.parentElement ? el.parentElement.tagName : '',
            };
        } catch (e) {
            return { error: e.message };
        }
    }

    function queryAll(selector) {
        const doc = tryIframeDoc();
        if (!doc) return { error: '无法访问 iframe' };
        try {
            const elements = doc.querySelectorAll(selector);
            const results = [];
            for (let i = 0; i < Math.min(elements.length, 50); i++) {
                const el = elements[i];
                const rect = el.getBoundingClientRect();
                results.push({
                    index: i,
                    tagName: el.tagName,
                    id: el.id || '',
                    className: (typeof el.className === 'string' ? el.className : '').substring(0, 200),
                    textContent: (el.textContent || '').trim().substring(0, 100),
                    href: el.href || '',
                    src: el.src || '',
                    visible: rect.width > 0 && rect.height > 0,
                    rect: { top: Math.round(rect.top), left: Math.round(rect.left), width: Math.round(rect.width), height: Math.round(rect.height) },
                });
            }
            return { ok: true, total: elements.length, shown: results.length, elements: results };
        } catch (e) {
            return { error: e.message };
        }
    }

    function simulateClick(selector) {
        const doc = tryIframeDoc();
        if (!doc) return { error: '无法访问 iframe' };
        try {
            const el = doc.querySelector(selector);
            if (!el) return { error: '未找到元素: ' + selector };
            el.click();
            return { ok: true, message: '已点击: ' + selector };
        } catch (e) {
            return { error: e.message };
        }
    }

    function simulateInput(selector, text, clearFirst) {
        const doc = tryIframeDoc();
        const win = tryIframeWin();
        if (!doc || !win) return { error: '无法访问 iframe' };
        try {
            const el = doc.querySelector(selector);
            if (!el) return { error: '未找到元素: ' + selector };

            // Use native setter for React/Vue compatibility
            if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
                const proto = el.tagName === 'TEXTAREA' ? win.HTMLTextAreaElement.prototype : win.HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value') && Object.getOwnPropertyDescriptor(proto, 'value').set;
                if (setter) {
                    setter.call(el, text);
                } else {
                    el.value = text;
                }
                el.dispatchEvent(new win.Event('input', { bubbles: true }));
                el.dispatchEvent(new win.Event('change', { bubbles: true }));
            } else {
                // For contenteditable elements
                el.textContent = text;
                el.dispatchEvent(new win.Event('input', { bubbles: true }));
            }
            return { ok: true, message: '已输入到: ' + selector, length: text.length };
        } catch (e) {
            return { error: e.message };
        }
    }

    function simulateKeyPress(key, selector) {
        const doc = tryIframeDoc();
        const win = tryIframeWin();
        if (!doc || !win) return { error: '无法访问 iframe' };
        try {
            const target = selector ? doc.querySelector(selector) : doc.body;
            if (!target && selector) return { error: '未找到元素: ' + selector };
            const el = target || doc.body;
            el.dispatchEvent(new win.KeyboardEvent('keydown', { key: key, code: key, bubbles: true, cancelable: true }));
            el.dispatchEvent(new win.KeyboardEvent('keyup', { key: key, code: key, bubbles: true }));
            return { ok: true, message: '按键: ' + key + (selector ? ' on ' + selector : '') };
        } catch (e) {
            return { error: e.message };
        }
    }

    function getCookies() {
        const doc = tryIframeDoc();
        if (!doc) return { error: '无法访问 iframe（跨域无法读取 Cookie）' };
        try {
            const cookieStr = doc.cookie || '';
            if (!cookieStr) return { ok: true, cookies: '(无 Cookie)', count: 0 };
            const cookies = cookieStr.split(';').map(c => c.trim()).filter(Boolean);
            const parsed = cookies.map(c => {
                const [name, ...rest] = c.split('=');
                return { name: name.trim(), value: rest.join('=').trim() };
            });
            return { ok: true, cookies: parsed, raw: cookieStr, count: parsed.length };
        } catch (e) {
            return { error: '无法读取 Cookie: ' + e.message + '（可能跨域限制）' };
        }
    }

    function getConsoleLogs(sinceIndex) {
        const start = (typeof sinceIndex === 'number') ? sinceIndex : 0;
        const logs = iframeLogs.slice(start);
        return { ok: true, total: iframeLogs.length, from: start, count: logs.length, logs: logs };
    }

    function navigate(url) {
        if (!iframe) return { error: '预览框架不可用' };
        if (!url) return { error: 'URL 不能为空' };
        if (!url.startsWith('http://') && !url.startsWith('https://')) {
            // Use http:// for localhost/local IPs, https:// for public domains
            url = 'http://' + url;
        }
        // Route through backend proxy to bypass X-Frame-Options / CSP restrictions
        const proxyUrl = '/api/browser/proxy?url=' + encodeURIComponent(url);
        iframe.src = proxyUrl;
        bridgeInjected = false;
        // Store original URL for external browser button
        if (urlInput) urlInput.dataset.originalUrl = url;
        return { ok: true, message: '正在导航到: ' + url };
    }

    async function openExternal(url) {
        if (!url) return;
        try {
            const origFetch = window._origFetch || window.fetch.bind(window);
            const resp = await origFetch('/api/browser/open-external', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url })
            });
            const data = await resp.json();
            if (data.ok) {
                if (window.showToast) window.showToast('已在外部浏览器中打开', 'success', 1500);
            } else {
                if (window.showToast) window.showToast('打开失败: ' + (data.error || ''), 'error');
            }
        } catch (e) {
            // Fallback: try window.open
            try {
                window.open(url, '_blank');
            } catch (ex) {
                if (window.showToast) window.showToast('无法打开外部浏览器', 'error');
            }
        }
    }

    function getPageInfo() {
        const doc = tryIframeDoc();
        const win = tryIframeWin();
        if (!doc || !win) return { error: '无法访问 iframe' };
        try {
            return {
                ok: true,
                title: doc.title || '',
                url: win.location.href || '',
                charset: doc.characterSet || '',
                contentType: doc.contentType || '',
                bodyChildCount: doc.body ? doc.body.children.length : 0,
                htmlLength: doc.documentElement.outerHTML.length,
                viewport: {
                    width: win.innerWidth,
                    height: win.innerHeight,
                },
                scrollPosition: {
                    x: win.scrollX || win.pageXOffset,
                    y: win.scrollY || win.pageYOffset,
                },
            };
        } catch (e) {
            return { error: e.message };
        }
    }

    // ── Command Dispatcher ──

    function executeCommand(action, params) {
        switch (action) {
            case 'evaluate':
                return evaluate(params.expression || '');
            case 'inspect':
                return inspectElement(params.selector || 'body');
            case 'query_all':
                return queryAll(params.selector || '*');
            case 'click':
                return simulateClick(params.selector || '');
            case 'input':
                return simulateInput(params.selector || '', params.text || '', params.clearFirst);
            case 'keypress':
                return simulateKeyPress(params.key || '', params.selector);
            case 'cookies':
                return getCookies();
            case 'console':
                return getConsoleLogs(params.sinceIndex);
            case 'navigate':
                return navigate(params.url || '');
            case 'page_info':
                return getPageInfo();
            default:
                return { error: '未知操作: ' + action };
        }
    }

    // ── Poll Backend for Pending Commands ──

    async function pollCommand() {
        try {
            // Use real fetch (not the intercepted one from DebugManager)
            const origFetch = window._origFetch || window.fetch.bind(window);
            const resp = await origFetch('/api/browser/poll');
            if (!resp.ok) return;
            const data = await resp.json();
            if (!data.cmd_id) return;

            // Execute the command
            const result = executeCommand(data.action, data.params || {});

            // Post result back
            try {
                await origFetch('/api/browser/result', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        cmd_id: data.cmd_id,
                        result: result,
                    }),
                });
            } catch (postErr) {
                // If posting the result also fails, report the error to backend
                try {
                    await origFetch('/api/browser/result', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            cmd_id: data.cmd_id,
                            error: 'Frontend failed to report result: ' + postErr.message,
                        }),
                    });
                } catch (e) { /* give up */ }
            }
        } catch (e) {
            // Log poll errors (but don't spam — only log once per 10s)
            if (!pollCommand._lastLog || Date.now() - pollCommand._lastLog > 10000) {
                console.warn('[BrowserInspector] poll error:', e.message);
                pollCommand._lastLog = Date.now();
            }
        }
    }

    // ── Render iframe console logs in a mini-panel ──

    function renderIframeLogs() {
        const container = document.getElementById('iframe-logs');
        if (!container) return;
        container.innerHTML = '';
        if (iframeLogs.length === 0) {
            container.innerHTML = '<div style="color:var(--text-muted);padding:8px;font-size:11px;text-align:center;">暂无日志（注入 Bridge 后自动捕获）</div>';
            return;
        }
        const start = Math.max(0, iframeLogs.length - 80);
        for (let i = start; i < iframeLogs.length; i++) {
            const log = iframeLogs[i];
            const div = document.createElement('div');
            div.style.cssText = 'padding:3px 8px;font-size:11px;border-bottom:1px solid var(--border);font-family:var(--font-mono);';

            let color = 'var(--text-secondary)';
            if (log.type === 'error' || log.type === 'uncaught') color = 'var(--red)';
            else if (log.type === 'warn') color = 'var(--yellow)';
            else if (log.type === 'info') color = 'var(--blue)';

            div.innerHTML =
                '<span style="color:var(--text-muted);font-size:9px;margin-right:4px;">' + escapeHTML(log.time) + '</span>' +
                '<span style="color:' + color + ';font-size:10px;font-weight:bold;margin-right:4px;">' + escapeHTML(log.type) + '</span>' +
                '<span style="color:var(--text-secondary);word-break:break-all;">' + escapeHTML(log.text).substring(0, 300) + '</span>';
            container.appendChild(div);
        }
        container.scrollTop = container.scrollHeight;
    }

    // ── Render console errors panel (error/uncaught/promise only) ──

    function renderConsoleErrors() {
        const container = document.getElementById('iframe-console-errors');
        const countEl = document.getElementById('iframe-console-count');
        if (!container) return;

        // Filter only errors
        const errors = iframeLogs.filter(function (log) {
            return log.type === 'error' || log.type === 'uncaught' || log.type === 'promise';
        });

        // Update count badge
        if (countEl) {
            countEl.textContent = errors.length > 0 ? errors.length + ' 个错误' : '';
        }

        container.innerHTML = '';
        if (errors.length === 0) {
            container.innerHTML = '<div style="color:var(--text-muted);padding:8px;font-size:11px;text-align:center;">无 JS 错误 🎉</div>';
            return;
        }

        for (let i = 0; i < errors.length; i++) {
            const log = errors[i];
            const div = document.createElement('div');
            div.style.cssText = 'padding:3px 8px;font-size:11px;border-bottom:1px solid var(--border);font-family:var(--font-mono);';

            let label = log.type;
            if (log.type === 'uncaught') label = 'uncaught';
            if (log.type === 'promise') label = 'promise';

            div.innerHTML =
                '<span style="color:var(--text-muted);font-size:9px;margin-right:4px;">' + escapeHTML(log.time) + '</span>' +
                '<span style="color:var(--red);font-size:10px;font-weight:bold;margin-right:4px;">' + escapeHTML(label) + '</span>' +
                '<span style="color:var(--text-secondary);word-break:break-all;">' + escapeHTML(log.text).substring(0, 500) + '</span>';
            container.appendChild(div);
        }
        container.scrollTop = container.scrollHeight;
    }

    // ── Send console errors to AI assistant ──

    function sendErrorsToAI() {
        const errors = iframeLogs.filter(function (log) {
            return log.type === 'error' || log.type === 'uncaught' || log.type === 'promise';
        });

        if (errors.length === 0) {
            if (window.showToast) window.showToast('当前没有 JS 错误', 'info');
            return;
        }

        // Build error report
        const url = document.getElementById('browser-url-input');
        const urlStr = url ? (url.dataset.originalUrl || url.value.trim()) : '';
        let report = '🐛 **预览页面 JS 错误报告**\n\n';
        if (urlStr) {
            report += '页面地址: ' + urlStr + '\n\n';
        }
        report += '共 ' + errors.length + ' 个错误:\n\n';
        for (let i = 0; i < errors.length; i++) {
            const e = errors[i];
            report += (i + 1) + '. [' + e.type + '] ' + e.time + ' — ' + e.text + '\n';
        }
        report += '\n请帮我分析并修复这些 JS 错误。';

        // Send to AI chat
        if (window.ChatManager && window.ChatManager.sendMessage) {
            window.ChatManager.sendMessage(report);
            if (window.showToast) window.showToast('已发送 ' + errors.length + ' 个错误给 AI 助手', 'success', 1500);
        } else {
            // Fallback: paste into chat input
            const input = document.getElementById('chat-input');
            if (input) {
                input.value = report;
                input.focus();
                if (window.showToast) window.showToast('已粘贴到输入框，请手动发送', 'info');
            }
        }
    }

    // ── Init ──

    function init() {
        iframe = document.getElementById('preview-frame');

        // Save original fetch before DebugManager wraps it
        if (window.fetch && !window._origFetch) {
            window._origFetch = window.fetch.bind(window);
        }

        initBridgeListener();

        // Start polling
        pollTimer = setInterval(pollCommand, pollInterval);

        // Wire UI buttons
        wireUI();

        // Watch iframe load
        if (iframe) {
            iframe.addEventListener('load', function () {
                bridgeInjected = false;
                // Auto-inject bridge after load
                setTimeout(function () {
                    injectBridge();
                }, 500);
            });
        }
    }

    function wireUI() {
        // Inject button
        const injectBtn = document.getElementById('browser-inject-btn');
        if (injectBtn) {
            injectBtn.addEventListener('click', function () { injectBridge(); });
        }

        // Navigate / Go button
        const goBtn = document.getElementById('browser-go-btn');
        const externalBtn = document.getElementById('browser-external-btn');
        const urlInput = document.getElementById('browser-url-input');
        if (goBtn && urlInput) {
            goBtn.addEventListener('click', function () {
                const url = urlInput.value.trim();
                if (url) navigate(url);
            });
            urlInput.addEventListener('keydown', function (e) {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    const url = urlInput.value.trim();
                    if (url) navigate(url);
                }
            });
        }

        // External browser button — use stored original URL
        if (externalBtn && urlInput) {
            externalBtn.addEventListener('click', function () {
                // Prefer the stored original URL over the raw input value
                const url = urlInput.dataset.originalUrl || urlInput.value.trim();
                if (url) openExternal(url);
                else if (window.showToast) window.showToast('请先输入网址', 'warning');
            });
        }

        // Refresh iframe
        const refreshBtn = document.getElementById('browser-refresh-btn');
        if (refreshBtn && iframe) {
            refreshBtn.addEventListener('click', function () {
                if (iframe.src && !iframe.src.endsWith('about:blank')) {
                    // Force reload by toggling src
                    const currentSrc = iframe.src;
                    iframe.src = 'about:blank';
                    setTimeout(function () { iframe.src = currentSrc; }, 50);
                }
            });
        }

        // Show iframe logs button
        const logsBtn = document.getElementById('browser-logs-btn');
        const logsPanel = document.getElementById('iframe-logs-panel');
        if (logsBtn && logsPanel) {
            logsBtn.addEventListener('click', function () {
                const visible = logsPanel.style.display !== 'none';
                logsPanel.style.display = visible ? 'none' : '';
                if (!visible) renderIframeLogs();
                logsBtn.textContent = visible ? '📋 日志' : '📋 关闭';
            });
        }

        // Clear iframe logs
        const clearLogsBtn = document.getElementById('iframe-logs-clear');
        if (clearLogsBtn) {
            clearLogsBtn.addEventListener('click', function () {
                iframeLogs = [];
                renderIframeLogs();
                renderConsoleErrors();
            });
        }

        // Console errors button
        const consoleBtn = document.getElementById('browser-console-btn');
        const consolePanel = document.getElementById('iframe-console-panel');
        if (consoleBtn && consolePanel) {
            consoleBtn.addEventListener('click', function () {
                const visible = consolePanel.style.display !== 'none';
                consolePanel.style.display = visible ? 'none' : 'flex';
                if (!visible) renderConsoleErrors();
                consoleBtn.textContent = visible ? '🐛 Console' : '🐛 关闭';
            });
        }

        // Clear console errors
        const clearConsoleBtn = document.getElementById('iframe-console-clear');
        if (clearConsoleBtn) {
            clearConsoleBtn.addEventListener('click', function () {
                // Remove error-type logs from iframeLogs
                iframeLogs = iframeLogs.filter(function (log) {
                    return log.type !== 'error' && log.type !== 'uncaught' && log.type !== 'promise';
                });
                renderConsoleErrors();
                renderIframeLogs();
            });
        }

        // Send console errors to AI assistant
        const sendAiBtn = document.getElementById('iframe-console-send-ai');
        if (sendAiBtn) {
            sendAiBtn.addEventListener('click', function () {
                sendErrorsToAI();
            });
        }
    }

    // ── Boot ──
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    return {
        init,
        injectBridge,
        evaluate,
        inspectElement,
        queryAll,
        simulateClick,
        simulateInput,
        simulateKeyPress,
        getCookies,
        getConsoleLogs,
        navigate,
        openExternal,
        getPageInfo,
        renderIframeLogs,
        renderConsoleErrors,
        sendErrorsToAI,
        get bridgeInjected() { return bridgeInjected; },
        get iframeLogs() { return iframeLogs; },
        get iframeReady() {
            try { return !!(iframe && iframe.contentDocument); }
            catch (e) { return false; }
        },
    };
})();

window.BrowserInspector = BrowserInspector;
