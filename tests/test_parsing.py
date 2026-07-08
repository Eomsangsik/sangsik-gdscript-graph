from __future__ import annotations


def test_syntax_error_in_one_file_does_not_abort_build(godot_project):
    godot_project.write("broken.gd", """
extends Node

func broken( -> void:
    pass
""")
    godot_project.write("fine.gd", """
extends Node

func ok() -> void:
    pass
""")
    conn = godot_project.build()
    files = {r["res_path"]: r["parse_error"] for r in conn.execute("SELECT res_path, parse_error FROM files")}
    assert files["res://broken.gd"] is not None
    assert files["res://fine.gd"] is None
    functions = [r["name"] for r in conn.execute("SELECT name FROM symbols WHERE kind='function'")]
    assert "ok" in functions
