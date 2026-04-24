#!/usr/bin/env python3
"""Test with FULL 42 tools to see if Step-3.5-Flash gets overwhelmed."""
import json
import urllib.request
import os
import re
import sys

API_URL = "https://api-inference.modelscope.cn/v1/chat/completions"
API_KEY = "ms-3eca52df-ea14-481b-9e72-73b988b612f7"
MODEL = "stepfun-ai/Step-3.5-Flash"

# Extract AGENT_TOOLS from chat.py
_chat_py_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'routes', 'chat.py')
with open(_chat_py_path, 'r') as f:
    chat_content = f.read()

_tools_match = re.search(r'AGENT_TOOLS\s*=\s*\[', chat_content)
if _tools_match:
    start_pos = _tools_match.start()
    bracket_count = 0
    end_pos = start_pos
    in_string = False
    string_char = None
    for i in range(_tools_match.end() - 1, len(chat_content)):
        c = chat_content[i]
        if in_string:
            if c == '\\':
                continue
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
    exec_globals = {}
    exec(tools_code, exec_globals)
    AGENT_TOOLS = exec_globals['AGENT_TOOLS']
    print(f"Loaded {len(AGENT_TOOLS)} tools")
else:
    print("ERROR: Could not find AGENT_TOOLS")
    sys.exit(1)

_IDE_PORT = os.environ.get('MUSIDE_PORT', '12345')

SYSTEM_PROMPT = f"""You are MusIDE AI Agent, a powerful coding assistant integrated in a mobile IDE.
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

## CRITICAL SAFETY RULES - NEVER VIOLATE
The process muside_server.py and port {_IDE_PORT} are the core of this IDE and AI assistant.
- NEVER stop, kill, or terminate the muside_server.py process
- NEVER use kill_port on port {_IDE_PORT}

## Platform Awareness
- On Linux/macOS: paths use forward slashes, Python is python3, venv binaries in bin/
"""

def test_full_tools(user_msg):
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_msg}
    ]
    
    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.6,
        "max_tokens": 2048,
        "tools": AGENT_TOOLS,
        "tool_choice": "auto",
        "stream": False,
    }
    
    body_bytes = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    print(f"Payload size: {len(body_bytes)} bytes ({len(body_bytes)/1024:.1f} KB)")
    
    headers = {
        'Content-Type': 'application/json; charset=utf-8',
        'Authorization': f'Bearer {API_KEY}',
    }
    
    req = urllib.request.Request(API_URL, body_bytes, headers=headers, method='POST')
    
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode())
            msg = result.get('choices', [{}])[0].get('message', {})
            tool_calls = msg.get('tool_calls', [])
            content = msg.get('content', '')
            
            tools_used = [tc.get('function', {}).get('name', '?') for tc in tool_calls]
            print(f"User: {user_msg[:80]}")
            print(f"Tools called: {tools_used if tools_used else '(none, text only)'}")
            if content:
                print(f"Text: {content[:300]}")
            for tc in tool_calls:
                fn = tc.get('function', {})
                print(f"  → {fn.get('name')}({fn.get('arguments', '')[:150]})")
            return tools_used
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"ERROR: HTTP {e.code}: {body[:300]}")
        return []
    except Exception as e:
        print(f"ERROR: {e}")
        return []

print("\n=== Test 1: Read file ===")
test_full_tools("Read the file /home/user/project/app.py")

print("\n=== Test 2: Edit file ===")
test_full_tools("Change the port from 5000 to 8080 in /home/user/project/app.py")

print("\n=== Test 3: Search code ===")
test_full_tools("Find all places where the database connection is configured")

print("\n=== Test 4: List directory ===")
test_full_tools("Show me what files are in the project directory")
