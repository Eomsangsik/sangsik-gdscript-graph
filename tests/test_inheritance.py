from __future__ import annotations

from helpers import resolved_calls


def test_bare_call_to_inherited_method_resolves(godot_project):
    godot_project.write("base.gd", """
class_name Base
extends Node

func heal(amount: int) -> void:
    pass
""")
    godot_project.write("derived.gd", """
class_name Derived
extends Base

func special_heal() -> void:
    heal(10)
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    assert any(
        c["src_fn"] == "special_heal" and c["tgt_fn"] == "heal" and c["tgt_file"] == "res://base.gd"
        for c in calls
    )


def test_super_call_resolves_to_parent(godot_project):
    godot_project.write("base.gd", """
class_name Base
extends Node

func ready_up() -> void:
    pass
""")
    godot_project.write("derived.gd", """
class_name Derived
extends Base

func ready_up() -> void:
    super.ready_up()
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    assert any(
        c["src_file"] == "res://derived.gd" and c["src_fn"] == "ready_up"
        and c["tgt_file"] == "res://base.gd" and c["tgt_fn"] == "ready_up"
        for c in calls
    )


def test_string_path_extends_resolves_inheritance(godot_project):
    godot_project.write("base.gd", """
extends Node

func attack() -> void:
    pass
""")
    godot_project.write("derived.gd", """
extends "res://base.gd"

func special() -> void:
    attack()
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    assert any(
        c["src_fn"] == "special" and c["tgt_fn"] == "attack" and c["tgt_file"] == "res://base.gd"
        for c in calls
    )


def test_inner_class_own_extends_does_not_override_file_level_extends(godot_project):
    """Regression test: a nested `class Inner: extends ...` must not
    clobber the file's own top-level `extends` value."""
    godot_project.write("x.gd", """
extends BaseEnemy

class Loot:
    extends Resource

func attack() -> void:
    heal()
""")
    godot_project.write("base_enemy.gd", """
class_name BaseEnemy
extends Node

func heal() -> void:
    pass
""")
    conn = godot_project.build()
    row = conn.execute("SELECT extends FROM files WHERE res_path = 'res://x.gd'").fetchone()
    assert row["extends"] == "BaseEnemy"
    assert any(
        c["src_fn"] == "attack" and c["tgt_fn"] == "heal" and c["tgt_file"] == "res://base_enemy.gd"
        for c in resolved_calls(conn)
    )


def test_dotted_extends_to_nested_class_stays_unresolved_not_misattributed(godot_project):
    """Regression test: `extends Base.State` (extending a class nested
    inside another file's top-level class -- a common state-machine idiom)
    must not be truncated to plain `Base`, which would misattribute calls
    to Base's unrelated top-level members of the same name. Resolving into
    the nested scope itself isn't supported in v1 (same as inner classes'
    own extends not being tracked), so the honest result is unresolved."""
    godot_project.write("base.gd", """
extends Node
class_name Base

class State:
    func enter() -> void:
        pass

func enter() -> void:
    pass
""")
    godot_project.write("my_state.gd", """
extends Base.State

func enter() -> void:
    super.enter()
""")
    conn = godot_project.build()
    row = conn.execute("SELECT extends FROM files WHERE res_path = 'res://my_state.gd'").fetchone()
    assert row["extends"] == "Base.State"
    assert not any(c["src_fn"] == "enter" and c["src_file"] == "res://my_state.gd" for c in resolved_calls(conn))


def test_dotted_extends_with_classname_stays_unresolved_not_misattributed(godot_project):
    """Same as above, for the `class_name X extends Base.State` grammar
    form (classname_extends_stmt), which parses differently from a bare
    `extends Base.State` (extends_stmt)."""
    godot_project.write("base.gd", """
extends Node
class_name Base

class State:
    func enter() -> void:
        pass

func enter() -> void:
    pass
""")
    godot_project.write("my_state.gd", """
class_name MyState
extends Base.State

func enter() -> void:
    super.enter()
""")
    conn = godot_project.build()
    row = conn.execute("SELECT extends FROM files WHERE res_path = 'res://my_state.gd'").fetchone()
    assert row["extends"] == "Base.State"
    assert not any(c["src_fn"] == "enter" and c["src_file"] == "res://my_state.gd" for c in resolved_calls(conn))


def test_deep_inheritance_chain_resolves(godot_project):
    godot_project.write("a.gd", """
class_name A
extends Node

func root_method() -> void:
    pass
""")
    godot_project.write("b.gd", """
class_name B
extends A
""")
    godot_project.write("c.gd", """
class_name C
extends B

func use_it() -> void:
    root_method()
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    assert any(
        c["src_fn"] == "use_it" and c["tgt_fn"] == "root_method" and c["tgt_file"] == "res://a.gd"
        for c in calls
    )
