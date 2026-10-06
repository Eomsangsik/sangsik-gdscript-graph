from __future__ import annotations

from helpers import resolved_calls, signal_connections, unresolved_calls


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


def test_dotted_extends_resolves_into_the_nested_class_not_its_outer_one(godot_project):
    """Regression test: `extends Base.State` (extending a class nested
    inside another file's top-level class -- a common state-machine idiom)
    must not be truncated to plain `Base`, which would misattribute calls
    to Base's unrelated top-level members of the same name: `super.enter()`
    reaches `State.enter`."""
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
    targets = {
        (c["tgt_file"], c["tgt_scope"], c["tgt_fn"])
        for c in resolved_calls(conn) if c["src_fn"] == "enter" and c["src_file"] == "res://my_state.gd"
    }
    assert targets == {("res://base.gd", "State", "enter")}


def test_dotted_extends_with_classname_resolves_into_the_nested_class(godot_project):
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
    targets = {
        (c["tgt_file"], c["tgt_scope"], c["tgt_fn"])
        for c in resolved_calls(conn) if c["src_fn"] == "enter" and c["src_file"] == "res://my_state.gd"
    }
    assert targets == {("res://base.gd", "State", "enter")}


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


def test_inner_classes_inherit_through_their_own_extends(godot_project):
    """Regression test: an inner class's own `extends` (`class Fancy
    extends Panel:`, or `extends X` as its first statement) was ignored --
    calls to inherited methods, `super.` calls, inherited signals and
    members all went unresolved, and engine methods (`draw_rect()` in a
    Control) looked like missing project methods."""
    godot_project.write("base_enemy.gd", "class_name BaseEnemy\nextends Node\nfunc heal() -> void:\n    pass\n")
    godot_project.write("ui.gd", """extends Node

class Panel extends Control:
    signal closed
    var label: Label
    func open() -> void:
        pass
    func _draw() -> void:
        draw_rect(Rect2(), Color())

class FancyPanel extends Panel:
    func show_it() -> void:
        open()
        super.open()
        closed.connect(_on_closed)
        label.set_text("x")
    func _on_closed() -> void:
        pass
    func _init() -> void:
        super()

class Body:
    extends Resource
    func save_it() -> void:
        emit_changed()

class Local extends BaseEnemy:
    func a() -> void:
        heal()

func run() -> void:
    var p := FancyPanel.new()
    p.open()
""")
    conn = godot_project.build()
    edges = {(c["src_scope"], c["src_fn"], c["tgt_file"], c["tgt_scope"], c["tgt_fn"]) for c in resolved_calls(conn)}
    assert {
        ("FancyPanel", "show_it", "res://ui.gd", "Panel", "open"),
        (None, "run", "res://ui.gd", "Panel", "open"),
        ("Local", "a", "res://base_enemy.gd", None, "heal"),
    } <= edges
    assert len([e for e in edges if e[1] == "show_it"]) == 1  # open() and super.open(): one edge each, same target
    assert len([c for c in resolved_calls(conn) if c["src_fn"] == "show_it"]) == 2

    [connection] = signal_connections(conn)
    assert (connection["signal_file"], connection["signal_scope"], connection["handler_fn"]) == (
        "res://ui.gd", "Panel", "_on_closed",
    )
    reasons = {u["called_name"]: u["reason"] for u in unresolved_calls(conn)}
    assert reasons == {
        "draw_rect": "engine_method", "Rect2": "builtin_function", "Color": "builtin_function",
        "set_text": "engine_receiver", "super": "builtin_function", "emit_changed": "engine_method",
    }
