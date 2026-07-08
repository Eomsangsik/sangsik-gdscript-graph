from __future__ import annotations

from helpers import resolved_calls, symbols


def test_field_const_enum_symbols_extracted(godot_project):
    godot_project.write("stats.gd", """
extends Node

const MAX_HP = 100
enum State { IDLE, RUNNING }
var health: int = 100
""")
    conn = godot_project.build()
    rows = {(s["name"], s["kind"]) for s in symbols(conn)}
    assert ("MAX_HP", "const") in rows
    assert ("State", "enum") in rows
    assert ("health", "var") in rows


def test_same_named_signal_in_different_scopes_both_stored_distinctly(godot_project):
    """Regression test: a top-level signal and an inner-class signal with
    the same name must both be stored with their correct, distinct scope."""
    godot_project.write("x.gd", """
extends Node

signal updated

class Inner:
    signal updated
""")
    conn = godot_project.build()
    rows = [s for s in symbols(conn) if s["name"] == "updated" and s["kind"] == "signal"]
    scopes = {s["scope"] for s in rows}
    assert len(rows) == 2
    assert scopes == {None, "Inner"}


def test_local_const_inside_function_not_extracted_as_field(godot_project):
    """Regression test: `const` shares its grammar node between class-level
    and function-local declarations, so a local const must not be
    misattributed as a class field."""
    godot_project.write("stats.gd", """
extends Node

const OUTER = 1

func _ready() -> void:
    const LOCAL = 2
    print(LOCAL)
""")
    conn = godot_project.build()
    rows = {(s["name"], s["kind"]) for s in symbols(conn)}
    assert ("OUTER", "const") in rows
    assert ("LOCAL", "const") not in rows


def test_local_const_inside_class_level_lambda_not_extracted_as_field(godot_project):
    """Regression test: a `const` declared inside a lambda used as a
    class-level `var`/`const` initializer (`var handler = func(): ...`) is
    local to the lambda, not a class field -- same hazard as a local const
    inside a function body or property accessor."""
    godot_project.write("stats.gd", """
extends Node

var handler = func():
    const LOCAL_X = 42
    print(LOCAL_X)

func _ready() -> void:
    handler.call()
""")
    conn = godot_project.build()
    rows = {(s["name"], s["kind"]) for s in symbols(conn)}
    assert ("LOCAL_X", "const") not in rows
    assert ("handler", "var") in rows


def test_abstract_func_def_is_registered_as_a_symbol_and_callable(godot_project):
    """Regression test: `func take_damage(amount)` with no trailing `:`/body
    (an abstract method declaration, gdtoolkit's `abstract_func_def`) is
    valid GDScript, not a parse error -- it must still be registered as a
    real function symbol and resolvable as a call target, not silently
    invisible everywhere downstream."""
    godot_project.write("player.gd", """
extends Node
class_name Player

func take_damage(amount)

func heal(amount):
    pass

func run() -> void:
    take_damage(5)
    heal(10)
""")
    conn = godot_project.build()
    rows = {(s["name"], s["kind"]) for s in symbols(conn)}
    assert ("take_damage", "function") in rows

    calls = resolved_calls(conn)
    assert any(c["src_fn"] == "run" and c["tgt_fn"] == "take_damage" for c in calls)


def test_anonymous_enum_is_skipped(godot_project):
    godot_project.write("stats.gd", """
extends Node

enum { IDLE, RUNNING }

func noop() -> void:
    pass
""")
    conn = godot_project.build()
    rows = symbols(conn)
    assert not any(s["kind"] == "enum" for s in rows)
