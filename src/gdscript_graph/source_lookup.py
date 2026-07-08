from __future__ import annotations

from pathlib import Path

from lark import Tree

from gdscript_graph.parsing import parse_file
from gdscript_graph.symbols import (
    _first_name_token_deep,
    first_name_token,
    iter_function_defs,
    iter_property_accessor_defs,
    iter_scoped_subtrees,
)

_FIELD_NODE_DATA = {"var": "class_var_stmt", "const": "const_stmt"}


def _node_span(node: Tree) -> tuple[int, int]:
    """Return (start_line, last_content_line), both 1-indexed and
    inclusive, from a Lark node's position metadata (`gather_metadata=True`
    is already enabled at parse time). `end_column == 1` means nothing on
    `end_line` itself was actually consumed by this node -- a common case
    right at a block boundary, e.g. a function whose `end_line` equals the
    *next* sibling's start line -- so the true last content line in that
    case is `end_line - 1`, not `end_line` itself (which would otherwise
    wrongly include one line of whatever comes next)."""
    m = node.meta
    last_line = m.end_line if m.end_column > 1 else m.end_line - 1
    return m.line, max(last_line, m.line)


def find_symbol_source(project_root: str, res_path: str, kind: str, scope: str | None, name: str) -> dict | None:
    """Re-parse the file `res_path` lives in -- fresh off disk, not
    whatever the db last captured, since a build can lag a live edit by up
    to the watcher's debounce window -- and return the verbatim source text
    of the exact (kind, scope, name) symbol. Returns None if the file can't
    be read/parsed, or the symbol can no longer be found in it at all (e.g.
    renamed or removed since the last build)."""
    file_path = Path(project_root) / res_path.removeprefix("res://")
    pr = parse_file(file_path, res_path)
    if pr.tree is None:
        return None

    span: tuple[int, int] | None = None

    if kind == "function":
        for fd in iter_function_defs(pr.tree):
            if fd.scope == scope and fd.name == name:
                span = _node_span(fd.node)
                break
        if span is None:
            for pa in iter_property_accessor_defs(pr.tree):
                if pa.scope == scope and pa.name == name:
                    body_nodes = [n for n in pa.body if isinstance(n, Tree)]
                    end = max((_node_span(n)[1] for n in body_nodes), default=pa.line)
                    span = (pa.line, max(end, pa.line))
                    break
    elif kind == "signal":
        for s, subtree in iter_scoped_subtrees(pr.tree):
            if subtree.data == "signal_stmt" and s == scope and first_name_token(subtree) == name:
                span = _node_span(subtree)
                break
    elif kind in _FIELD_NODE_DATA:
        target_data = _FIELD_NODE_DATA[kind]
        for s, subtree in iter_scoped_subtrees(pr.tree):
            if subtree.data == target_data and s == scope and _first_name_token_deep(subtree) == name:
                span = _node_span(subtree)
                break
    elif kind == "enum":
        for s, subtree in iter_scoped_subtrees(pr.tree):
            if subtree.data != "enum_stmt" or s != scope:
                continue
            named = next((c for c in subtree.children if isinstance(c, Tree) and c.data == "enum_named"), None)
            if named is not None and first_name_token(named) == name:
                span = _node_span(subtree)
                break

    if span is None:
        return None

    start_line, end_line = span
    lines = pr.source.splitlines()
    text = "\n".join(lines[start_line - 1:end_line])
    return {"file": res_path, "start_line": start_line, "end_line": end_line, "text": text}
