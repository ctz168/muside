# Project Rules & Guidelines

## Architecture
- PhoneIDE is a Python Flask application serving a mobile-responsive web IDE
- Backend: Flask routes in `routes/` (chat.py, files.py, run.py, git.py, browser.py, debug.py, server_mgmt.py, update.py, venv.py)
- Frontend: CodeMirror 5 editor + custom JS in `static/js/` (chat.js, editor.js, file-tree.js, git.js, etc.)
- Configuration: `utils.py` for workspace/config management, `config.json` per project
- Code intelligence: `routes/ast_index.py` — tree-sitter AST engine for semantic code analysis

## Key Patterns
- SSE (Server-Sent Events) for streaming agent responses to frontend
- Agent Loop pattern: LLM → tool_calls → execute → feed results back → LLM → repeat
- Tool handlers: `_tool_*` functions registered in `_TOOL_HANDLERS` dict
- File paths must always be validated with `_validate_path()` before access
- Workspace isolation: all file operations are scoped to the project directory

## Important Constraints
- Mobile-first design — UI must work well on small screens
- Single-file chat.js is intentional ( bundled inline in HTML template)
- All tool results should be concise to conserve context window tokens
- AST index (`project_index`) auto-updates when files are written/edited
