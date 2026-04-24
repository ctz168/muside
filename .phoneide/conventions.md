# Coding Conventions

## Python
- Use f-strings for string formatting
- Use type hints in function signatures where helpful
- Tool handlers: prefix with `_tool_`, return string results
- Error returns: start with `"Error: "` prefix
- Constants: UPPER_SNAKE_CASE (e.g., `WORKSPACE`, `MAX_AGENT_ITERATIONS`)
- Keep tool result strings concise (context window is expensive)

## JavaScript (Frontend)
- Vanilla JS, no framework (for mobile performance)
- Event-driven SSE handling with `EventSource`
- Use CSS custom properties for theming (dark/light mode)
- Mobile-first responsive design

## File Operations
- Always use absolute paths
- Validate paths with `_validate_path()` before any file access
- Use `edit_file` (MultiEdit) for targeted changes, not `write_file`
- Read file before writing to understand existing content

## Testing
- Test code changes with `run_command` after modifications
- Use `browser_navigate` + `browser_console` for frontend testing
- Check `server_logs` for backend errors after changes
