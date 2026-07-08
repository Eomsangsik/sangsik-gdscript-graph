from __future__ import annotations

from helpers import signal_connections, unresolved_calls, unresolved_connections


def test_bare_and_self_connect_resolve(godot_project):
    godot_project.write("connector.gd", """
extends Node

signal died

func setup() -> void:
    died.connect(_on_died)
    died.connect(self._on_died_2)

func _on_died() -> void:
    pass

func _on_died_2() -> void:
    pass
""")
    conn = godot_project.build()
    conns = signal_connections(conn)
    handlers = {c["handler_fn"] for c in conns}
    assert handlers == {"_on_died", "_on_died_2"}
    assert all(c["signal_name"] == "died" for c in conns)


def test_self_prefixed_signal_connect_resolves(godot_project):
    """Regression test: `self.<signal>.connect(handler)` means exactly the
    same thing as bare `<signal>.connect(handler)` -- "self" is a reserved
    word that can never be a real autoload/class_name/local-var
    identifier, so it must be normalized to the bare form rather than
    routed through the (newer) chained-receiver resolution path, where it
    could never match anything and would always stay unresolved."""
    godot_project.write("connector.gd", """
extends Node

signal died

func setup() -> void:
    self.died.connect(_on_died)

func _on_died() -> void:
    pass
""")
    conn = godot_project.build()
    conns = signal_connections(conn)
    assert len(conns) == 1
    assert conns[0]["signal_name"] == "died"
    assert conns[0]["handler_fn"] == "_on_died"
    assert not unresolved_connections(conn)


def test_connect_call_site_is_not_also_reported_as_unresolved(godot_project):
    godot_project.write("connector.gd", """
extends Node

signal died

func setup() -> void:
    died.connect(_on_died)

func _on_died() -> void:
    pass
""")
    conn = godot_project.build()
    assert not any(u["called_name"] == "connect" for u in unresolved_calls(conn))


def test_connect_inside_inner_class_scope_resolves(godot_project):
    """Regression test: a signal declared and connected entirely inside a
    `class Inner:` must not be silently dropped, and its handler must
    resolve to the Inner-scoped function, not an unrelated top-level one."""
    godot_project.write("x.gd", """
extends Node

func _on_died() -> void:
    pass

class Inner:
    signal died

    func setup() -> void:
        died.connect(_on_died)

    func _on_died() -> void:
        pass
""")
    conn = godot_project.build()
    conns = signal_connections(conn)
    assert len(conns) == 1
    assert conns[0]["source_scope"] == "Inner"
    assert conns[0]["handler_scope"] == "Inner"
    assert not unresolved_connections(conn)


def test_connect_with_unparseable_handler_is_reported_distinctly(godot_project):
    """Regression test: `signal.connect(func(): ...)` must not leak into
    unresolved_calls as a bogus call to "connect", but should be visible
    in unresolved_connections with a distinct reason."""
    godot_project.write("x.gd", """
extends Node

signal died

func setup() -> void:
    died.connect(func(): print(1))
""")
    conn = godot_project.build()
    assert not any(u["called_name"] == "connect" for u in unresolved_calls(conn))
    unresolved = unresolved_connections(conn)
    assert len(unresolved) == 1
    assert unresolved[0]["reason"] == "unsupported_handler_shape"
    assert unresolved[0]["handler_name"] is None


def test_connect_handler_through_local_var_shadowing_class_name_resolves(godot_project):
    """Regression test: a `.connect()` handler reference through a local var
    whose name collides with a class_name must resolve via the local's
    declared type, not the identically named global class_name."""
    godot_project.write("enemy.gd", "class_name Enemy\nextends Node\nfunc on_died():\n    pass\n")
    godot_project.write("player.gd", "class_name Player\nextends Node\nfunc on_died():\n    pass\n")
    godot_project.write("world.gd", """
extends Node

signal died

func setup() -> void:
    var Player: Enemy = Enemy.new()
    died.connect(Player.on_died)
""")
    conn = godot_project.build()
    conns = signal_connections(conn)
    assert len(conns) == 1
    assert conns[0]["handler_file"] == "res://enemy.gd"


def test_connect_handler_through_untyped_local_var_shadowing_class_name_stays_unresolved(godot_project):
    """Regression test: an untyped local var whose name collides with a
    class_name still shadows it in a `.connect()` handler reference --
    must be left unresolved, not silently matched to the class_name."""
    godot_project.write("player.gd", "class_name Player\nextends Node\nfunc on_died():\n    pass\n")
    godot_project.write("world.gd", """
extends Node

signal died

func setup() -> void:
    var Player = get_node(".")
    died.connect(Player.on_died)
""")
    conn = godot_project.build()
    assert not signal_connections(conn)
    unresolved = unresolved_connections(conn)
    assert len(unresolved) == 1
    assert unresolved[0]["reason"] == "unknown_receiver"


def test_connect_inside_lambda_with_shadowed_var_stays_honestly_unresolved(godot_project):
    """Regression test: a `.connect()` call made *inside* a lambda, through
    a handler receiver name the lambda re-declares with a different type
    than the enclosing function's same-named var, must not resolve using
    the enclosing function's (stale, for this call site) type -- same rule
    as a plain call inside a lambda (see test_local_types.py)."""
    godot_project.write("foo.gd", "class_name Foo\nextends Node\nfunc _on_died():\n    pass\n")
    godot_project.write("bar.gd", "class_name Bar\nextends Node\nfunc _on_died():\n    pass\n")
    godot_project.write("main.gd", """
extends Node
signal died

func setup() -> void:
    var other_obj: Foo = Foo.new()
    var cb = func():
        var other_obj: Bar = Bar.new()
        died.connect(other_obj._on_died)
""")
    conn = godot_project.build()
    assert not signal_connections(conn)
    unresolved = unresolved_connections(conn)
    assert len(unresolved) == 1
    assert unresolved[0]["handler_receiver"] == "other_obj"
    assert unresolved[0]["reason"] == "unknown_receiver"


def test_connect_to_inherited_signal_via_bare_name_resolves(godot_project):
    """Regression test: `died.connect(_on_died)` where `died` is declared
    on an ancestor class in a different file (an extremely common pattern)
    must be recognized as a signal connection at all -- it used to be
    downgraded to a generic call to `connect()` and land in
    unresolved_calls, never even reaching unresolved_connections, because
    only the current file's own signals were considered."""
    godot_project.write("base.gd", "extends Node\nclass_name CharacterBase\nsignal died\n")
    godot_project.write("player.gd", """
extends CharacterBase
class_name Player

func _ready() -> void:
    died.connect(_on_died)

func _on_died() -> void:
    pass
""")
    conn = godot_project.build()
    conns = signal_connections(conn)
    assert len(conns) == 1
    assert conns[0]["signal_name"] == "died"
    assert conns[0]["handler_file"] == "res://player.gd"
    assert conns[0]["handler_fn"] == "_on_died"
    assert not unresolved_connections(conn)
    assert not any(u["called_name"] == "connect" for u in unresolved_calls(conn))


def test_connect_to_own_signal_takes_precedence_over_inherited_same_name(godot_project):
    """Regression test: a subclass that declares its OWN signal with the
    same name as an ancestor's must resolve `<name>.connect(...)` to its
    own declaration, not the inherited one."""
    godot_project.write("base.gd", "extends Node\nclass_name Base\nsignal died\n")
    godot_project.write("derived.gd", """
extends Base
class_name Derived
signal died

func _ready() -> void:
    died.connect(_on_died)

func _on_died() -> void:
    pass
""")
    conn = godot_project.build()
    conns = signal_connections(conn)
    assert len(conns) == 1
    assert conns[0]["signal_file"] == "res://derived.gd"


def test_connect_to_autoload_handler_resolves(godot_project):
    godot_project.write("project.godot", '[application]\n\n[autoload]\nGameState="*res://game_state.gd"\n')
    godot_project.write("game_state.gd", """
extends Node

func on_player_died() -> void:
    pass
""")
    godot_project.write("player.gd", """
extends Node
signal died

func setup() -> void:
    died.connect(GameState.on_player_died)
""")
    conn = godot_project.build()
    conns = signal_connections(conn)
    assert any(
        c["handler_file"] == "res://game_state.gd" and c["handler_fn"] == "on_player_died"
        for c in conns
    )


def test_connect_via_autoload_signal_receiver_resolves(godot_project):
    """Regression test: `GameManager.card_drawn.connect(handler)` -- a
    signal accessed through an autoload receiver, by far the most common
    real-world signal-wiring idiom -- must be recognized as a connection.
    Previously this whole shape was invisible: the chained receiver
    collapsed to the `<chained>` sentinel at extraction time, so it fell
    through to a generic unresolved call to `connect()`, never even
    reaching `unresolved_connections`."""
    godot_project.write("project.godot", '[application]\n\n[autoload]\nGameManager="*res://game_manager.gd"\n')
    godot_project.write("game_manager.gd", "extends Node\nsignal card_drawn(card)\n")
    godot_project.write("hand_ui.gd", """
extends Node

func _ready() -> void:
    GameManager.card_drawn.connect(_on_card_drawn)

func _on_card_drawn(card) -> void:
    pass
""")
    conn = godot_project.build()
    conns = signal_connections(conn)
    assert len(conns) == 1
    assert conns[0]["signal_file"] == "res://game_manager.gd"
    assert conns[0]["signal_name"] == "card_drawn"
    assert conns[0]["handler_fn"] == "_on_card_drawn"
    assert not unresolved_connections(conn)
    assert not any(u["called_name"] == "connect" for u in unresolved_calls(conn))


def test_connect_via_class_name_typed_signal_receiver_walks_inheritance_chain(godot_project):
    """Regression test: `<typed_local>.signal_name.connect(handler)` must
    resolve via the local's declared class_name, walking that class's
    inheritance chain for the signal (top-level only) the same way a
    chained method-call receiver would."""
    godot_project.write("base_unit.gd", "extends Node\nclass_name BaseUnit\nsignal died\n")
    godot_project.write("unit.gd", "extends BaseUnit\nclass_name Unit\nfunc noop():\n    pass\n")
    godot_project.write("main.gd", """
extends Node

func on_typed_local(u: Unit) -> void:
    u.died.connect(_on_unit_died)

func _on_unit_died() -> void:
    pass
""")
    conn = godot_project.build()
    conns = signal_connections(conn)
    assert len(conns) == 1
    assert conns[0]["signal_file"] == "res://base_unit.gd"
    assert conns[0]["signal_scope"] is None
    assert conns[0]["handler_fn"] == "_on_unit_died"


def test_connect_via_unrecognized_signal_receiver_stays_unresolved(godot_project):
    """Regression test: `<unrecognized>.signal_name.connect(handler)` (not
    a local/autoload/class_name) must land in `unresolved_connections`
    with `unknown_receiver`, not silently disappear or crash."""
    godot_project.write("main.gd", """
extends Node

func on_unknown_receiver() -> void:
    bogus_thing.some_signal.connect(_on_died)

func _on_died() -> void:
    pass
""")
    conn = godot_project.build()
    assert not signal_connections(conn)
    unresolved = unresolved_connections(conn)
    assert len(unresolved) == 1
    assert unresolved[0]["signal_receiver"] == "bogus_thing"
    assert unresolved[0]["signal_name"] == "some_signal"
    assert unresolved[0]["reason"] == "unknown_receiver"


def test_connect_via_recognized_receiver_with_nonexistent_signal_stays_unresolved(godot_project):
    """Regression test: `<typed_local>.no_such_signal.connect(handler)`
    where the receiver resolves fine but its class (and its whole
    inheritance chain) never declares that signal must land in
    `unresolved_connections` with `method_not_found_in_target`, distinct
    from an unrecognized receiver."""
    godot_project.write("unit.gd", "extends Node\nclass_name Unit\nsignal died\n")
    godot_project.write("main.gd", """
extends Node

func on_wrong_signal_name(u: Unit) -> void:
    u.no_such_signal.connect(_on_died)

func _on_died() -> void:
    pass
""")
    conn = godot_project.build()
    assert not signal_connections(conn)
    unresolved = unresolved_connections(conn)
    assert len(unresolved) == 1
    assert unresolved[0]["reason"] == "method_not_found_in_target"


def test_connect_via_signal_receiver_shadowed_inside_lambda_stays_unresolved(godot_project):
    """Regression test: a `<receiver>.signal_name.connect(...)` made
    *inside* a lambda, through a receiver name the lambda re-declares with
    a different type than the enclosing function's same-named var, must
    not resolve using the enclosing function's stale type -- same rule as
    the handler-side lambda-shadow guard (see
    test_connect_inside_lambda_with_shadowed_var_stays_honestly_unresolved)."""
    godot_project.write("unit.gd", "extends Node\nclass_name Unit\nsignal died\n")
    godot_project.write("main.gd", """
extends Node

func on_lambda_shadow(u: Unit) -> void:
    var cb = func():
        var u: Node = Node.new()
        u.died.connect(_on_died)
    cb.call()

func _on_died() -> void:
    pass
""")
    conn = godot_project.build()
    assert not signal_connections(conn)
    unresolved = unresolved_connections(conn)
    assert len(unresolved) == 1
    assert unresolved[0]["signal_receiver"] == "u"
    assert unresolved[0]["reason"] == "unknown_receiver"
