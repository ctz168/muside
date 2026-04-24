"""
MusIDE - Browser preview and inspection API.
Provides command queue for AI tools to control the preview iframe.
"""

import json
import uuid
import time
import ssl
import threading
import webbrowser
import subprocess
import re
import gzip
import zlib
import io
import http.client
import socket
import urllib.request
import urllib.error
import urllib.parse
from urllib.parse import urlparse, urljoin, urlencode

from flask import Blueprint, jsonify, request, Response, make_response
from utils import handle_error

# Try importing brotli (may not be installed)
try:
    import brotli
    _HAS_BROTLI = True
except ImportError:
    _HAS_BROTLI = False

bp = Blueprint('browser', __name__)

# ── In-memory command queue ──
# AI tool creates command → frontend polls & executes → frontend posts result → tool returns
_commands = {}  # cmd_id -> {action, params, status, result, event, created}
_lock = threading.Lock()
COMMAND_TIMEOUT = 20  # seconds


def _cleanup_old_commands():
    """Remove commands older than 60 seconds."""
    now = time.time()
    expired = [cid for cid, cmd in _commands.items() if now - cmd.get('created', 0) > 60]
    for cid in expired:
        cmd = _commands.pop(cid, None)
        if cmd and cmd.get('event'):
            cmd['event'].set()


def create_browser_command(action, params):
    """Create a pending browser command. Returns cmd_id."""
    _cleanup_old_commands()
    cmd_id = uuid.uuid4().hex[:8]
    with _lock:
        _commands[cmd_id] = {
            'action': action,
            'params': params,
            'status': 'pending',  # pending -> claimed -> done
            'result': None,
            'error': None,
            'event': threading.Event(),
            'created': time.time(),
        }
    return cmd_id


def wait_browser_result(cmd_id, timeout=COMMAND_TIMEOUT):
    """Wait for the frontend to execute a command and return the result."""
    with _lock:
        cmd = _commands.get(cmd_id)
    if not cmd:
        return {'error': 'Command not found (may have expired)'}
    ok = cmd['event'].wait(timeout=timeout)
    with _lock:
        result = cmd.get('result')
        error = cmd.get('error')
    if not ok:
        return {'error': f'Browser command timed out after {timeout}s. Is the preview tab active with a loaded page?'}
    if error:
        return {'error': error}
    return result


def set_browser_result(cmd_id, result):
    """Frontend posts command execution result."""
    with _lock:
        cmd = _commands.get(cmd_id)
        if not cmd:
            return False
        cmd['result'] = result
        cmd['status'] = 'done'
        cmd['event'].set()
    return True


def set_browser_error(cmd_id, error):
    """Frontend posts command execution error."""
    with _lock:
        cmd = _commands.get(cmd_id)
        if not cmd:
            return False
        cmd['error'] = error
        cmd['status'] = 'done'
        cmd['event'].set()
    return True


# ── API Routes ──

@bp.route('/api/browser/poll')
@handle_error
def poll_command():
    """Frontend polls for a pending browser command to execute.
    Returns the next pending command and marks it as 'claimed'."""
    with _lock:
        # Find oldest pending command
        pending = [
            (cid, cmd) for cid, cmd in _commands.items()
            if cmd['status'] == 'pending'
        ]
        if pending:
            pending.sort(key=lambda x: x[1]['created'])
            cid, cmd = pending[0]
            cmd['status'] = 'claimed'
            return jsonify({
                'cmd_id': cid,
                'action': cmd['action'],
                'params': cmd['params'],
            })
    return jsonify({'cmd_id': None})


@bp.route('/api/browser/result', methods=['POST'])
@handle_error
def post_result():
    """Frontend posts the result of executing a browser command."""
    data = request.json or {}
    cmd_id = data.get('cmd_id', '')
    result = data.get('result')
    error = data.get('error')

    if not cmd_id:
        return jsonify({'error': 'cmd_id required'}), 400

    if error:
        set_browser_error(cmd_id, error)
    else:
        set_browser_result(cmd_id, result)

    return jsonify({'ok': True})


@bp.route('/api/browser/status')
@handle_error
def browser_status():
    """Return current browser command queue status."""
    with _lock:
        pending = sum(1 for c in _commands.values() if c['status'] == 'pending')
        claimed = sum(1 for c in _commands.values() if c['status'] == 'claimed')
    return jsonify({
        'pending': pending,
        'claimed': claimed,
        'total': pending + claimed,
    })


@bp.route('/api/browser/open-external', methods=['POST'])
@handle_error
def open_external():
    """Open a URL in the system/default browser."""
    data = request.json or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'URL is required'}), 400
    if not url.startswith('http://') and not url.startswith('https://'):
        url = 'https://' + url
    try:
        # Try webbrowser first (works on desktop)
        opened = webbrowser.open(url)
        if opened:
            return jsonify({'ok': True, 'message': f'Opened in browser: {url}'})
        # Fallback: subprocess (works on Termux / Android)
        fallback_ok = False
        for cmd in [['xdg-open', url], ['termux-open-url', url]]:
            try:
                subprocess.Popen(cmd, stderr=subprocess.DEVNULL)
                fallback_ok = True
                break
            except Exception:
                continue
        if fallback_ok:
            return jsonify({'ok': True, 'message': f'Opened: {url}'})
        return jsonify({'error': f'Failed to open URL: {url} (no browser available)'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Proxy Endpoint ──

# Headers from the target that should be stripped to allow iframe embedding
_STRIP_HEADERS = {
    'x-frame-options',
    'content-security-policy',
    'content-security-policy-report-only',
    'x-content-type-options',  # strip nosniff so we can proxy any content type
}

# Headers from the target that should be passed through
# NOTE: content-encoding is NOT passed — we decompress on the server side
# so that we can rewrite URLs before sending to the browser.
# NOTE: cache-control, etag, last-modified are NOT passed — we strip them
# to prevent the browser from caching stale rewritten content.
_PASS_HEADERS = {
    'content-type',
    'transfer-encoding',
    'set-cookie',
    'access-control-allow-origin',
    'access-control-allow-credentials',
}

# SSL context that doesn't verify certs (for local dev with self-signed certs)
_SSL_CONTEXT = ssl.create_default_context()
_SSL_CONTEXT.check_hostname = False
_SSL_CONTEXT.verify_mode = ssl.CERT_NONE


class _ProxyResponse:
    """Lightweight wrapper to mimic requests.Response interface for _proxy_response."""
    def __init__(self, raw_body, status_code, headers):
        self.content = raw_body
        self.status_code = status_code
        self.headers = _CaseInsensitiveDict(headers)
        # Try to detect encoding from content-type
        ct = headers.get('Content-Type', '')
        self.encoding = 'utf-8'
        if 'charset=' in ct:
            for part in ct.split(';'):
                part = part.strip()
                if part.lower().startswith('charset='):
                    self.encoding = part.split('=', 1)[1].strip().strip('"').strip("'")
                    break


class _CaseInsensitiveDict:
    """Minimal case-insensitive dict for response headers."""
    def __init__(self, source=None):
        self._store = {}
        if source:
            for k, v in source.items():
                self._store[k.lower()] = (k, v)

    def get(self, key, default=None):
        item = self._store.get(key.lower())
        return item[1] if item else default

    def items(self):
        return [(k, v) for k, (_, v) in self._store.items()]


def _decompress_body(raw_body, content_encoding):
    """Decompress the response body if it was compressed by the server.
    Some servers ignore Accept-Encoding: identity and send compressed content anyway."""
    if not content_encoding:
        return raw_body
    enc = content_encoding.lower().strip()
    try:
        if enc == 'gzip' or enc == 'x-gzip':
            return gzip.decompress(raw_body)
        elif enc == 'deflate':
            return zlib.decompress(raw_body)
        elif enc == 'br' and _HAS_BROTLI:
            return brotli.decompress(raw_body)
    except Exception:
        pass
    return raw_body


def _proxy_response(target_resp, proxy_base):
    """Build a Flask Response from a target response, rewriting URLs if needed."""
    content_type = target_resp.headers.get('content-type', '')
    raw_body = target_resp.content

    # Decompress if server sent compressed content despite Accept-Encoding: identity
    ce = target_resp.headers.get('content-encoding', '')
    if ce:
        raw_body = _decompress_body(raw_body, ce)

    # For HTML, rewrite URLs to route through our proxy
    # NOTE: also catch content_type that is None or missing (some servers omit it)
    ct_lower = (content_type or '').lower()
    if not ct_lower:
        # No content-type — sniff by checking if body looks like HTML
        if raw_body and raw_body.lstrip()[:100].lower().startswith(b'<!') or b'<html' in raw_body[:500].lower():
            ct_lower = 'text/html'
            content_type = 'text/html; charset=utf-8'

    if 'text/html' in ct_lower:
        try:
            text = raw_body.decode(target_resp.encoding or 'utf-8', errors='replace')
            text = _rewrite_html_urls(text, proxy_base)
            # Inject a script that intercepts dynamically-created script elements
            # and rewrites their src to route through the proxy. This handles cases
            # where JS code creates <script> elements at runtime (e.g. chat.js's
            # dynamic script loader using document.createElement('script')).
            text = _inject_script_interceptor(text, proxy_base)
            raw_body = text.encode('utf-8')
            content_type = 'text/html; charset=utf-8'
        except Exception:
            pass
    # For CSS, rewrite url() references and @import
    elif 'text/css' in ct_lower:
        try:
            text = raw_body.decode(target_resp.encoding or 'utf-8', errors='replace')
            text = _rewrite_css_urls(text, proxy_base)
            raw_body = text.encode('utf-8')
        except Exception:
            pass
    # For JavaScript, rewrite import statements to route through proxy
    elif 'javascript' in ct_lower or 'application/x-javascript' in ct_lower or 'text/javascript' in ct_lower or 'module' in ct_lower:
        try:
            text = raw_body.decode(target_resp.encoding or 'utf-8', errors='replace')
            text = _rewrite_js_urls(text, proxy_base)
            raw_body = text.encode('utf-8')
        except Exception:
            pass
    # For SVG, rewrite URLs (SVG is XML-based)
    elif 'image/svg+xml' in ct_lower:
        try:
            text = raw_body.decode(target_resp.encoding or 'utf-8', errors='replace')
            text = _rewrite_html_urls(text, proxy_base)
            raw_body = text.encode('utf-8')
        except Exception:
            pass

    resp = Response(raw_body, status=target_resp.status_code)

    # Set Content-Type (always)
    if content_type:
        resp.headers['Content-Type'] = content_type

    # Pass through safe headers (NOT content-encoding since we decompressed)
    for key in _PASS_HEADERS:
        val = target_resp.headers.get(key)
        if val:
            resp.headers[key] = val

    # Prevent caching of proxied content (URLs are rewritten per-session)
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'

    # Allow embedding from any origin
    resp.headers['X-Frame-Options'] = 'ALLOWALL'
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = '*'

    return resp


def _rewrite_html_urls(html, proxy_base):
    """Rewrite URLs in HTML attributes to route through the proxy."""
    url_attrs = [
        (r'(<a\s[^>]*?href\s*=\s*["\'])([^"\']*)(["\'])', 2),
        (r'(<link\s[^>]*?href\s*=\s*["\'])([^"\']*)(["\'])', 2),
        (r'(<script\s[^>]*?src\s*=\s*["\'])([^"\']*)(["\'])', 2),
        (r'(<img\s[^>]*?src\s*=\s*["\'])([^"\']*)(["\'])', 2),
        (r'(<iframe\s[^>]*?src\s*=\s*["\'])([^"\']*)(["\'])', 2),
        (r'(<form\s[^>]*?action\s*=\s*["\'])([^"\']*)(["\'])', 2),
        (r'(<source\s[^>]*?src\s*=\s*["\'])([^"\']*)(["\'])', 2),
        (r'(<video\s[^>]*?src\s*=\s*["\'])([^"\']*)(["\'])', 2),
        (r'(<audio\s[^>]*?src\s*=\s*["\'])([^"\']*)(["\'])', 2),
        (r'(<track\s[^>]*?src\s*=\s*["\'])([^"\']*)(["\'])', 2),
        (r'(<embed\s[^>]*?src\s*=\s*["\'])([^"\']*)(["\'])', 2),
        (r'(<object\s[^>]*?data\s*=\s*["\'])([^"\']*)(["\'])', 2),
        (r'(<input\s[^>]*?src\s*=\s*["\'])([^"\']*)(["\'])', 2),
        (r'(<area\s[^>]*?href\s*=\s*["\'])([^"\']*)(["\'])', 2),
        (r'(<button\s[^>]*?formaction\s*=\s*["\'])([^"\']*)(["\'])', 2),
    ]

    def _replace(match):
        prefix = match.group(1)
        url = match.group(2)
        suffix = match.group(3)
        if url and not url.startswith(('javascript:', 'data:', '#', 'mailto:', 'tel:', 'blob:')):
            url = _proxy_url(url, proxy_base)
        return prefix + url + suffix

    for pattern, _ in url_attrs:
        html = re.sub(pattern, _replace, html, flags=re.IGNORECASE)

    # Rewrite srcset attributes (responsive images)
    def _rewrite_srcset(match):
        attr_prefix = match.group(1)
        srcset_val = match.group(2)
        quote = match.group(3)
        parts = srcset_val.split(',')
        new_parts = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            tokens = part.split(None, 1)
            url = tokens[0]
            descriptor = tokens[1] if len(tokens) > 1 else ''
            if url and not url.startswith(('data:', 'blob:')):
                url = _proxy_url(url, proxy_base)
            if descriptor:
                new_parts.append(url + ' ' + descriptor)
            else:
                new_parts.append(url)
        return attr_prefix + ', '.join(new_parts) + quote

    html = re.sub(
        r'((?:srcset|data-srcset)\s*=\s*["\'])([^"\']*)(["\'])',
        _rewrite_srcset, html, flags=re.IGNORECASE
    )

    # Rewrite inline style="...url(...)..."
    def _rewrite_inline_style(match):
        prefix = match.group(1)
        style_val = match.group(2)
        quote = match.group(3)
        style_val = _rewrite_css_urls(style_val, proxy_base)
        return prefix + style_val + quote

    html = re.sub(
        r'(<[^>]+\bstyle\s*=\s*["\'])([^"\']*)(["\'])',
        _rewrite_inline_style, html, flags=re.IGNORECASE
    )

    # Rewrite <base href> if present
    html = re.sub(
        r'(<base\s[^>]*?href\s*=\s*["\'])([^"\']*)(["\'])',
        lambda m: m.group(1) + _proxy_url(m.group(2), proxy_base) + m.group(3),
        html, flags=re.IGNORECASE
    )

    # Remove CSP meta tags that could block embedding
    html = re.sub(
        r'<meta\s+[^>]*?http-equiv\s*=\s*["\']Content-Security-Policy["\'][^>]*?>',
        '', html, flags=re.IGNORECASE
    )

    # Do NOT inject <base> tag — all URLs are already rewritten to absolute
    # proxy URLs, so <base> is unnecessary and can cause subtle issues with
    # JavaScript URL construction and relative path resolution.
    # (Previously we injected <base> but it caused more harm than good.)

    # Rewrite URLs inside inline event handlers (onclick, onload, onerror, etc.)
    # Catches patterns like: onclick="window.location.href='/ui/index.html'"
    # Must use separate patterns for double-quoted and single-quoted attributes
    # because the inner JS code may contain the other type of quote.
    def _rewrite_event_handlers_dq(match):
        """Double-quoted event handler: onclick="..." (content may have single quotes)."""
        attr_prefix = match.group(1)
        handler_body = match.group(2)
        # Rewrite navigation URLs inside the handler body
        handler_body = re.sub(
            r"((?:window\.)?location\.href\s*=\s*['\"])([^'\"]+)(['\"])",
            lambda m: m.group(1) + _proxy_url(m.group(2), proxy_base) + m.group(3),
            handler_body
        )
        handler_body = re.sub(
            r"((?:window\.)?location\.assign\s*\(\s*['\"])([^'\"]+)(['\"]\s*\))",
            lambda m: m.group(1) + _proxy_url(m.group(2), proxy_base) + m.group(3),
            handler_body
        )
        handler_body = re.sub(
            r"((?:window\.)?location\.replace\s*\(\s*['\"])([^'\"]+)(['\"]\s*\))",
            lambda m: m.group(1) + _proxy_url(m.group(2), proxy_base) + m.group(3),
            handler_body
        )
        handler_body = re.sub(
            r"(window\.open\s*\(\s*['\"])([^'\"]+)(['\"])",
            lambda m: m.group(1) + _proxy_url(m.group(2), proxy_base) + m.group(3),
            handler_body
        )
        return attr_prefix + handler_body + '"'

    def _rewrite_event_handlers_sq(match):
        """Single-quoted event handler: onclick='...' (content may have double quotes)."""
        attr_prefix = match.group(1)
        handler_body = match.group(2)
        handler_body = re.sub(
            r"((?:window\.)?location\.href\s*=\s*['\"])([^'\"]+)(['\"])",
            lambda m: m.group(1) + _proxy_url(m.group(2), proxy_base) + m.group(3),
            handler_body
        )
        handler_body = re.sub(
            r"((?:window\.)?location\.assign\s*\(\s*['\"])([^'\"]+)(['\"]\s*\))",
            lambda m: m.group(1) + _proxy_url(m.group(2), proxy_base) + m.group(3),
            handler_body
        )
        handler_body = re.sub(
            r"((?:window\.)?location\.replace\s*\(\s*['\"])([^'\"]+)(['\"]\s*\))",
            lambda m: m.group(1) + _proxy_url(m.group(2), proxy_base) + m.group(3),
            handler_body
        )
        handler_body = re.sub(
            r"(window\.open\s*\(\s*['\"])([^'\"]+)(['\"])",
            lambda m: m.group(1) + _proxy_url(m.group(2), proxy_base) + m.group(3),
            handler_body
        )
        return attr_prefix + handler_body + "'"

    # Double-quoted: onclick="..."
    html = re.sub(
        r'((?:on\w+)\s*=\s*")([^"]*)(")',
        _rewrite_event_handlers_dq, html, flags=re.IGNORECASE
    )
    # Single-quoted: onclick='...'
    html = re.sub(
        r"((?:on\w+)\s*=\s*')([^']*)(')",
        _rewrite_event_handlers_sq, html, flags=re.IGNORECASE
    )

    return html


def _inject_script_interceptor(html, proxy_base):
    """Inject a script into the HTML that intercepts dynamically-created elements
    and JavaScript-driven navigation, rewriting URLs to route through the proxy.

    Five layers of client-side interception:
    1. document.createElement('script'/'link') — rewrite src/href on set
    2. Navigation APIs — Location.prototype.href/assign/replace, window.open
       + HTMLAnchorElement.prototype.href — intercept dynamically created <a> href
    3. Document-level click listener — safety net for unrewritten <a> clicks
       (covers innerHTML-inserted anchors, and any <a> the server missed)
    4. fetch() interceptor — route relative fetch URLs through the proxy
       (critical for SPA apps that use fetch('/api/...') for API calls)
    4b. XMLHttpRequest.open interceptor — same as fetch but for legacy XHR

    Also fixes _toProxyUrl to use new URL(src, base) instead of string
    concatenation, which caused double-slash errors for root-relative paths
    like /ui/index.html when appended to a directory base URL.
    """
    # Extract the original directory URL from proxy_base
    original_dir = _extract_base_from_proxy_base(proxy_base)
    if not original_dir:
        return html

    # Build the interceptor script — use string concatenation to avoid
    # f-string brace-escaping issues with deeply nested JavaScript.
    _orig_dir_js = repr(original_dir)
    _proxy_base_js = repr(proxy_base)
    interceptor = (
        "<script>\n"
        "(function() {\n"
        "    var _origCreate = document.createElement.bind(document);\n"
        "    var _ORIG_DIR = " + _orig_dir_js + ";\n"
        "    var _PROXY_BASE = " + _proxy_base_js + ";\n"
        "\n"
        "    var _scriptSrcDesc = Object.getOwnPropertyDescriptor(HTMLScriptElement.prototype, 'src');\n"
        "    var _linkHrefDesc = Object.getOwnPropertyDescriptor(HTMLLinkElement.prototype, 'href');\n"
        "\n"
        "    // Fixed _toProxyUrl: use new URL(src, base) for correct resolution.\n"
        "    // Previously _ORIG_DIR + src caused double-slash for root-relative paths\n"
        "    // like /ui/index.html -> http://host/dir//ui/index.html\n"
        "    function _toProxyUrl(src) {\n"
        "        if (!src || typeof src !== 'string') return src;\n"
        "        if (src.startsWith('data:') || src.startsWith('blob:') || src.startsWith('javascript:') ||\n"
        "            src.startsWith('#') || src.startsWith('mailto:') || src.startsWith('tel:')) return src;\n"
        "        if (src.indexOf('/api/browser/proxy') !== -1) return src;  // already proxied\n"
        "        var absUrl;\n"
        "        try {\n"
        "            absUrl = new URL(src, _ORIG_DIR).href;\n"
        "        } catch(e) {\n"
        "            absUrl = _ORIG_DIR + src;\n"
        "        }\n"
        "        return _PROXY_BASE + '&rel=' + encodeURIComponent(absUrl);\n"
        "    }\n"
        "\n"
        "    // --- Layer 1: Intercept createElement('script'/'link') to rewrite src/href ---\n"
        "    document.createElement = function(tag) {\n"
        "        var el = _origCreate(tag);\n"
        "        if (tag.toLowerCase() === 'script' && _scriptSrcDesc) {\n"
        "            Object.defineProperty(el, 'src', {\n"
        "                get: function() { return _scriptSrcDesc.get.call(this); },\n"
        "                set: function(val) { _scriptSrcDesc.set.call(this, _toProxyUrl(val)); },\n"
        "                configurable: true\n"
        "            });\n"
        "        }\n"
        "        if (tag.toLowerCase() === 'link' && _linkHrefDesc) {\n"
        "            Object.defineProperty(el, 'href', {\n"
        "                get: function() { return _linkHrefDesc.get.call(this); },\n"
        "                set: function(val) { _linkHrefDesc.set.call(this, _toProxyUrl(val)); },\n"
        "                configurable: true\n"
        "            });\n"
        "        }\n"
        "        return el;\n"
        "    };\n"
        "\n"
        "    // --- Layer 2a: Intercept navigation APIs ---\n"
        "    // Location.prototype.href setter — catches window.location.href = '/path'\n"
        "    var _origHrefDesc = Object.getOwnPropertyDescriptor(Location.prototype, 'href');\n"
        "    if (_origHrefDesc) {\n"
        "        Object.defineProperty(Location.prototype, 'href', {\n"
        "            get: function() { return _origHrefDesc.get.call(this); },\n"
        "            set: function(val) { _origHrefDesc.set.call(this, _toProxyUrl(val)); },\n"
        "            configurable: true\n"
        "        });\n"
        "    }\n"
        "\n"
        "    // Location.prototype.assign — catches window.location.assign('/path')\n"
        "    var _origAssign = Location.prototype.assign;\n"
        "    Location.prototype.assign = function(url) {\n"
        "        return _origAssign.call(this, _toProxyUrl(url));\n"
        "    };\n"
        "\n"
        "    // Location.prototype.replace — catches window.location.replace('/path')\n"
        "    var _origReplace = Location.prototype.replace;\n"
        "    Location.prototype.replace = function(url) {\n"
        "        return _origReplace.call(this, _toProxyUrl(url));\n"
        "    };\n"
        "\n"
        "    // window.open — catches window.open('/path')\n"
        "    var _origOpen = window.open;\n"
        "    window.open = function(url) {\n"
        "        if (url && typeof url === 'string') {\n"
        "            arguments[0] = _toProxyUrl(url);\n"
        "        }\n"
        "        return _origOpen.apply(this, arguments);\n"
        "    };\n"
        "\n"
        "    // --- Layer 2b: HTMLAnchorElement.prototype.href setter ---\n"
        "    // Catches dynamically created <a> elements where JS sets a.href = '/path'\n"
        "    var _anchorHrefDesc = Object.getOwnPropertyDescriptor(HTMLAnchorElement.prototype, 'href');\n"
        "    if (_anchorHrefDesc) {\n"
        "        Object.defineProperty(HTMLAnchorElement.prototype, 'href', {\n"
        "            get: function() { return _anchorHrefDesc.get.call(this); },\n"
        "            set: function(val) { _anchorHrefDesc.set.call(this, _toProxyUrl(val)); },\n"
        "            configurable: true\n"
        "        });\n"
        "    }\n"
        "\n"
        "    // --- Layer 3: Document-level click listener ---\n"
        "    // Safety net for <a> clicks that were NOT rewritten by the server or\n"
        "    // intercepted by the setter above (e.g. innerHTML-inserted anchors).\n"
        "    // Uses getAttribute('href') to get the raw attribute value, NOT the\n"
        "    // resolved el.href (which would be wrong in the proxy context).\n"
        "    document.addEventListener('click', function(e) {\n"
        "        var el = e.target;\n"
        "        while (el && el.tagName !== 'A') el = el.parentElement;\n"
        "        if (el && el.tagName === 'A') {\n"
        "            var attrHref = el.getAttribute('href');\n"
        "            if (attrHref && !attrHref.startsWith('data:') && !attrHref.startsWith('blob:') &&\n"
        "                !attrHref.startsWith('javascript:') && !attrHref.startsWith('#') &&\n"
        "                !attrHref.startsWith('mailto:') && !attrHref.startsWith('tel:') &&\n"
        "                attrHref.indexOf('/api/browser/proxy') === -1) {\n"
        "                e.preventDefault();\n"
        "                e.stopPropagation();\n"
        "                // Use raw attribute value, not resolved URL\n"
        "                window.location.href = _toProxyUrl(attrHref);\n"
        "            }\n"
        "        }\n"
        "    }, true);  // capture phase to intercept before any other handler\n"
        "\n"
        "    // --- Layer 4: fetch() interceptor ---\n"
        "    // Intercept all fetch() calls to route relative URLs through the proxy.\n"
        "    // This is critical for SPA applications that use fetch() for API calls\n"
        "    // (e.g. fetch('/api/organization')) -- without this, relative URLs resolve\n"
        "    // to the proxy server (IDE) instead of the original target server.\n"
        "    var _origFetch = window.fetch;\n"
        "    window.fetch = function(input, init) {\n"
        "        var url;\n"
        "        if (typeof input === 'string') {\n"
        "            url = input;\n"
        "        } else if (input && typeof input === 'object' && typeof input.url === 'string') {\n"
        "            url = input.url;\n"
        "        } else {\n"
        "            return _origFetch.apply(this, arguments);\n"
        "        }\n"
        "        // Skip already-proxied URLs, data URLs, blob URLs, javascript URLs\n"
        "        if (!url || url.indexOf('/api/browser/proxy') !== -1 ||\n"
        "            url.startsWith('data:') || url.startsWith('blob:') ||\n"
        "            url.startsWith('javascript:')) {\n"
        "            return _origFetch.apply(this, arguments);\n"
        "        }\n"
        "        // Rewrite relative URLs (starting with / or not starting with a protocol)\n"
        "        var isRelative = url.startsWith('/') ||\n"
        "            (!url.startsWith('http://') && !url.startsWith('https://'));\n"
        "        if (isRelative) {\n"
        "            var proxyUrl = _toProxyUrl(url);\n"
        "            if (typeof input === 'string') {\n"
        "                return _origFetch.call(this, proxyUrl, init);\n"
        "            } else {\n"
        "                // Request object -- create a new one with the proxied URL\n"
        "                return _origFetch.call(this, new Request(proxyUrl, input));\n"
        "            }\n"
        "        }\n"
        "        return _origFetch.apply(this, arguments);\n"
        "    };\n"
        "\n"
        "    // --- Layer 4b: XMLHttpRequest interceptor ---\n"
        "    // Same as fetch() but for legacy XMLHttpRequest.open() calls.\n"
        "    var _origXHROpen = XMLHttpRequest.prototype.open;\n"
        "    XMLHttpRequest.prototype.open = function(method, url) {\n"
        "        if (url && typeof url === 'string' &&\n"
        "            url.indexOf('/api/browser/proxy') === -1 &&\n"
        "            !url.startsWith('data:') && !url.startsWith('blob:')) {\n"
        "            var isRelative = url.startsWith('/') ||\n"
        "                (!url.startsWith('http://') && !url.startsWith('https://'));\n"
        "            if (isRelative) {\n"
        "                arguments[1] = _toProxyUrl(url);\n"
        "            }\n"
        "        }\n"
        "        return _origXHROpen.apply(this, arguments);\n"
        "    };\n"
        "})();\n"
        "</script>"
    )

    # Inject right after <head> tag, or before </head> if no <head> tag
    if '<head>' in html.lower():
        # Case-insensitive insert after <head>
        idx = html.lower().index('<head>')
        # Find the end of the actual <head> tag (may have attributes)
        end = html.index('>', idx) + 1
        html = html[:end] + '\n' + interceptor + html[end:]
    elif '</head>' in html.lower():
        html = html.replace('</head>', interceptor + '\n</head>')
    else:
        # No head tag at all — prepend to body
        html = interceptor + '\n' + html

    return html


def _rewrite_css_urls(css, proxy_base):
    """Rewrite url() references and @import in CSS to route through the proxy."""
    def _replace_url(match):
        url = match.group(1)
        if url and not url.startswith(('data:', 'blob:')):
            url = _proxy_url(url, proxy_base)
        return 'url(' + url + ')'

    css = re.sub(r'url\(\s*["\']?([^"\'\)\s]+)["\']?\s*\)', _replace_url, css, flags=re.IGNORECASE)

    # Rewrite @import "..." and @import '...' (without url())
    def _replace_import(match):
        quote = match.group(1)
        url = match.group(2)
        if url and not url.startswith(('data:', 'blob:')):
            url = _proxy_url(url, proxy_base)
        return '@import ' + quote + url + quote

    css = re.sub(r'@import\s+(["\'])([^"\']+)(["\'])', _replace_import, css, flags=re.IGNORECASE)

    return css


def _extract_base_from_proxy_base(proxy_base):
    """Extract the original target directory URL from a proxy_base string.

    proxy_base format: http://localhost:12346/api/browser/proxy?url=<target>&base=<dir>
    Returns the <dir> part (the original directory URL on the target server).
    """
    parsed = urllib.parse.urlparse(proxy_base)
    params = urllib.parse.parse_qs(parsed.query)
    base_urls = params.get('base', [])
    if base_urls:
        return base_urls[0]
    return ''


def _rewrite_js_urls(js, proxy_base):
    """Rewrite static import URLs in JavaScript to route through the proxy.
    This is best-effort — dynamic URLs constructed at runtime cannot be fully handled server-side."""
    # Rewrite import() dynamic imports
    def _replace_import(match):
        quote = match.group(1)
        url = match.group(2)
        if url and not url.startswith(('data:', 'blob:')):
            url = _proxy_url(url, proxy_base)
        return 'import(' + quote + url + quote + ')'

    js = re.sub(r'import\(\s*(["\'])([^"\']+)(["\'])\s*\)', _replace_import, js)

    # Rewrite static import ... from '...' and from "..."
    def _replace_static_import(match):
        prefix = match.group(1)
        quote = match.group(2)
        url = match.group(3)
        if url and not url.startswith(('.', '/', 'data:', 'blob:', 'http')):
            # Bare module specifier — don't rewrite
            return match.group(0)
        if url and not url.startswith(('data:', 'blob:')):
            url = _proxy_url(url, proxy_base)
        return prefix + quote + url + quote

    js = re.sub(r'(import\s+[^;\n]+?\s+from\s+)(["\'])([^"\']+)(["\'])', _replace_static_import, js)
    # Rewrite simple import "..." statements
    js = re.sub(r'(import\s*)(["\'])([^"\']+)(["\'])', _replace_static_import, js)

    # ── Rewrite JS base URL computation patterns ──
    # Many SPAs and frameworks use patterns like:
    #   new URL('.', window.location.href).href
    #   new URL('./', window.location.href).href
    # to compute the current directory. When served through our proxy,
    # window.location.href points to the proxy URL, not the original URL.
    # Replace these with the actual original directory URL.
    original_dir = _extract_base_from_proxy_base(proxy_base)
    if original_dir:
        def _replace_new_url_base(match):
            return repr(original_dir)

        # Match: new URL(".", window.location.href).href  (with optional quotes and /)
        js = re.sub(
            r'new\s+URL\(\s*["\']\.\/?["\']\s*,\s*window\.location\.href\s*\)\.href',
            _replace_new_url_base, js
        )
        # Also match: new URL(relPath, window.location.href) where relPath is a simple path
        js = re.sub(
            r'new\s+URL\(\s*["\']([^"\']*)["\']\s*,\s*window\.location\.href\s*\)',
            lambda m: f'new URL({repr(m.group(1))}, {repr(original_dir)})',
            js
        )

    # ── Rewrite navigation URLs in JS ──
    # Catches static string arguments to window.location.href/assign/replace
    # and window.open, routing them through the proxy instead of navigating
    # directly on the IDE server's origin (which would 404).

    # window.location.href = '/path' or location.href = '/path'
    js = re.sub(
        r"((?:window\.)?location\.href\s*=\s*['\"])([^'\"]+)(['\"])",
        lambda m: m.group(1) + _proxy_url(m.group(2), proxy_base) + m.group(3),
        js
    )
    # window.location.assign('/path') or location.assign('/path')
    js = re.sub(
        r"((?:window\.)?location\.assign\s*\(\s*['\"])([^'\"]+)(['\"]\s*\))",
        lambda m: m.group(1) + _proxy_url(m.group(2), proxy_base) + m.group(3),
        js
    )
    # window.location.replace('/path') or location.replace('/path')
    js = re.sub(
        r"((?:window\.)?location\.replace\s*\(\s*['\"])([^'\"]+)(['\"]\s*\))",
        lambda m: m.group(1) + _proxy_url(m.group(2), proxy_base) + m.group(3),
        js
    )
    # window.open('/path')
    js = re.sub(
        r"(window\.open\s*\(\s*['\"])([^'\"]+)(['\"])",
        lambda m: m.group(1) + _proxy_url(m.group(2), proxy_base) + m.group(3),
        js
    )

    # ── Rewrite fetch() URLs in JS ──
    # Catches static string arguments to fetch('/path')
    # This is best-effort; the client-side fetch interceptor handles dynamic URLs.
    def _replace_fetch_url(match):
        prefix = match.group(1)
        url = match.group(2)
        suffix = match.group(3)
        # Only rewrite if it looks like a URL/path
        if url and (url.startswith('/') or url.startswith('./') or url.startswith('../') or
                    url.startswith('http://') or url.startswith('https://')):
            url = _proxy_url(url, proxy_base)
        return prefix + url + suffix

    js = re.sub(
        r"(fetch\s*\(\s*['\"])([^'\"]+)(['\"])",
        _replace_fetch_url, js
    )

    return js


def _proxy_url(url, proxy_base):
    """Convert an absolute or relative URL to an absolute proxy URL.

    proxy_base has the form:
      http://localhost:12346/api/browser/proxy?url=<encoded_target>&base=<encoded_dir>
    Always returns absolute URLs to avoid interference from <base> tag injection.
    """
    if not url:
        return url
    # Already proxied (and hopefully absolute)
    if '/api/browser/proxy' in url:
        return url

    # Extract origin from proxy_base for constructing absolute proxy URLs
    origin = ''
    if proxy_base.startswith('http://') or proxy_base.startswith('https://'):
        idx = proxy_base.find('/', 8)  # after http(s)://
        if idx > 0:
            origin = proxy_base[:idx]

    # Absolute URL — construct independent absolute proxy URL
    if url.startswith('http://') or url.startswith('https://'):
        proxy = '/api/browser/proxy?url=' + urllib.parse.quote(url, safe='') + '&base=' + urllib.parse.quote(url, safe='')
        return (origin + proxy) if origin else proxy

    # Protocol-relative
    if url.startswith('//'):
        full = 'https:' + url
        proxy = '/api/browser/proxy?url=' + urllib.parse.quote(full, safe='') + '&base=' + urllib.parse.quote(full, safe='')
        return (origin + proxy) if origin else proxy

    # Relative URL — proxy_base already absolute, just append rel param
    return proxy_base + '&rel=' + urllib.parse.quote(url, safe='')


@bp.route('/api/browser/proxy', methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS', 'PATCH'])
def proxy():
    """
    Reverse proxy for iframe preview.
    Fetches the target URL, strips X-Frame-Options / CSP headers,
    rewrites HTML/CSS/JS URLs to route through this proxy.
    Supports all HTTP methods (GET, POST, PUT, DELETE, OPTIONS, PATCH)
    with request body forwarding and SSE streaming response passthrough.

    Query params:
      url  — the target URL to fetch
      rel  — (optional) relative URL path to append to the target URL
      base — (optional) override the base URL for resolving relative paths
    """
    # ── Handle CORS preflight ──
    if request.method == 'OPTIONS':
        resp = Response('', status=204)
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS, PATCH'
        resp.headers['Access-Control-Allow-Headers'] = '*'
        resp.headers['Access-Control-Max-Age'] = '86400'
        return resp

    target_url = request.args.get('url', '').strip()
    if not target_url:
        return jsonify({'error': 'url parameter required'}), 400

    # Normalize protocol
    if target_url.startswith('//'):
        target_url = 'https:' + target_url

    # Handle relative URLs (rel param is set when HTML references are rewritten)
    rel_path = request.args.get('rel', '')
    base_url = request.args.get('base', '')

    if rel_path:
        # Resolve relative URL against the base
        resolve_against = base_url or target_url
        target_url = urljoin(resolve_against, rel_path)

    # Security: only allow http/https
    if not target_url.startswith('http://') and not target_url.startswith('https://'):
        return jsonify({'error': 'Only http/https URLs are allowed'}), 400

    parsed = urlparse(target_url)
    hostname = parsed.hostname or ''
    # Compute self_origin early so it's available in all exception handlers
    self_origin = request.host_url.rstrip('/')

    # Determine if we need to stream the response (for SSE)
    # We detect SSE by checking the request's Accept header or the response content-type later

    try:
        # Build the outgoing request
        req_method = request.method
        req_body = request.get_data()  # gets raw body bytes for POST/PUT

        # Use http.client for full control over method, body, and streaming
        if parsed.scheme == 'https':
            conn = http.client.HTTPSConnection(
                hostname,
                parsed.port or 443,
                timeout=120,
                context=_SSL_CONTEXT,
            )
        else:
            conn = http.client.HTTPConnection(
                hostname,
                parsed.port or 80,
                timeout=120,
            )

        # Build path with query string (only the target's own query params, not our proxy params)
        target_path = parsed.path or '/'
        if parsed.query:
            target_path += '?' + parsed.query

        # http.client requires ASCII-only path. Percent-encode any non-ASCII characters
        # (e.g. Chinese characters in myagent conversation names).
        # Only encode non-ASCII; preserve already-valid URL structure (/, ?, =, &, %).
        target_path = re.sub(
            r'[^\x00-\x7F]+',
            lambda m: urllib.parse.quote(m.group(), safe=''),
            target_path,
        )

        # Forward request headers (selectively)
        forward_headers = {}
        # Content-Type and Content-Length for POST/PUT with body
        if req_body:
            ct = request.headers.get('Content-Type', '')
            if ct:
                forward_headers['Content-Type'] = ct
            forward_headers['Content-Length'] = str(len(req_body))
        # Authorization header (for API calls that need auth)
        auth = request.headers.get('Authorization', '')
        if auth:
            forward_headers['Authorization'] = auth
        # Accept header
        accept = request.headers.get('Accept', '*/*')
        if accept:
            forward_headers['Accept'] = accept
        # Forward User-Agent
        forward_headers['User-Agent'] = request.headers.get('User-Agent', 'Mozilla/5.0 MusIDE Proxy')
        # Forward Host
        forward_headers['Host'] = hostname
        # Forward Referer
        referer = f'{parsed.scheme}://{parsed.netloc}/'
        forward_headers['Referer'] = referer
        # Accept-Encoding: identity to avoid gzip (for non-streaming, we need to rewrite)
        forward_headers['Accept-Encoding'] = 'identity'
        # Forward Origin if present (some APIs need it)
        origin = request.headers.get('Origin', '')
        if origin:
            forward_headers['Origin'] = origin
        # Forward X-Requested-With (some backends check for this)
        xrw = request.headers.get('X-Requested-With', '')
        if xrw:
            forward_headers['X-Requested-With'] = xrw

        conn.request(req_method, target_path, body=req_body if req_body else None, headers=forward_headers)
        raw_resp = conn.getresponse()

        status_code = raw_resp.status
        resp_headers = dict(raw_resp.getheaders())

        # ── Follow redirects server-side (like urllib did before 8dbd8ed) ──
        # MUST follow redirects on the server side, NOT return 3xx to the browser.
        # If we return the redirect to the browser, the Location header points to
        # the original target URL (different origin), causing iframe cross-origin errors.
        MAX_REDIRECTS = 10
        _redirect_count = 0
        while 300 <= status_code < 400 and 'Location' in resp_headers and _redirect_count < MAX_REDIRECTS:
            conn.close()
            location = resp_headers['Location']
            # Resolve relative redirect
            if not location.startswith('http'):
                location = urljoin(target_url, location)
            print(f'[PROXY] {status_code} redirect #{_redirect_count+1}: {target_url} → {location}')
            target_url = location
            parsed = urlparse(target_url)
            hostname = parsed.hostname or ''

            # Open new connection to the redirect target
            if parsed.scheme == 'https':
                conn = http.client.HTTPSConnection(
                    hostname,
                    parsed.port or 443,
                    timeout=120,
                    context=_SSL_CONTEXT,
                )
            else:
                conn = http.client.HTTPConnection(
                    hostname,
                    parsed.port or 80,
                    timeout=120,
                )

            target_path = parsed.path or '/'
            if parsed.query:
                target_path += '?' + parsed.query
            # Percent-encode non-ASCII characters
            target_path = re.sub(
                r'[^\x00-\x7F]+',
                lambda m: urllib.parse.quote(m.group(), safe=''),
                target_path,
            )
            # Update forwarded headers for new host
            forward_headers['Host'] = hostname
            forward_headers['Referer'] = f'{parsed.scheme}://{parsed.netloc}/'

            # For 307/308, preserve method and body; for 301/302/303, switch to GET
            if status_code in (307, 308):
                conn.request(req_method, target_path, body=req_body if req_body else None, headers=forward_headers)
            else:
                # 301, 302, 303 — switch to GET, drop body
                conn.request('GET', target_path, body=None, headers=forward_headers)

            raw_resp = conn.getresponse()
            status_code = raw_resp.status
            resp_headers = dict(raw_resp.getheaders())
            _redirect_count += 1

        content_type = resp_headers.get('Content-Type', '') or resp_headers.get('content-type', '')

        if _redirect_count > 0:
            print(f'[PROXY] {req_method} {status_code} {target_url} [{content_type}] (after {_redirect_count} redirects)')
        else:
            print(f'[PROXY] {req_method} {status_code} {target_url} [{content_type}]')

        # ── For SSE / streaming responses, stream through directly ──
        ct_lower = (content_type or '').lower()
        is_streaming = (
            'text/event-stream' in ct_lower or
            'application/x-ndjson' in ct_lower or
            'application/octet-stream' in ct_lower
        )

        if is_streaming:
            # Stream the response through without buffering
            def _generate():
                try:
                    while True:
                        chunk = raw_resp.read(8192)
                        if not chunk:
                            break
                        yield chunk
                finally:
                    conn.close()

            resp = Response(_generate(), status=status_code)
            # Set content type
            if content_type:
                resp.headers['Content-Type'] = content_type
            # Pass through CORS headers
            resp.headers['Access-Control-Allow-Origin'] = '*'
            resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS, PATCH'
            resp.headers['Access-Control-Allow-Headers'] = '*'
            # Prevent buffering by proxies
            resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            resp.headers['X-Accel-Buffering'] = 'no'
            return resp

        # ── For non-streaming responses, buffer and optionally rewrite ──
        raw_body = raw_resp.read()
        conn.close()

        body_size = len(raw_body)
        print(f'[PROXY] {status_code} {target_url} [{content_type}] {body_size}B')

        # Build proxy base URL for rewriting
        # Use the directory of the target URL as base, so relative URLs resolve correctly
        path = parsed.path or '/'
        if '/' in path.rstrip('/'):
            dir_path = path.rsplit('/', 1)[0] + '/'
        else:
            dir_path = '/'
        dir_url = f'{parsed.scheme}://{parsed.netloc}{dir_path}'

        proxy_base = f'{self_origin}/api/browser/proxy?url={urllib.parse.quote(target_url, safe="")}'
        proxy_base += f'&base={urllib.parse.quote(dir_url, safe="")}'

        wrapped = _ProxyResponse(raw_body, status_code, resp_headers)
        return _proxy_response(wrapped, proxy_base)

    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        reason = str(e.reason) if hasattr(e, 'reason') and e.reason else str(e)
        print(f'[PROXY] Error for {target_url}: {reason}')
        return jsonify({'error': f'Proxy error: {reason[:200]}'}), 502
    except Exception as e:
        import traceback as _tb
        _tb.print_exc()
        return jsonify({'error': f'Proxy error: {str(e)[:200]}'}), 502
