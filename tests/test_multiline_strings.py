from __future__ import annotations

import json

from gdscript_graph.lexical import triple_quote_multiline_strings
from helpers import resolved_calls, symbols
from test_mcp_server import _run_session


def test_file_with_a_line_break_inside_a_string_is_fully_indexed(godot_project):
    """Regression test: Godot accepts a line break inside an ordinary
    "..." string; the grammar didn't, so the whole file was dropped -- 13
    files of one project, every call in and out of them lost."""
    godot_project.write("util.gd", "class_name Util\nextends Node\nstatic func fmt(s):\n    return s\n")
    godot_project.write("label.gd", '''extends Node

func title() -> String:
    return "[b]%s[/b]
[i]%s[/i]" % [Util.fmt("a"), 'x
y']

func after() -> void:
    title()
''')
    conn = godot_project.build()
    assert conn.execute("SELECT parse_error FROM files WHERE res_path = 'res://label.gd'").fetchone()[0] is None
    edges = {(c["src_fn"], c["tgt_fn"]) for c in resolved_calls(conn)}
    assert {("title", "fmt"), ("after", "title")} <= edges
    lines = {s["name"]: s["line"] for s in symbols(conn)}
    assert lines["after"] == 8  # the rewrite moved no line

    db_path = godot_project.root.parent / "graph.db"
    result = _run_session(db_path, lambda session: session.call_tool("node", {"name": "title"}), ["--no-watch"])
    source = json.loads(result.content[0].text)["source"]["text"]
    assert source.startswith('func title() -> String:\n    return "[b]%s[/b]\n[i]')  # the file's own text


def test_unparseable_file_still_registers_its_declarations(godot_project):
    """A file the grammar rejects for any other reason still declares
    functions others call: they must exist, or every call into the file
    is unresolved and its functions look like dead code."""
    godot_project.write("broken.gd", '''class_name Broken
extends "res://base.gd"

signal changed(value)

static func make() -> Broken:
    return Broken.new()

func helper():
    var s := """
func not_a_function():
"""
    this line is not valid gdscript !!!

class Inner:
    func deep():
        pass

func after_inner():
    pass
''')
    godot_project.write("base.gd", "extends Node\n")
    godot_project.write("user.gd", "extends Node\nfunc run():\n    Broken.make()\n")
    conn = godot_project.build()

    row = conn.execute("SELECT class_name, extends, parse_error FROM files WHERE res_path = 'res://broken.gd'").fetchone()
    assert (row["class_name"], row["extends"]) == ("Broken", "res://base.gd")
    assert "line scan" in row["parse_error"]
    declared = {(s["name"], s["kind"], s["scope"]) for s in symbols(conn) if s["res_path"] == "res://broken.gd"}
    assert declared == {
        ("changed", "signal", None), ("make", "function", None), ("helper", "function", None),
        ("deep", "function", "Inner"), ("after_inner", "function", None),
    }
    assert ("run", "make") in {(c["src_fn"], c["tgt_fn"]) for c in resolved_calls(conn)}


def test_rewrite_only_touches_strings_that_span_lines():
    source = (
        'var a = "one\nline"  # a "quote\' in a comment\n'
        "var b = 'it\\'s fine'\n"
        'var c = "escaped \\" quote\nend\\""\n'
        'var d = """already\ntriple"""\n'
        'var e = %"unique\nnode"\n'
    )
    assert triple_quote_multiline_strings(source) == (
        'var a = """one\nline"""  # a "quote\' in a comment\n'
        "var b = 'it\\'s fine'\n"
        'var c = """escaped \\" quote\nend\\""""\n'
        'var d = """already\ntriple"""\n'
        'var e = %"unique\nnode"\n'
    )
