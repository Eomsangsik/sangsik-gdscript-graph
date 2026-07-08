from __future__ import annotations

from helpers import resolved_calls


def test_bare_call_does_not_leak_into_inner_class_scope(godot_project):
    """Regression test: a same-named function inside `class Inner:` must
    not be linked as the target of a bare call made from top-level scope."""
    godot_project.write("weird.gd", """
extends Node

class Inner:
    func setup() -> void:
        pass

func setup() -> void:
    pass

func use_it() -> void:
    setup()
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    match = next(c for c in calls if c["src_fn"] == "use_it")
    assert match["tgt_fn"] == "setup"
    assert match["tgt_scope"] is None


def test_inner_class_call_resolves_to_its_own_scope(godot_project):
    godot_project.write("weird.gd", """
extends Node

class Inner:
    func setup() -> void:
        pass

    func run() -> void:
        setup()

func setup() -> void:
    pass
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    match = next(c for c in calls if c["src_fn"] == "run")
    assert match["tgt_fn"] == "setup"
    assert match["tgt_scope"] == "Inner"
