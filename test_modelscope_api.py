#!/usr/bin/env python3
"""Test script to reproduce ModelScope API "Unterminated string" error.
Builds the same payload as chat.py would and sends it to the API.
"""
import json
import urllib.request
import urllib.error
import os
import re

API_URL = "https://api-inference.modelscope.cn/v1/chat/completions"
API_KEY = "ms-3eca52df-ea14-481b-9e72-73b988b612f7"
MODEL = "stepfun-ai/Step-3.5-Flash"

# Replicate the system prompt construction from chat.py
_IDE_PORT = os.environ.get('MUSIDE_PORT', '12345')

DEFAULT_SYSTEM_PROMPT = f"""You are MusIDE AI Agent, a powerful coding assistant integrated in a mobile IDE.
You have access to specialized tools for reading, writing, editing, searching, and managing code projects.

## Choose the Most Efficient Tool for Each Task
Each tool is designed for a specific purpose. Using the right tool gives you **better accuracy, structured output, and faster results** than run_command. Here's when to use each:

**Planning:** todo_write (plan tasks), todo_read (check progress)
**Read:** read_file (line numbers, encoding, offset/limit)
**Edit 1 spot:** edit_file with old_text/new_text (atomic, auto-lint)
**Edit 2+ spots:** edit_file with replacements array (all succeed or all fail)
**Rewrite whole file:** write_file
**Append to file:** append_file
**List dir:** list_directory (structured with sizes)
**Find files:** glob_files (pattern: "**/*.py")
**Search content:** search_files (regex, line numbers)
**File metadata:** file_info (size, dates, permissions)
**Create dirs:** create_directory (mkdir -p)
**Delete:** delete_path (recursive option)
**Move/rename:** move_file (auto-updates AST index)
**Search code:** grep_code (context lines, file info)
**Find definition:** find_definition (AST-based, precise)
**Find usages:** find_references (AST-based, excludes comments)
**File structure:** file_structure (AST outline)
**Lint:** run_linter (auto-detects project+linter)
**Test:** run_tests (auto-detects framework)
**Install pkg:** install_package (auto-handles venv)
**List pkgs:** list_packages (shows versions)
**Git:** git_status/git_diff/git_log/git_commit/git_checkout (structured output)
**Web search:** web_search (structured results)
**Fetch page:** web_fetch (clean text, no HTML)
**Preview:** browser_navigate (open URL in iframe)
**JS in page:** browser_evaluate (DOM access)
**Inspect element:** browser_inspect (tag, attrs, styles, position)
**Find elements:** browser_query_all (CSS selector, up to 50)
**Click:** browser_click (simulate click)
**Type input:** browser_input (React/Vue compatible)
**Console:** browser_console (captured logs)
**Page info:** browser_page_info (title, URL, viewport)
**Cookies:** browser_cookies (parsed pairs)
**Server logs:** server_logs (backend errors)
**Subtask:** delegate_task (independent sub-agent)
**Parallel:** parallel_tasks (2-4 sub-agents)
**Kill port:** kill_port (stop process by port)
**Shell:** run_command (dev servers, compiling, scripts — right choice for these)

Key principle: run_command is NOT wrong — just less efficient for tasks that have a dedicated tool.

## Core Workflow Rules
1. ALWAYS use todo_write BEFORE starting any complex task (3+ steps) - plan first, then execute
2. Update todo status in real-time - mark items in_progress when starting, completed when done
3. Choose the most efficient tool - check the list above before falling back to run_command
4. ALWAYS test your changes - use run_linter and run_tests after modifications
5. Before writing a file, read it first to understand existing content
6. When modifying code, use edit_file for targeted changes instead of rewriting entire files
7. PREFER find_definition/find_references over grep_code for code navigation - AST analysis is more precise
8. Always use absolute paths when referencing files
9. After executing commands, check the output for errors before proceeding
10. For large files, use offset_line and limit_lines to read specific sections

## Task Planning Workflow (MANDATORY)
You MUST use todo_write before starting ANY task with 3+ steps.
- Break complex tasks into specific, actionable steps
- Use id like "1", "2", "3" for ordering
- Set priority: high/medium/low
- Update status in real-time: in_progress -> completed

## Testing & Debugging Workflow (CRITICAL)
After every code modification:
1. Use edit_file or write_file to make changes
2. Use run_linter to check for issues
3. Use run_tests to verify changes
4. Use server_logs after backend changes
5. Use browser_navigate + browser_console after frontend changes

## CRITICAL SAFETY RULES - NEVER VIOLATE
The process muside_server.py and port {_IDE_PORT} are the core of this IDE and AI assistant. WITHOUT them, the entire system stops working.
- NEVER stop, kill, or terminate the muside_server.py process
- NEVER use kill_port on port {_IDE_PORT} - this is the IDE's own port
- NEVER run any command that would stop muside_server.py
- If you need to start a user's project server and port {_IDE_PORT} is mentioned, use a DIFFERENT port

## Platform Awareness
- On Windows: paths use backslashes, Python is python, venv binaries in Scripts/
- On Linux/macOS: paths use forward slashes, Python is python3, venv binaries in bin/
"""

# Now extract the actual AGENT_TOOLS from chat.py
# We'll exec just the tools definition portion
_chat_py_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'routes', 'chat.py')
with open(_chat_py_path, 'r') as f:
    chat_content = f.read()

# Find the AGENT_TOOLS = [ ... ] block
# It starts with "AGENT_TOOLS = [" and ends with the matching "]"
_tools_match = re.search(r'AGENT_TOOLS\s*=\s*\[', chat_content)
if _tools_match:
    # Find the end by counting brackets
    start_pos = _tools_match.start()
    bracket_count = 0
    end_pos = start_pos
    in_string = False
    string_char = None
    i = _tools_match.end() - 1  # start from the opening [
    for i in range(_tools_match.end() - 1, len(chat_content)):
        c = chat_content[i]
        if in_string:
            if c == '\\':
                continue  # skip next char (escaped)
            if c == string_char:
                in_string = False
        else:
            if c in ('"', "'"):
                in_string = True
                string_char = c
            elif c == '[':
                bracket_count += 1
            elif c == ']':
                bracket_count -= 1
                if bracket_count == 0:
                    end_pos = i + 1
                    break

    tools_code = chat_content[start_pos:end_pos]
    # Execute to get the actual list
    exec_globals = {}
    exec(tools_code, exec_globals)
    AGENT_TOOLS = exec_globals['AGENT_TOOLS']
    print(f"Extracted {len(AGENT_TOOLS)} tools from chat.py")
else:
    print("ERROR: Could not find AGENT_TOOLS in chat.py")
    exit(1)


def test_api():
    """Send a minimal request with the full system prompt and tools to ModelScope."""

    messages = [
        {"role": "system", "content": DEFAULT_SYSTEM_PROMPT},
        {"role": "user", "content": "Hello, what tools do you have?"}
    ]

    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 4096,
        "tools": AGENT_TOOLS,
        "tool_choice": "auto",
        "stream": False,
    }

    # Encode exactly the same way as chat.py
    body_bytes = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')

    print(f"\n=== Payload Info ===")
    print(f"Size: {len(body_bytes)} bytes ({len(body_bytes)/1024:.1f} KB)")
    print(f"System prompt length: {len(DEFAULT_SYSTEM_PROMPT)} chars")
    print(f"Number of tools: {len(AGENT_TOOLS)}")
    tools_json_len = len(json.dumps(AGENT_TOOLS, ensure_ascii=False, separators=(',',':')))
    print(f"Tools JSON length: {tools_json_len} chars")

    # Verify the payload is valid JSON
    try:
        parsed_back = json.loads(body_bytes)
        print("✓ Payload is valid JSON")
    except json.JSONDecodeError as e:
        print(f"✗ Payload is INVALID JSON: {e}")
        print(f"  Error at position: {e.pos}")
        if e.pos < len(body_bytes):
            start = max(0, e.pos - 80)
            end = min(len(body_bytes), e.pos + 80)
            snippet = body_bytes[start:end].decode('utf-8', errors='replace')
            print(f"  Around error position ({start}-{end}):")
            print(f"  ...{snippet}...")
        return

    # Scan for problematic bytes
    print("\n=== Scanning for problematic bytes ===")
    found_issues = []
    for i, b in enumerate(body_bytes):
        if b < 0x20 and b not in (0x09, 0x0A, 0x0D):
            found_issues.append((i, b))
            if len(found_issues) >= 10:
                break
    if found_issues:
        for pos, byte_val in found_issues:
            start = max(0, pos - 40)
            end = min(len(body_bytes), pos + 40)
            context = body_bytes[start:end].decode('utf-8', errors='replace')
            print(f"  ⚠ Control byte 0x{byte_val:02x} at position {pos}")
            print(f"    Context: ...{context}...")
    else:
        print("✓ No problematic control bytes found")

    # Send the actual request
    print(f"\n=== Sending request to {API_URL} ===")
    print(f"Model: {MODEL}")
    headers = {
        'Content-Type': 'application/json; charset=utf-8',
        'Authorization': f'Bearer {API_KEY}',
    }

    req = urllib.request.Request(API_URL, body_bytes, headers=headers, method='POST')

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp_body = resp.read().decode()
            print(f"✓ Success! HTTP {resp.status}")
            try:
                result = json.loads(resp_body)
                content = result.get('choices', [{}])[0].get('message', {}).get('content', '')
                print(f"Response content (first 500 chars): {content[:500]}")
            except:
                print(f"Raw response (first 500 chars): {resp_body[:500]}")

    except urllib.error.HTTPError as e:
        body = e.read().decode() if hasattr(e, 'read') else ''
        print(f"✗ HTTP Error {e.code}")
        print(f"\n=== Full Error Response ===")
        print(body)
        print(f"\n=== End Error Response ===")

        # Parse the char position
        char_match = re.search(r'char (\d+)', body)
        line_match = re.search(r'line (\d+)', body)
        col_match = re.search(r'column (\d+)', body)

        if char_match:
            char_pos = int(char_match.group(1))
            print(f"\n=== Error Position Analysis ===")
            print(f"Char position: {char_pos}")
            if line_match:
                print(f"Line: {line_match.group(1)}")
            if col_match:
                print(f"Column: {col_match.group(1)}")

            if char_pos < len(body_bytes):
                # Show bytes around the error
                start = max(0, char_pos - 200)
                end = min(len(body_bytes), char_pos + 200)
                snippet = body_bytes[start:end].decode('utf-8', errors='replace')
                print(f"\nPayload bytes around char {char_pos} (showing {start}-{end}):")
                print(f"...{snippet}...")

                # Show the exact byte
                print(f"\nExact byte at position {char_pos}: 0x{body_bytes[char_pos]:02x} = {chr(body_bytes[char_pos])!r}")

                # Try to identify which JSON field
                _identify_field(body_bytes, char_pos, payload)

            # Also show as decoded UTF-8 string around that position
            decoded = body_bytes.decode('utf-8', errors='replace')
            if char_pos < len(decoded):
                start = max(0, char_pos - 200)
                end = min(len(decoded), char_pos + 200)
                print(f"\nDecoded string around char {char_pos}:")
                print(f"...{decoded[start:end]}...")

    except Exception as e:
        print(f"✗ Error: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()


def _identify_field(body_bytes, char_pos, payload_obj):
    """Try to identify which JSON field the error position falls in."""
    decoded = body_bytes.decode('utf-8', errors='replace')

    # Find system prompt content boundaries in the compact JSON
    sys_content_json = json.dumps(DEFAULT_SYSTEM_PROMPT, ensure_ascii=False)
    # In the compact payload, the system prompt appears as: "content":"<escaped sys prompt>"
    # Find it
    sys_search = '"role":"system","content":'
    sys_idx = decoded.find(sys_search)
    if sys_idx >= 0:
        content_start = sys_idx + len(sys_search)
        # The content value starts with a quote, find where it ends
        # It ends with an unescaped quote
        content_end = content_start + 1  # skip opening quote
        i = content_end
        while i < len(decoded):
            if decoded[i] == '\\':
                i += 2  # skip escaped char
                continue
            if decoded[i] == '"':
                content_end = i
                break
            i += 1

        if content_start <= char_pos <= content_end:
            offset_in_sys = char_pos - content_start - 1  # -1 for opening quote
            print(f"\n📍 Error position is INSIDE the SYSTEM PROMPT content!")
            print(f"   Offset within system prompt JSON: ~{offset_in_sys}")
            # Map back to the original string
            sys_escaped = decoded[content_start+1:content_end]
            # Find what part of the original string this corresponds to
            if 0 < offset_in_sys < len(sys_escaped):
                start = max(0, offset_in_sys - 80)
                end = min(len(sys_escaped), offset_in_sys + 80)
                print(f"   System prompt content around that position:")
                print(f"   ...{sys_escaped[start:end]}...")
            return

    # Check tools section
    tools_search = '"tools":['
    tools_idx = decoded.find(tools_search)
    if tools_idx >= 0 and char_pos > tools_idx:
        tools_end = decoded.find(']', tools_idx)
        # Find last ] that closes tools
        bracket_count = 0
        for i in range(tools_idx + len(tools_search) - 1, len(decoded)):
            if decoded[i] == '[':
                bracket_count += 1
            elif decoded[i] == ']':
                if bracket_count == 0:
                    tools_end = i
                    break
                bracket_count -= 1

        if tools_idx <= char_pos <= tools_end:
            offset_in_tools = char_pos - tools_idx
            print(f"\n📍 Error position is INSIDE the TOOLS array!")
            print(f"   Offset within tools JSON: ~{offset_in_tools}")

            # Find which tool definition this falls in
            tool_start = 0
            for i, tool in enumerate(payload_obj['tools']):
                tool_json = json.dumps(tool, ensure_ascii=False, separators=(',',':'))
                # Find this tool's position in the tools array
                tool_idx = decoded.find(f'"name":"{tool["function"]["name"]}"', tools_idx)
                if tool_idx >= 0 and tool_idx < char_pos:
                    tool_start = tool_idx
                elif tool_idx >= 0 and tool_idx > char_pos:
                    # The error is in the previous tool
                    prev_tool = payload_obj['tools'][i-1]
                    print(f"   Likely in tool: {prev_tool['function']['name']}")
                    break
            return

    print(f"\n📍 Could not determine which field contains char {char_pos}")
    # Show the general area
    start = max(0, char_pos - 300)
    end = min(len(decoded), char_pos + 100)
    print(f"   Context (wider view): ...{decoded[start:end]}...")


if __name__ == '__main__':
    test_api()
