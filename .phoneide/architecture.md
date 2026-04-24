# Project Architecture

## Backend (Python Flask)

```
app.py                  # Flask application entry point
routes/
  chat.py               # AI agent loop, tool definitions, LLM integration, SSE streaming
  files.py              # File CRUD operations
  run.py                # Code execution (run/stop/restart)
  git.py                # Git operations
  browser.py            # Browser debugging tools
  debug.py              # Python debugger integration
  server_mgmt.py        # Server start/stop/restart
  update.py             # Self-update mechanism
  venv.py               # Virtual environment management
  ast_index.py          # Tree-sitter AST engine (find_definition, find_references, file_structure)

utils.py                # Config, workspace, system info utilities
```

## Frontend

```
templates/
  index.html            # Main IDE page (includes all JS/CSS inline)
static/
  js/
    chat.js             # Chat UI, SSE handling, tool result rendering
    editor.js           # CodeMirror editor setup
    file-tree.js        # File browser tree
    git.js              # Git panel
    browser.js          # Browser debug panel
    debug.js            # Debug panel
  css/
    (inline in templates for mobile performance)
```

## Data Flow
1. User sends message → POST `/api/chat/send/stream`
2. `run_agent_loop_stream()` → builds context → calls LLM API
3. LLM returns tool_calls → executed → results fed back
4. Each event yielded as SSE → frontend renders in real-time
5. Tool calls: `tool_start` → show spinner, `tool_result` → show result card
