"""
AST Index Engine for MusIDE
Uses tree-sitter for semantic code analysis: find_definition, find_references, file_structure.
Supports Python, JavaScript, TypeScript, Go.
"""

import os
import threading
import time
from collections import defaultdict

try:
    import tree_sitter_python as tspython
    import tree_sitter_javascript as tsjs
    import tree_sitter_typescript as tsts
    import tree_sitter_go as tsgo
    from tree_sitter import Language, Parser, Node
    _HAS_TREE_SITTER = True
except ImportError:
    _HAS_TREE_SITTER = False

# ==================== Language Registry ====================

_LANGUAGE_PARSERS = {}  # ext -> Parser
_LANGUAGE_CACHE = {}    # ext -> {parser, language}

_SKIP_DIRS = {'.git', '__pycache__', 'node_modules', '.venv', 'venv', '.idea',
              '.vscode', 'dist', 'build', '.next', '.muside'}

_SOURCE_EXTENSIONS = {
    '.py': 'python',
    '.js': 'javascript',
    '.mjs': 'javascript',
    '.cjs': 'javascript',
    '.jsx': 'javascript',
    '.ts': 'typescript',
    '.tsx': 'typescript',
    '.go': 'go',
}

# ==================== Parser Initialization ====================

def _init_parsers():
    """Initialize tree-sitter parsers for all supported languages."""
    if not _HAS_TREE_SITTER:
        return
    if _LANGUAGE_CACHE:
        return
    try:
        _LANGUAGE_CACHE['python'] = {
            'parser': Parser(Language(tspython.language())),
        }
        _LANGUAGE_CACHE['javascript'] = {
            'parser': Parser(Language(tsjs.language())),
        }
        _LANGUAGE_CACHE['typescript'] = {
            'parser': Parser(Language(tsts.language_typescript())),
        }
        _LANGUAGE_CACHE['go'] = {
            'parser': Parser(Language(tsgo.language())),
        }
    except Exception as e:
        print(f'[AST] Failed to initialize parsers: {e}')

_init_parsers()


def get_parser(ext):
    """Get a tree-sitter parser for the given file extension."""
    lang = _SOURCE_EXTENSIONS.get(ext)
    if not lang:
        return None
    entry = _LANGUAGE_CACHE.get(lang)
    if not entry:
        return None
    return entry['parser']


# ==================== AST Node Helpers ====================

def _get_child_by_type(node, node_type):
    """Get the first child of a specific type."""
    for child in node.children:
        if child.type == node_type:
            return child
    return None


def _get_descendant_by_type(node, node_type):
    """Get the first descendant of a specific type (depth-first)."""
    for child in node.children:
        if child.type == node_type:
            return child
        result = _get_descendant_by_type(child, node_type)
        if result:
            return result
    return None


def _node_text(node, source_bytes):
    """Get the text content of a node."""
    return source_bytes[node.start_byte:node.end_byte].decode('utf-8', errors='replace')


def _node_start_line(node):
    return node.start_point[0] + 1  # 1-indexed


def _is_inside_string_or_comment(node):
    """Check if a node is inside a string literal or comment."""
    parent = node.parent
    while parent:
        if parent.type in ('string', 'string_literal', 'comment', 'line_comment',
                          'block_comment', 'docstring', 'fstring'):
            return True
        parent = parent.parent
    return False


# ==================== Symbol Extraction (Definitions) ====================

def extract_definitions(filepath, source_bytes=None):
    """Extract all definitions (classes, functions, methods, variables) from a source file.
    Returns a list of dicts: {name, kind, line, col, end_line, signature, parent}
    """
    ext = os.path.splitext(filepath)[1].lower()
    parser = get_parser(ext)
    if not parser:
        return []

    if source_bytes is None:
        try:
            with open(filepath, 'rb') as f:
                source_bytes = f.read()
        except (OSError, IOError):
            return []

    tree = parser.parse(source_bytes)
    root = tree.root_node
    definitions = []

    # Language-specific definition extraction
    lang = _SOURCE_EXTENSIONS.get(ext)
    if lang == 'python':
        _extract_python_definitions(root, source_bytes, definitions, parent=None)
    elif lang in ('javascript', 'typescript'):
        _extract_js_ts_definitions(root, source_bytes, definitions, parent=None)
    elif lang == 'go':
        _extract_go_definitions(root, source_bytes, definitions, parent=None)

    return definitions


def _extract_python_definitions(node, source_bytes, defs, parent=None):
    """Recursively extract definitions from a Python AST."""
    for child in node.children:
        if child.type == 'function_definition':
            name_node = _get_child_by_type(child, 'identifier')
            if name_node:
                name = _node_text(name_node, source_bytes)
                sig = _node_text(child, source_bytes).split('\n')[0][:120]
                defs.append({
                    'name': name,
                    'kind': 'async function' if _get_child_by_type(child, 'async') else 'function',
                    'line': _node_start_line(child),
                    'col': child.start_point[1],
                    'end_line': child.end_point[0] + 1,
                    'signature': sig,
                    'parent': parent,
                    'node_type': child.type,
                })
                # Recurse into function body for nested functions/classes
                _extract_python_definitions(child, source_bytes, defs, parent=name)

        elif child.type == 'class_definition':
            name_node = _get_descendant_by_type(child, 'identifier')
            if name_node:
                name = _node_text(name_node, source_bytes)
                # Get base classes
                bases = []
                arg_list = _get_child_by_type(child, 'argument_list')
                if arg_list:
                    for arg in arg_list.children:
                        if arg.type == 'identifier' or arg.type == 'attribute':
                            bases.append(_node_text(arg, source_bytes))
                sig = f"class {name}({', '.join(bases)})" if bases else f"class {name}"
                defs.append({
                    'name': name,
                    'kind': 'class',
                    'line': _node_start_line(child),
                    'col': child.start_point[1],
                    'end_line': child.end_point[0] + 1,
                    'signature': sig,
                    'parent': parent,
                    'node_type': child.type,
                })
                _extract_python_definitions(child, source_bytes, defs, parent=name)

        elif child.type == 'assignment':
            # Top-level or class-level variable assignments
            left = _get_child_by_type(child, 'left')
            if left and left.type == 'identifier':
                name = _node_text(left, source_bytes)
                # Only track UPPER_CASE constants at module/class level
                if name == name.upper() and len(name) > 1:
                    defs.append({
                        'name': name,
                        'kind': 'constant',
                        'line': _node_start_line(child),
                        'col': child.start_point[1],
                        'end_line': child.end_point[0] + 1,
                        'signature': f'{name} = ...',
                        'parent': parent,
                        'node_type': 'assignment',
                    })


def _extract_js_ts_definitions(node, source_bytes, defs, parent=None):
    """Recursively extract definitions from a JS/TS AST."""
    for child in node.children:
        # Class declarations
        if child.type in ('class_declaration', 'abstract_class_declaration'):
            name_node = _get_child_by_type(child, 'identifier')
            if name_node:
                name = _node_text(name_node, source_bytes)
                defs.append({
                    'name': name,
                    'kind': 'class',
                    'line': _node_start_line(child),
                    'col': child.start_point[1],
                    'end_line': child.end_point[0] + 1,
                    'signature': f"class {name}",
                    'parent': parent,
                    'node_type': child.type,
                })
                _extract_js_ts_definitions(child, source_bytes, defs, parent=name)

        # Function declarations
        elif child.type == 'function_declaration':
            name_node = _get_child_by_type(child, 'identifier')
            if name_node:
                name = _node_text(name_node, source_bytes)
                prefix = 'async ' if child.children and child.children[0].type == 'async' else ''
                sig = _node_text(child, source_bytes).split('{')[0].strip()[:120]
                defs.append({
                    'name': name,
                    'kind': f'{prefix}function',
                    'line': _node_start_line(child),
                    'col': child.start_point[1],
                    'end_line': child.end_point[0] + 1,
                    'signature': sig,
                    'parent': parent,
                    'node_type': child.type,
                })

        # Variable declarations with function/arrow assignment
        elif child.type == 'variable_declaration':
            for declarator in child.children:
                if declarator.type == 'variable_declarator':
                    name_node = _get_child_by_type(declarator, 'identifier')
                    if not name_node:
                        name_node = _get_child_by_type(declarator, 'array_pattern')
                        if name_node:
                            continue
                        name_node = _get_child_by_type(declarator, 'object_pattern')
                        if name_node:
                            continue
                    if not name_node:
                        continue
                    name = _node_text(name_node, source_bytes)
                    value = _get_child_by_type(declarator, 'function')
                    arrow = _get_child_by_type(declarator, 'arrow_function')
                    kind = 'variable'
                    if value or arrow:
                        kind = 'function'
                    defs.append({
                        'name': name,
                        'kind': kind,
                        'line': _node_start_line(child),
                        'col': child.start_point[1],
                        'end_line': child.end_point[0] + 1,
                        'signature': f'{name} = ...',
                        'parent': parent,
                        'node_type': child.type,
                    })

        # TypeScript interfaces and type aliases
        elif child.type in ('interface_declaration', 'type_alias_declaration'):
            name_node = _get_child_by_type(child, 'type_identifier')
            if name_node:
                name = _node_text(name_node, source_bytes)
                defs.append({
                    'name': name,
                    'kind': child.type.replace('_declaration', ''),
                    'line': _node_start_line(child),
                    'col': child.start_point[1],
                    'end_line': child.end_point[0] + 1,
                    'signature': f'{child.type.split("_")[0]} {name}',
                    'parent': parent,
                    'node_type': child.type,
                })

        # Export statements
        elif child.type == 'export_statement':
            _extract_js_ts_definitions(child, source_bytes, defs, parent=parent)

        # Method definitions inside class body
        elif child.type == 'class_body':
            for member in child.children:
                if member.type == 'method_definition':
                    name_node = _get_child_by_type(member, 'property_identifier')
                    if not name_node:
                        name_node = _get_child_by_type(member, 'private_property_identifier')
                    if name_node:
                        name = _node_text(name_node, source_bytes)
                        prefix = ''
                        if member.children and member.children[0].type == 'async':
                            prefix = 'async '
                        elif member.children and member.children[0].type == 'static':
                            prefix = 'static '
                        defs.append({
                            'name': name,
                            'kind': f'{prefix}method',
                            'line': _node_start_line(member),
                            'col': member.start_point[1],
                            'end_line': member.end_point[0] + 1,
                            'signature': f'{prefix}{name}(...)',
                            'parent': parent,
                            'node_type': 'method_definition',
                        })
                elif member.type in ('public_field_definition', 'property_definition'):
                    name_node = _get_child_by_type(member, 'property_identifier')
                    if name_node:
                        name = _node_text(name_node, source_bytes)
                        defs.append({
                            'name': name,
                            'kind': 'property',
                            'line': _node_start_line(member),
                            'col': member.start_point[1],
                            'end_line': member.end_point[0] + 1,
                            'signature': f'{name}',
                            'parent': parent,
                            'node_type': 'property_definition',
                        })


def _extract_go_definitions(node, source_bytes, defs, parent=None):
    """Extract definitions from a Go AST."""
    for child in node.children:
        if child.type == 'function_declaration':
            # func (receiver) Name(params) ...
            name_node = _get_descendant_by_type(child, 'field_identifier')
            if not name_node:
                name_node = _get_descendant_by_type(child, 'identifier')
            if name_node:
                name = _node_text(name_node, source_bytes)
                sig = _node_text(child, source_bytes).split('{')[0].strip()[:120]
                defs.append({
                    'name': name,
                    'kind': 'function',
                    'line': _node_start_line(child),
                    'col': child.start_point[1],
                    'end_line': child.end_point[0] + 1,
                    'signature': sig,
                    'parent': parent,
                    'node_type': child.type,
                })

        elif child.type == 'method_declaration':
            name_node = _get_descendant_by_type(child, 'field_identifier')
            if name_node:
                name = _node_text(name_node, source_bytes)
                sig = _node_text(child, source_bytes).split('{')[0].strip()[:120]
                defs.append({
                    'name': name,
                    'kind': 'method',
                    'line': _node_start_line(child),
                    'col': child.start_point[1],
                    'end_line': child.end_point[0] + 1,
                    'signature': sig,
                    'parent': parent,
                    'node_type': child.type,
                })

        elif child.type == 'type_declaration':
            type_spec = _get_child_by_type(child, 'type_spec')
            if type_spec:
                name_node = _get_child_by_type(type_spec, 'type_identifier')
                if name_node:
                    name = _node_text(name_node, source_bytes)
                    kind = 'type'
                    # Check if it's a struct or interface
                    type_body = _get_child_by_type(type_spec, 'struct_type')
                    if not type_body:
                        type_body = _get_child_by_type(type_spec, 'interface_type')
                    if type_body:
                        kind = 'struct' if type_body.type == 'struct_type' else 'interface'
                    defs.append({
                        'name': name,
                        'kind': kind,
                        'line': _node_start_line(child),
                        'col': child.start_point[1],
                        'end_line': child.end_point[0] + 1,
                        'signature': f'type {name} {kind}',
                        'parent': parent,
                        'node_type': child.type,
                    })

        elif child.type == 'var_declaration':
            for spec in child.children:
                if spec.type == 'var_spec':
                    for i, sc in enumerate(spec.children):
                        if sc.type == 'identifier':
                            defs.append({
                                'name': _node_text(sc, source_bytes),
                                'kind': 'variable',
                                'line': _node_start_line(child),
                                'col': child.start_point[1],
                                'end_line': child.end_point[0] + 1,
                                'signature': f'var {_node_text(sc, source_bytes)}',
                                'parent': parent,
                                'node_type': 'var_declaration',
                            })


# ==================== Reference Finding ====================

def find_references_ast(filepath, symbol, source_bytes=None):
    """Find all semantic references to a symbol in a file using AST.
    Returns a list of dicts: {line, col, text, context_kind}.
    Skips string literals and comments.
    """
    ext = os.path.splitext(filepath)[1].lower()
    parser = get_parser(ext)
    if not parser:
        return []

    if source_bytes is None:
        try:
            with open(filepath, 'rb') as f:
                source_bytes = f.read()
        except (OSError, IOError):
            return []

    tree = parser.parse(source_bytes)
    root = tree.root_node
    references = []

    _walk_for_references(root, symbol, source_bytes, references)

    return references


def _walk_for_references(node, symbol, source_bytes, refs, skip_definition=False):
    """Walk the AST to find identifier references matching symbol."""
    # Check if this node itself is a matching identifier
    if node.type == 'identifier' and _node_text(node, source_bytes) == symbol:
        if not _is_inside_string_or_comment(node):
            # Get the surrounding context
            parent = node.parent
            context_kind = 'reference'
            while parent:
                if parent.type in ('function_definition', 'function_declaration',
                                   'class_definition', 'class_declaration',
                                   'method_definition', 'method_declaration',
                                   'assignment', 'variable_declarator',
                                   'call', 'call_expression', 'import_statement',
                                   'import_from_statement', 'attribute',
                                   'member_expression', 'selector_expression'):
                    context_kind = parent.type
                    break
                parent = parent.parent

            # Skip the definition site
            is_definition = False
            if skip_definition and context_kind in ('assignment', 'function_definition',
                                                     'function_declaration', 'class_definition',
                                                     'class_declaration'):
                is_definition = True
            # Also skip if it's the name of a function/class definition
            direct_parent = node.parent
            if direct_parent and direct_parent.type in ('function_definition', 'function_declaration',
                                                         'class_definition', 'class_declaration',
                                                         'type_alias_declaration', 'interface_declaration',
                                                         'method_definition', 'method_declaration'):
                is_definition = True

            if not is_definition:
                line_text = source_bytes.split(b'\n')[node.start_point[0]].decode('utf-8', errors='replace').rstrip()
                refs.append({
                    'line': node.start_point[0] + 1,
                    'col': node.start_point[1],
                    'text': line_text,
                    'context_kind': context_kind,
                })

    # Also check field_identifier (Go methods, JS properties)
    if node.type == 'field_identifier' and _node_text(node, source_bytes) == symbol:
        if not _is_inside_string_or_comment(node):
            line_text = source_bytes.split(b'\n')[node.start_point[0]].decode('utf-8', errors='replace').rstrip()
            refs.append({
                'line': node.start_point[0] + 1,
                'col': node.start_point[1],
                'text': line_text,
                'context_kind': 'member_access',
            })

    # Check property_identifier (JS/TS)
    if node.type == 'property_identifier' and _node_text(node, source_bytes) == symbol:
        if not _is_inside_string_or_comment(node):
            # Skip if this is a method/property definition
            direct_parent = node.parent
            if direct_parent and direct_parent.type not in ('method_definition', 'property_definition',
                                                             'public_field_definition'):
                line_text = source_bytes.split(b'\n')[node.start_point[0]].decode('utf-8', errors='replace').rstrip()
                refs.append({
                    'line': node.start_point[0] + 1,
                    'col': node.start_point[1],
                    'text': line_text,
                    'context_kind': 'property_access',
                })

    # Recurse into children
    for child in node.children:
        _walk_for_references(child, symbol, source_bytes, refs, skip_definition)


# ==================== File Structure ====================

def get_file_structure(filepath, source_bytes=None):
    """Get the structural outline of a source file using AST.
    Returns a dict with imports, classes, functions, variables, interfaces.
    """
    ext = os.path.splitext(filepath)[1].lower()
    parser = get_parser(ext)
    if not parser:
        return None

    if source_bytes is None:
        try:
            with open(filepath, 'rb') as f:
                source_bytes = f.read()
        except (OSError, IOError):
            return None

    tree = parser.parse(source_bytes)
    root = tree.root_node

    lang = _SOURCE_EXTENSIONS.get(ext)
    if lang == 'python':
        return _get_python_structure(root, source_bytes)
    elif lang in ('javascript', 'typescript'):
        return _get_js_ts_structure(root, source_bytes)
    elif lang == 'go':
        return _get_go_structure(root, source_bytes)
    return None


def _get_python_structure(root, source_bytes):
    """Get Python file structure."""
    imports = []
    classes = []
    functions = []
    variables = []
    current_class = None

    for child in root.children:
        if child.type == 'import_statement':
            text = _node_text(child, source_bytes).strip()[:100]
            imports.append({'line': _node_start_line(child), 'text': text})
        elif child.type == 'import_from_statement':
            text = _node_text(child, source_bytes).strip()[:100]
            imports.append({'line': _node_start_line(child), 'text': text})
        elif child.type == 'class_definition':
            name_node = _get_descendant_by_type(child, 'identifier')
            if name_node:
                name = _node_text(name_node, source_bytes)
                current_class = name
                # Get bases
                bases = []
                arg_list = _get_child_by_type(child, 'argument_list')
                if arg_list:
                    for arg in arg_list.children:
                        if arg.type in ('identifier', 'attribute'):
                            bases.append(_node_text(arg, source_bytes))
                bases_str = f'({", ".join(bases)})' if bases else ''
                classes.append({'line': _node_start_line(child), 'text': f'class {name}{bases_str}', 'name': name})
                # Extract methods
                for member in child.children:
                    if member.type == 'block':
                        for item in member.children:
                            if item.type == 'function_definition':
                                mname = _get_descendant_by_type(item, 'identifier')
                                if mname:
                                    mname_str = _node_text(mname, source_bytes)
                                    prefix = 'async ' if _get_child_by_type(item, 'async') else ''
                                    # Get params
                                    params = _get_child_by_type(item, 'parameters')
                                    params_str = _node_text(params, source_bytes) if params else '()'
                                    functions.append({'line': _node_start_line(item), 'text': f'  {prefix}def {mname_str}{params_str}', 'parent': name})
        elif child.type == 'function_definition':
            current_class = None
            name_node = _get_descendant_by_type(child, 'identifier')
            if name_node:
                name = _node_text(name_node, source_bytes)
                prefix = 'async ' if _get_child_by_type(child, 'async') else ''
                params = _get_child_by_type(child, 'parameters')
                params_str = _node_text(params, source_bytes) if params else '()'
                functions.append({'line': _node_start_line(child), 'text': f'{prefix}def {name}{params_str}', 'parent': None})
        elif child.type == 'expression_statement':
            assign = _get_child_by_type(child, 'assignment')
            if assign:
                left = _get_child_by_type(assign, 'identifier')
                if left:
                    name = _node_text(left, source_bytes)
                    if name == name.upper() and len(name) > 1:
                        variables.append({'line': _node_start_line(assign), 'text': name})

    return {
        'imports': imports,
        'classes': classes,
        'functions': functions,
        'variables': variables,
    }


def _get_js_ts_structure(root, source_bytes):
    """Get JavaScript/TypeScript file structure."""
    imports = []
    classes = []
    functions = []
    variables = []

    def _walk(node, parent_class=None):
        for child in node.children:
            # Import/export declarations
            if child.type in ('import_statement', 'export_statement'):
                text = _node_text(child, source_bytes).strip()[:100]
                imports.append({'line': _node_start_line(child), 'text': text})
                if child.type == 'export_statement':
                    _walk(child, parent_class)
            elif child.type in ('class_declaration', 'abstract_class_declaration'):
                name_node = _get_child_by_type(child, 'identifier')
                if name_node:
                    name = _node_text(name_node, source_bytes)
                    classes.append({'line': _node_start_line(child), 'text': f'class {name}', 'name': name})
                    # Walk class body
                    body = _get_child_by_type(child, 'class_body')
                    if body:
                        _walk(body, parent_class=name)
            elif child.type == 'class_body':
                for member in child.children:
                    if member.type == 'method_definition':
                        name_node = _get_child_by_type(member, 'property_identifier')
                        if not name_node:
                            name_node = _get_child_by_type(member, 'private_property_identifier')
                        if name_node:
                            name = _node_text(name_node, source_bytes)
                            prefix = ''
                            if member.children and member.children[0].type in ('async', 'static', 'get', 'set'):
                                prefix = member.children[0].type + ' '
                            params = _get_child_by_type(member, 'formal_parameters')
                            params_str = _node_text(params, source_bytes) if params else '()'
                            functions.append({'line': _node_start_line(member), 'text': f'  {prefix}{name}{params_str}', 'parent': parent_class})
                    elif member.type in ('public_field_definition', 'property_definition'):
                        name_node = _get_child_by_type(member, 'property_identifier')
                        if name_node:
                            name = _node_text(name_node, source_bytes)
                            variables.append({'line': _node_start_line(member), 'text': f'  {name}', 'parent': parent_class})
            elif child.type == 'function_declaration':
                name_node = _get_child_by_type(child, 'identifier')
                if name_node:
                    name = _node_text(name_node, source_bytes)
                    prefix = 'async ' if child.children and child.children[0].type == 'async' else ''
                    params = _get_child_by_type(child, 'formal_parameters')
                    params_str = _node_text(params, source_bytes) if params else '()'
                    functions.append({'line': _node_start_line(child), 'text': f'{prefix}function {name}{params_str}', 'parent': None})
            elif child.type == 'variable_declaration':
                for declarator in child.children:
                    if declarator.type == 'variable_declarator':
                        name_node = _get_child_by_type(declarator, 'identifier')
                        if name_node:
                            name = _node_text(name_node, source_bytes)
                            value = _get_child_by_type(declarator, 'function')
                            arrow = _get_child_by_type(declarator, 'arrow_function')
                            if value or arrow:
                                params = _get_child_by_type(arrow or value, 'formal_parameters')
                                params_str = _node_text(params, source_bytes) if params else '()'
                                functions.append({'line': _node_start_line(child), 'text': f'{name}{params_str}', 'parent': None})
                            else:
                                variables.append({'line': _node_start_line(child), 'text': name, 'parent': None})
            elif child.type in ('interface_declaration', 'type_alias_declaration'):
                name_node = _get_child_by_type(child, 'type_identifier')
                if name_node:
                    name = _node_text(name_node, source_bytes)
                    kind = child.type.replace('_declaration', '')
                    classes.append({'line': _node_start_line(child), 'text': f'{kind} {name}', 'name': name})

    _walk(root)
    return {
        'imports': imports,
        'classes': classes,
        'functions': functions,
        'variables': variables,
    }


def _get_go_structure(root, source_bytes):
    """Get Go file structure."""
    imports = []
    functions = []
    types = []
    variables = []

    for child in root.children:
        if child.type == 'import_declaration':
            text = _node_text(child, source_bytes).strip()[:100]
            imports.append({'line': _node_start_line(child), 'text': text})
        elif child.type in ('function_declaration', 'method_declaration'):
            name_node = _get_descendant_by_type(child, 'field_identifier')
            if not name_node:
                name_node = _get_descendant_by_type(child, 'identifier')
            if name_node:
                name = _node_text(name_node, source_bytes)
                kind = 'method' if child.type == 'method_declaration' else 'func'
                sig = _node_text(child, source_bytes).split('{')[0].strip()[:120]
                functions.append({'line': _node_start_line(child), 'text': f'{kind} {sig}', 'parent': None})
        elif child.type == 'type_declaration':
            type_spec = _get_child_by_type(child, 'type_spec')
            if type_spec:
                name_node = _get_child_by_type(type_spec, 'type_identifier')
                if name_node:
                    name = _node_text(name_node, source_bytes)
                    kind = 'type'
                    type_body = _get_child_by_type(type_spec, 'struct_type')
                    if not type_body:
                        type_body = _get_child_by_type(type_spec, 'interface_type')
                    if type_body:
                        kind = 'struct' if type_body.type == 'struct_type' else 'interface'
                    types.append({'line': _node_start_line(child), 'text': f'{kind} {name}', 'name': name})
        elif child.type == 'var_declaration':
            for spec in child.children:
                if spec.type == 'var_spec':
                    names = [c for c in spec.children if c.type == 'identifier']
                    for n in names:
                        variables.append({'line': _node_start_line(spec), 'text': _node_text(n, source_bytes)})

    return {
        'imports': imports,
        'classes': types,  # Go types map to "classes"
        'functions': functions,
        'variables': variables,
    }


# ==================== Project-Level Symbol Index ====================

class ProjectIndex:
    """Thread-safe project-wide symbol index built from AST analysis.
    Caches definitions per file, supports fast symbol lookup.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._file_defs = {}    # filepath -> list of definitions
        self._symbol_index = defaultdict(list)  # symbol_name -> [(filepath, def_dict)]
        self._last_index_time = 0
        self._indexing = False

    def index_file(self, filepath, source_bytes=None):
        """Index a single file. Returns the list of definitions."""
        defs = extract_definitions(filepath, source_bytes)
        with self._lock:
            # Remove old entries for this file
            old_defs = self._file_defs.get(filepath, [])
            for d in old_defs:
                key = d['name']
                self._symbol_index[key] = [
                    (fp, dd) for fp, dd in self._symbol_index[key] if fp != filepath
                ]
            # Add new entries
            self._file_defs[filepath] = defs
            for d in defs:
                self._symbol_index[d['name']].append((filepath, d))
        return defs

    def remove_file(self, filepath):
        """Remove a file from the index."""
        with self._lock:
            old_defs = self._file_defs.pop(filepath, [])
            for d in old_defs:
                key = d['name']
                self._symbol_index[key] = [
                    (fp, dd) for fp, dd in self._symbol_index[key] if fp != filepath
                ]

    def find_definition(self, symbol, search_path=None):
        """Find definition of a symbol. Returns list of (filepath, def_dict)."""
        with self._lock:
            entries = list(self._symbol_index.get(symbol, []))

        if search_path and os.path.isfile(search_path):
            # Prefer definitions from the same file
            same_file = [(fp, d) for fp, d in entries if fp == search_path]
            if same_file:
                return same_file
        return entries

    def get_all_symbols(self):
        """Get all indexed symbols. Returns dict of symbol -> [(filepath, def)]."""
        with self._lock:
            return dict(self._symbol_index)

    def get_file_definitions(self, filepath):
        """Get definitions for a specific file."""
        with self._lock:
            return list(self._file_defs.get(filepath, []))

    def index_project(self, root_path, max_files=2000, max_time=15):
        """Index all source files in a project directory.
        Returns the number of files indexed.
        """
        start = time.time()
        with self._lock:
            self._indexing = True

        try:
            count = 0
            for dirpath, dirnames, filenames in os.walk(root_path):
                if time.time() - start > max_time or count >= max_files:
                    break
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
                for fname in filenames:
                    if time.time() - start > max_time or count >= max_files:
                        break
                    ext = os.path.splitext(fname)[1].lower()
                    if ext in _SOURCE_EXTENSIONS:
                        fpath = os.path.join(dirpath, fname)
                        self.index_file(fpath)
                        count += 1

            self._last_index_time = time.time()
            return count
        finally:
            with self._lock:
                self._indexing = False

    @property
    def is_indexing(self):
        with self._lock:
            return self._indexing

    @property
    def file_count(self):
        with self._lock:
            return len(self._file_defs)

    @property
    def symbol_count(self):
        with self._lock:
            return len(self._symbol_index)

    @property
    def last_index_time(self):
        return self._last_index_time


# Global singleton
project_index = ProjectIndex()
