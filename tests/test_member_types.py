from __future__ import annotations

from helpers import resolved_calls, signal_connections, unresolved_calls

PAUSE_MENU = "class_name PauseMenu\nextends Control\nsignal closed\nfunc open():\n    pass\nfunc close():\n    pass\n"


def _targets(conn, src_fn: str) -> set[tuple[str, str | None, str]]:
    return {
        (c["tgt_file"], c["tgt_scope"], c["tgt_fn"])
        for c in resolved_calls(conn) if c["src_fn"] == src_fn
    }


def _unresolved_reason(conn, called_name: str, source_function: str) -> str:
    return next(
        u["reason"] for u in unresolved_calls(conn)
        if u["called_name"] == called_name and u["source_function"] == source_function
    )


def test_typed_member_field_call_resolves(godot_project):
    """Regression test: a call through a class-level field with an explicit
    type annotation (`var a: T`, including `@onready`/`@export` forms and a
    field with a property accessor) used to be left unresolved -- only
    function-local types were tracked."""
    godot_project.write("pause_menu.gd", PAUSE_MENU)
    godot_project.write("game.gd", """
extends Node

@onready var a: PauseMenu = $A
@export var b: PauseMenu
var c: PauseMenu:
    set(v):
        c = v

func run_a() -> void:
    a.open()

func run_b() -> void:
    b.open()

func run_c() -> void:
    c.close()
""")
    conn = godot_project.build()
    assert _targets(conn, "run_a") == {("res://pause_menu.gd", None, "open")}
    assert _targets(conn, "run_b") == {("res://pause_menu.gd", None, "open")}
    assert _targets(conn, "run_c") == {("res://pause_menu.gd", None, "close")}


def test_inferred_member_field_from_as_cast_and_new_resolves(godot_project):
    """Regression test: `@onready var x := $Path as T` (the feedback's
    `$Path as PauseMenu` case) and `var x := T.new()` fix the field's type
    at declaration, so calls through them must resolve."""
    godot_project.write("pause_menu.gd", PAUSE_MENU)
    godot_project.write("game.gd", """
extends Node

@onready var _pause_menu := $InterfaceLayer/PauseMenu as PauseMenu
var _spare := PauseMenu.new()

func toggle() -> void:
    _pause_menu.open()
    _spare.close()
""")
    conn = godot_project.build()
    assert _targets(conn, "toggle") == {
        ("res://pause_menu.gd", None, "open"),
        ("res://pause_menu.gd", None, "close"),
    }


def test_untyped_member_initialized_with_new_stays_unresolved(godot_project):
    """A plain `var x = T.new()` is Variant -- it can be reassigned to
    anything later, so inferring T from the initializer could produce a
    wrong edge. It must stay honestly unresolved."""
    godot_project.write("pause_menu.gd", PAUSE_MENU)
    godot_project.write("game.gd", """
extends Node

var menu = PauseMenu.new()

func run() -> void:
    menu.open()
""")
    conn = godot_project.build()
    assert _targets(conn, "run") == set()
    assert _unresolved_reason(conn, "open", "run") == "unknown_receiver"


def test_inline_cast_receiver_resolves(godot_project):
    godot_project.write("pause_menu.gd", PAUSE_MENU)
    godot_project.write("game.gd", """
extends Node

func run() -> void:
    ($Menu as PauseMenu).open()
""")
    conn = godot_project.build()
    assert _targets(conn, "run") == {("res://pause_menu.gd", None, "open")}


def test_inferred_local_var_from_as_cast_and_new_resolves(godot_project):
    godot_project.write("pause_menu.gd", PAUSE_MENU)
    godot_project.write("game.gd", """
extends Node

func run() -> void:
    var a := get_node("Menu") as PauseMenu
    a.open()
    var b := PauseMenu.new()
    b.close()
    var c = PauseMenu.new()
    c.open()
""")
    conn = godot_project.build()
    targets = [c for c in resolved_calls(conn) if c["src_fn"] == "run"]
    assert sorted(c["tgt_fn"] for c in targets) == ["close", "open"]
    # The untyped `var c = ...` call stays unresolved.
    assert any(u["receiver"] == "c" and u["reason"] == "unknown_receiver" for u in unresolved_calls(conn))


def test_field_chain_through_self_and_other_typed_fields_resolves(godot_project):
    """Regression test: `self.menu.open()` and `hud.menu.open()` (a field
    of a field) were collapsed into an unresolvable `<chained>` receiver."""
    godot_project.write("pause_menu.gd", PAUSE_MENU)
    godot_project.write("hud.gd", "class_name Hud\nextends Control\nvar menu: PauseMenu\n")
    godot_project.write("game.gd", """
extends Node

var menu: PauseMenu
var hud: Hud

func via_self() -> void:
    self.menu.open()

func via_field_chain() -> void:
    hud.menu.close()

func via_untyped_link() -> void:
    hud.missing_field.open()
""")
    conn = godot_project.build()
    assert _targets(conn, "via_self") == {("res://pause_menu.gd", None, "open")}
    assert _targets(conn, "via_field_chain") == {("res://pause_menu.gd", None, "close")}
    assert _targets(conn, "via_untyped_link") == set()
    assert _unresolved_reason(conn, "open", "via_untyped_link") == "unknown_receiver"


def test_member_field_inherited_from_parent_script_resolves(godot_project):
    godot_project.write("pause_menu.gd", PAUSE_MENU)
    godot_project.write("base_screen.gd", "class_name BaseScreen\nextends Node\nvar menu: PauseMenu\n")
    godot_project.write("game.gd", """
extends BaseScreen

func run() -> void:
    menu.open()
""")
    conn = godot_project.build()
    assert _targets(conn, "run") == {("res://pause_menu.gd", None, "open")}


def test_local_var_shadows_member_field_of_another_type(godot_project):
    godot_project.write("pause_menu.gd", PAUSE_MENU)
    godot_project.write("other.gd", "class_name Other\nextends Node\nfunc open():\n    pass\n")
    godot_project.write("game.gd", """
extends Node

var menu: PauseMenu

func run() -> void:
    var menu: Other = Other.new()
    menu.open()
""")
    conn = godot_project.build()
    assert _targets(conn, "run") == {("res://other.gd", None, "open")}


def test_member_field_shadows_same_named_autoload(godot_project):
    """A member var always shadows a same-named global (GDScript resolves
    class members before autoloads/class_names) -- an untyped one must
    leave the call unresolved rather than misattribute it to the global."""
    godot_project.write("project.godot", '[application]\n\n[autoload]\nGlobal="*res://global.gd"\n')
    godot_project.write("global.gd", "extends Node\nfunc notify():\n    pass\n")
    godot_project.write("game.gd", """
extends Node

var Global

func run() -> void:
    Global.notify()
""")
    conn = godot_project.build()
    assert _targets(conn, "run") == set()
    assert _unresolved_reason(conn, "notify", "run") == "unknown_receiver"


def test_engine_typed_member_field_stays_unresolved(godot_project):
    godot_project.write("game.gd", """
extends Node

@onready var timer: Timer = $Timer

func start() -> void:
    pass

func run() -> void:
    timer.start()
""")
    conn = godot_project.build()
    assert _targets(conn, "run") == set()
    # Recorded as a call into the engine's Timer.start, not a missed one.
    assert _unresolved_reason(conn, "start", "run") == "engine_receiver"


def test_const_preload_alias_resolves_static_and_instance_calls(godot_project):
    """`const Enemy = preload("enemy.gd")` (relative or res://) names that
    script: `Enemy.spawn()` is a call on it, `Enemy.new()` an instance of it."""
    godot_project.write("enemies/enemy.gd", "extends Node\nstatic func spawn():\n    pass\nfunc hit():\n    pass\n")
    godot_project.write("enemies/wave.gd", """
extends Node

const Enemy = preload("enemy.gd")
const EnemyAbs := preload("res://enemies/enemy.gd")

func run() -> void:
    Enemy.spawn()
    var e := Enemy.new()
    e.hit()
    var f: EnemyAbs = EnemyAbs.new()
    f.hit()
""")
    conn = godot_project.build()
    targets = [c for c in resolved_calls(conn) if c["src_fn"] == "run"]
    assert sorted((c["tgt_file"], c["tgt_fn"]) for c in targets) == [
        ("res://enemies/enemy.gd", "hit"),
        ("res://enemies/enemy.gd", "hit"),
        ("res://enemies/enemy.gd", "spawn"),
    ]


def test_inner_class_typed_var_resolves_to_inner_scope(godot_project):
    godot_project.write("server.gd", """
extends Node

class Peer extends RefCounted:
    func is_open() -> bool:
        return true

func is_open() -> bool:
    return false

func poll(peers: Array) -> void:
    for p: Peer in peers:
        p.is_open()
""")
    conn = godot_project.build()
    assert _targets(conn, "poll") == {("res://server.gd", "Peer", "is_open")}


def test_inner_class_member_field_does_not_leak_from_outer_class(godot_project):
    """An inner class can't see its outer class's (non-static) vars -- a
    same-named field on the outer class must not type a call inside it."""
    godot_project.write("pause_menu.gd", PAUSE_MENU)
    godot_project.write("game.gd", """
extends Node

var menu: PauseMenu

class Inner:
    var menu: PauseMenu

    func own() -> void:
        menu.open()

class Other:
    func leak() -> void:
        menu.open()
""")
    conn = godot_project.build()
    assert _targets(conn, "own") == {("res://pause_menu.gd", None, "open")}
    assert _targets(conn, "leak") == set()


def test_lambda_shadowed_member_field_call_stays_unresolved(godot_project):
    """Same rule as for locals: inside a lambda that re-declares a name,
    the enclosing class's field type for that name can't be trusted."""
    godot_project.write("pause_menu.gd", PAUSE_MENU)
    godot_project.write("bar.gd", "class_name Bar\nextends Node\nfunc do_bar():\n    pass\n")
    godot_project.write("game.gd", """
extends Node

var x: PauseMenu

func _ready() -> void:
    var callback = func():
        var x: Bar = Bar.new()
        x.do_bar()
    callback.call()
    x.open()
""")
    conn = godot_project.build()
    assert _targets(conn, "_ready") == {("res://pause_menu.gd", None, "open")}
    assert _unresolved_reason(conn, "do_bar", "_ready") == "unknown_receiver"


def test_signal_connect_through_typed_member_fields_resolves(godot_project):
    """Both sides of `.connect()` resolve through typed fields too:
    `menu.closed.connect(...)`, `self.menu.closed.connect(menu.open)`."""
    godot_project.write("pause_menu.gd", PAUSE_MENU)
    godot_project.write("game.gd", """
extends Node

@onready var menu := $Menu as PauseMenu

func _ready() -> void:
    menu.closed.connect(_on_menu_closed)
    self.menu.closed.connect(menu.open)

func _on_menu_closed() -> void:
    pass
""")
    conn = godot_project.build()
    conns = {(c["signal_file"], c["signal_name"], c["handler_file"], c["handler_fn"]) for c in signal_connections(conn)}
    assert conns == {
        ("res://pause_menu.gd", "closed", "res://game.gd", "_on_menu_closed"),
        ("res://pause_menu.gd", "closed", "res://pause_menu.gd", "open"),
    }
