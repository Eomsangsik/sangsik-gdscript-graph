from __future__ import annotations

from helpers import resolved_calls, symbols, unresolved_calls


def test_property_setter_call_is_tracked(godot_project):
    """Regression test: calls made inside an inline property `set(value):`
    body must not vanish from the graph -- they used to be silently
    dropped entirely since these accessor bodies aren't `func_def` nodes."""
    godot_project.write("stats.gd", """
extends Node

var health: int = 10:
    set(value):
        health = value
        update_ui()
    get:
        return health

func update_ui() -> void:
    pass
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    assert any(c["src_fn"] == "health.set" and c["tgt_fn"] == "update_ui" for c in calls)


def test_property_accessors_registered_as_symbols(godot_project):
    godot_project.write("stats.gd", """
extends Node

var health: int = 10:
    set(value):
        health = value
    get:
        return health
""")
    conn = godot_project.build()
    names = {s["name"] for s in symbols(conn)}
    assert "health.set" in names
    assert "health.get" in names


def test_annotation_between_property_var_and_accessor_block_does_not_drop_it(godot_project):
    """Regression test: `iter_property_accessor_defs` associates a
    `property_body_def` with its `class_var_stmt` via literal sibling
    adjacency in the parse tree -- but gdtoolkit's grammar allows an
    annotation (e.g. `@export`) to sit between them while the indented
    accessor block still belongs to the same property. Without treating
    annotations as transparent, the accessor's call graph vanished
    entirely (not even recorded as unresolved) instead of being tracked,
    because the annotation was treated as an ordinary intervening
    statement that severs the pending property-name association."""
    godot_project.write("stats.gd", """
extends Node

var health: int = 10:
@export
    set(value):
        health = value
        update_ui()
    get:
        return health

func update_ui() -> void:
    pass
""")
    conn = godot_project.build()
    names = {s["name"] for s in symbols(conn)}
    assert "health.set" in names
    assert "health.get" in names
    calls = resolved_calls(conn)
    assert any(c["src_fn"] == "health.set" and c["tgt_fn"] == "update_ui" for c in calls)


def test_static_var_property_setter_call_is_tracked(godot_project):
    """Regression test: `static var x: T = v:` wraps its declaration one
    level deeper (static_class_var_stmt -> class_var_stmt) than a regular
    var, which must not break property-name association."""
    godot_project.write("stats.gd", """
extends Node

static var health: int = 10:
    set(value):
        health = value
        update_ui()

static func update_ui() -> void:
    pass
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    assert any(c["src_fn"] == "health.set" and c["tgt_fn"] == "update_ui" for c in calls)


def test_property_setter_param_shadows_same_named_autoload_stays_unresolved(godot_project):
    """Regression test: a setter's implicit value parameter always shadows
    a same-named autoload/class_name, same as any other local -- must not
    silently resolve to the global of that name (the setter's own value
    parameter isn't tracked as a func_def, so this shadowing rule used to
    silently not apply inside accessor bodies)."""
    godot_project.write("project.godot", '[application]\n\n[autoload]\nvalue="*res://value_singleton.gd"\n')
    godot_project.write("value_singleton.gd", "extends Node\nfunc something():\n    pass\n")
    godot_project.write("player.gd", """
extends Node

var health: int = 10:
    set(value):
        value.something()
        health = value
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    assert not any(c["src_fn"] == "health.set" and c["tgt_fn"] == "something" for c in calls)
    shadowed = next(
        u for u in unresolved_calls(conn) if u["source_function"] == "health.set" and u["called_name"] == "something"
    )
    assert shadowed["reason"] == "unknown_receiver"


def test_local_const_inside_property_accessor_is_not_misattributed_as_class_field(godot_project):
    """Regression test: a `const` declared inside a property accessor
    (`get:`/`set(value):`) body is a local, not a class field -- `const`
    shares its grammar node with class-level consts, so without a guard
    stopping at the accessor-body boundary it leaks into the symbols table
    (and `search` results) as a phantom class-level const, the same class
    of bug already guarded against for regular function bodies."""
    godot_project.write("thing.gd", """
extends Node

var health: int = 100:
    get:
        const BONUS = 5
        return health + BONUS
    set(value):
        const CAP = 200
        health = min(value, CAP)

func foo():
    const OUTER = 1
    print(OUTER)
""")
    conn = godot_project.build()
    names = {s["name"] for s in symbols(conn)}
    assert "BONUS" not in names
    assert "CAP" not in names
    assert "OUTER" not in names
    assert "health" in names


def test_property_accessor_in_inner_class_scoped_correctly(godot_project):
    godot_project.write("stats.gd", """
extends Node

class Inner:
    var health: int = 10:
        set(value):
            health = value
            update_ui()

    func update_ui() -> void:
        pass
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    assert any(
        c["src_fn"] == "health.set" and c["src_scope"] == "Inner" and c["tgt_fn"] == "update_ui"
        for c in calls
    )
