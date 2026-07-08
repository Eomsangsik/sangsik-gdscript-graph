from __future__ import annotations

from helpers import resolved_calls, unresolved_calls


def test_typed_local_var_call_resolves(godot_project):
    godot_project.write("player.gd", """
class_name Player
extends Node

func take_damage() -> void:
    pass
""")
    godot_project.write("enemy.gd", """
extends Node

func attack() -> void:
    var target: Player = get_target()
    target.take_damage()

func get_target():
    pass
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    assert any(
        c["src_fn"] == "attack" and c["tgt_fn"] == "take_damage" and c["tgt_file"] == "res://player.gd"
        for c in calls
    )


def test_typed_param_call_resolves_through_inheritance(godot_project):
    godot_project.write("base.gd", """
class_name Base
extends Node

func heal() -> void:
    pass
""")
    godot_project.write("player.gd", """
class_name Player
extends Base
""")
    godot_project.write("world.gd", """
extends Node

func on_hit(target: Player) -> void:
    target.heal()
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    assert any(
        c["src_fn"] == "on_hit" and c["tgt_fn"] == "heal" and c["tgt_file"] == "res://base.gd"
        for c in calls
    )


def test_lambda_local_var_type_does_not_clobber_enclosing_function(godot_project):
    """Regression test: a typed local var declared inside a lambda must not
    overwrite a same-named, differently-typed var in the enclosing function."""
    godot_project.write("enemy.gd", "class_name Enemy\nextends Node\nfunc enemy_only():\n    pass\n")
    godot_project.write("player.gd", "class_name Player\nextends Node\nfunc player_only():\n    pass\n")
    godot_project.write("handler.gd", """
extends Node

func get_enemy():
    pass

func get_other():
    pass

func handle() -> void:
    var target: Enemy = get_enemy()
    var cb = func():
        var target: Player = get_other()
        target.player_only()
    target.enemy_only()
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    assert any(
        c["src_fn"] == "handle" and c["tgt_fn"] == "enemy_only" and c["tgt_file"] == "res://enemy.gd"
        for c in calls
    )
    assert not any(c["tgt_fn"] == "player_only" for c in calls)


def test_lambda_shadowed_var_call_stays_honestly_unresolved(godot_project):
    """Regression test: a call made *inside* a lambda, on a name the lambda
    re-declares with a different type than the enclosing function's
    same-named var, must not be resolved using the enclosing function's
    (wrong, for this call site) type -- it should honestly fall through as
    unresolved instead of confidently reporting the wrong target as
    missing."""
    godot_project.write("foo.gd", "class_name Foo\nextends Node\nfunc do_foo():\n    pass\n")
    godot_project.write("bar.gd", "class_name Bar\nextends Node\nfunc do_bar():\n    pass\n")
    godot_project.write("main.gd", """
extends Node

func _ready() -> void:
    var x: Foo = Foo.new()
    var callback = func():
        var x: Bar = Bar.new()
        x.do_bar()
    callback.call()
    x.do_foo()
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    unresolved = unresolved_calls(conn)

    # The call outside the lambda still resolves correctly using the
    # enclosing function's own (unshadowed) type.
    assert any(c["src_fn"] == "_ready" and c["tgt_fn"] == "do_foo" for c in calls)

    # The call inside the lambda must not be wrongly resolved (or wrongly
    # reported as "missing" on the outer type) -- it's honestly unknown.
    assert not any(c["tgt_fn"] == "do_bar" for c in calls)
    shadowed = next(u for u in unresolved if u["receiver"] == "x" and u["called_name"] == "do_bar")
    assert shadowed["reason"] == "unknown_receiver"


def test_typed_local_var_shadows_same_named_class_name(godot_project):
    """Regression test: a local var whose name collides with a project-wide
    class_name must resolve via its own declared type, not the identically
    named global class_name -- GDScript resolves locals before globals."""
    godot_project.write("enemy.gd", "class_name Enemy\nextends Node\nfunc attack():\n    pass\n")
    godot_project.write("player.gd", "class_name Player\nextends Node\nfunc attack():\n    pass\n")
    godot_project.write("world.gd", """
extends Node

func setup() -> void:
    var Player: Enemy = Enemy.new()
    Player.attack()
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    match = next(c for c in calls if c["src_fn"] == "setup")
    assert match["tgt_file"] == "res://enemy.gd"


def test_typed_local_var_shadows_same_named_autoload(godot_project):
    """Regression test: a local var whose name collides with an autoload
    singleton name must resolve via its own declared type, not the
    autoload -- a local always shadows a same-named global."""
    godot_project.write("project.godot", '[application]\n\n[autoload]\nGlobal="*res://global.gd"\n')
    godot_project.write("global.gd", "extends Node\nfunc notify():\n    pass\n")
    godot_project.write("local_thing.gd", "class_name Local\nextends Node\nfunc notify():\n    pass\n")
    godot_project.write("player.gd", """
extends Node

func do_thing() -> void:
    var Global: Local = Local.new()
    Global.notify()
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    match = next(c for c in calls if c["src_fn"] == "do_thing")
    assert match["tgt_file"] == "res://local_thing.gd"


def test_untyped_local_var_stays_unresolved(godot_project):
    godot_project.write("player.gd", """
class_name Player
extends Node

func take_damage() -> void:
    pass
""")
    godot_project.write("enemy.gd", """
extends Node

func attack() -> void:
    var target = get_target()
    target.take_damage()

func get_target():
    pass
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    assert not any(c["tgt_fn"] == "take_damage" for c in calls)


def test_untyped_local_var_shadows_same_named_class_name_stays_unresolved(godot_project):
    """Regression test: an *untyped* local var whose name collides with a
    class_name still shadows it -- a local always shadows a same-named
    global regardless of whether we know its type. Since we don't know
    its actual type here, the call must be left honestly unresolved
    rather than silently matching the class_name of the same name."""
    godot_project.write("player.gd", "class_name Player\nextends Node\nfunc take_damage():\n    pass\n")
    godot_project.write("enemy.gd", """
extends Node

func attack() -> void:
    var Player = get_target()
    Player.take_damage()

func get_target():
    pass
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    assert not any(c["tgt_fn"] == "take_damage" for c in calls)
    shadowed = next(
        u for u in unresolved_calls(conn) if u["receiver"] == "Player" and u["called_name"] == "take_damage"
    )
    assert shadowed["reason"] == "unknown_receiver"


def test_for_loop_var_shadows_same_named_class_name_stays_unresolved(godot_project):
    """Regression test: a for-loop iteration variable whose name collides
    with a class_name still shadows it, same as any other local -- must
    not be silently resolved as if every loop element were that
    class_name."""
    godot_project.write("player.gd", "class_name Player\nextends Node\nfunc take_damage():\n    pass\n")
    godot_project.write("enemy.gd", """
extends Node

func process_list(items: Array) -> void:
    for Player in items:
        Player.take_damage()
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    assert not any(c["tgt_fn"] == "take_damage" for c in calls)
    shadowed = next(
        u for u in unresolved_calls(conn) if u["receiver"] == "Player" and u["called_name"] == "take_damage"
    )
    assert shadowed["reason"] == "unknown_receiver"


def test_local_const_shadows_same_named_autoload_stays_unresolved(godot_project):
    """Regression test: a local `const` declaration always shadows a
    same-named autoload/class_name too, same as `var` -- must be left
    unresolved rather than silently matching the global of that name."""
    godot_project.write("project.godot", '[application]\n\n[autoload]\nGlobal="*res://global.gd"\n')
    godot_project.write("global.gd", "extends Node\nfunc notify():\n    pass\n")
    godot_project.write("player.gd", """
extends Node

func do_thing() -> void:
    const Global = 5
    Global.notify()
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    assert not any(c["tgt_fn"] == "notify" for c in calls)
    shadowed = next(
        u for u in unresolved_calls(conn) if u["receiver"] == "Global" and u["called_name"] == "notify"
    )
    assert shadowed["reason"] == "unknown_receiver"


def test_nested_lambda_param_in_header_does_not_leak_into_outer_lambda_shadow_set(godot_project):
    """Regression test: a lambda embedded in *another lambda's* own
    default-argument-value expression has its own separate parameter list
    -- that name must not be recorded as something the OUTER lambda
    re-declares, or a real call inside the outer lambda that legitimately
    uses the enclosing function's local var type gets falsely treated as
    shadowed."""
    godot_project.write("foo.gd", "class_name Foo\nextends Node\nfunc real_method():\n    pass\n")
    godot_project.write("real_target.gd", "class_name RealTarget\nextends Node\nfunc real_method():\n    pass\n")
    godot_project.write("main.gd", """
extends Node

func do_work() -> void:
    var Foo: RealTarget = RealTarget.new()
    var f = func(x = (func(Foo): return Foo).call(1)):
        Foo.real_method()
    f.call()
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    assert any(c["tgt_fn"] == "real_method" and c["tgt_file"] == "res://real_target.gd" for c in calls)
    assert not any(c["tgt_file"] == "res://foo.gd" for c in calls)


def test_lambda_in_default_arg_value_shadowing_a_header_param_stays_unresolved(godot_project):
    """Regression test: a lambda embedded in a default-argument-value
    expression that re-declares one of the ENCLOSING function's own
    parameter names (not just another lambda's own param, see the test
    above) must be recognized as shadowing it -- the call inside the
    lambda must not silently use the outer parameter's stale type."""
    godot_project.write("a.gd", "class_name A\nextends Node\nfunc method_a():\n    pass\n")
    godot_project.write("b.gd", "class_name B\nextends Node\nfunc method_a():\n    pass\n")
    godot_project.write("main.gd", """
extends Node

func run(v: A, cb = func(): var v = B.new(); v.method_a()) -> void:
    pass
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    assert not any(c["tgt_fn"] == "method_a" for c in calls)
    shadowed = next(
        u for u in unresolved_calls(conn) if u["receiver"] == "v" and u["called_name"] == "method_a"
    )
    assert shadowed["reason"] == "unknown_receiver"


def test_lambda_param_in_default_arg_value_does_not_leak_into_enclosing_header(godot_project):
    """Regression test: a lambda embedded in a default-argument-value
    expression (`func f(x = call(func(Y): ...)):`) has its own separate
    parameter list -- that parameter name must not leak into the
    enclosing function's own header-arg type map and falsely shadow a
    same-named, otherwise-resolvable class_name."""
    godot_project.write("foo.gd", "class_name Foo\nextends Node\nstatic func bar():\n    return 1\n")
    godot_project.write("main.gd", """
extends Node

func outer(x = call_with_lambda(func(Foo): return Foo)) -> void:
    Foo.bar()

func call_with_lambda(f):
    return f.call(1)
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    assert any(c["src_fn"] == "outer" and c["tgt_fn"] == "bar" and c["tgt_file"] == "res://foo.gd" for c in calls)


def test_lambda_local_for_loop_var_shadowed_call_stays_honestly_unresolved(godot_project):
    """Regression test: a `for x in ...:` loop variable declared *inside a
    lambda* must be recognized as shadowing an enclosing-function `x` of a
    different type, same as a lambda-local `var`/`const` (see
    test_lambda_shadowed_var_call_stays_honestly_unresolved) -- the call
    inside the lambda must not silently use the outer function's stale
    type for `x`."""
    godot_project.write("foo.gd", "class_name Foo\nextends Node\nfunc do_foo():\n    pass\nfunc do_bar():\n    pass\n")
    godot_project.write("main.gd", """
extends Node

func _ready() -> void:
    var x: Foo = Foo.new()
    var items = [1, 2, 3]
    var callback = func():
        for x in items:
            x.do_bar()
    callback.call()
    x.do_foo()
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    assert any(c["src_fn"] == "_ready" and c["tgt_fn"] == "do_foo" for c in calls)
    assert not any(c["tgt_fn"] == "do_bar" for c in calls)
    shadowed = next(
        u for u in unresolved_calls(conn) if u["receiver"] == "x" and u["called_name"] == "do_bar"
    )
    assert shadowed["reason"] == "unknown_receiver"


def test_typed_and_inferred_local_const_shadow_same_named_autoload_stays_unresolved(godot_project):
    """Regression test: `const_stmt` has three sibling grammar forms
    (`const X = v`, `const X: T = v`, `const X := v`) -- all three must
    shadow a same-named autoload/class_name, not just the untyped one."""
    godot_project.write("project.godot", '[application]\n\n[autoload]\nGlobal="*res://global.gd"\n')
    godot_project.write("global.gd", "extends Node\nfunc notify():\n    pass\n")
    godot_project.write("player.gd", """
extends Node

func do_typed() -> void:
    const Global: int = 5
    Global.notify()

func do_inf() -> void:
    const Global := 5
    Global.notify()
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    assert not any(c["tgt_fn"] == "notify" for c in calls)
    reasons = {
        u["source_function"]: u["reason"]
        for u in unresolved_calls(conn) if u["receiver"] == "Global" and u["called_name"] == "notify"
    }
    assert reasons == {"do_typed": "unknown_receiver", "do_inf": "unknown_receiver"}


def test_lambda_local_typed_const_shadowed_call_stays_honestly_unresolved(godot_project):
    """Regression test: a `const x: T = v` declared *inside* a lambda must
    be recognized as shadowing an enclosing-function `x` of a different
    type, same as a lambda-local `var` (see
    test_lambda_shadowed_var_call_stays_honestly_unresolved) -- the call
    inside the lambda must not silently use the outer function's stale
    type for `x`."""
    godot_project.write("foo.gd", "class_name Foo\nextends Node\nfunc do_foo():\n    pass\n")
    godot_project.write("bar.gd", "class_name Bar\nextends Node\nfunc do_bar():\n    pass\n")
    godot_project.write("main.gd", """
extends Node

func _ready() -> void:
    var x: Foo = Foo.new()
    var callback = func():
        const x: Bar = Bar.new()
        x.do_bar()
    callback.call()
    x.do_foo()
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    assert any(c["src_fn"] == "_ready" and c["tgt_fn"] == "do_foo" for c in calls)
    assert not any(c["tgt_fn"] == "do_bar" for c in calls)
    shadowed = next(
        u for u in unresolved_calls(conn) if u["receiver"] == "x" and u["called_name"] == "do_bar"
    )
    assert shadowed["reason"] == "unknown_receiver"


def test_match_capture_var_shadows_same_named_autoload_stays_unresolved(godot_project):
    """Regression test: a `match` pattern's `var X:` capture binding is a
    local too -- must shadow a same-named autoload/class_name rather than
    silently matching the global of that name."""
    godot_project.write("project.godot", '[application]\n\n[autoload]\nGlobal="*res://global.gd"\n')
    godot_project.write("global.gd", "extends Node\nfunc notify():\n    pass\n")
    godot_project.write("player.gd", """
extends Node

func do_match(x) -> void:
    match x:
        var Global:
            Global.notify()
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    assert not any(c["tgt_fn"] == "notify" for c in calls)
    shadowed = next(
        u for u in unresolved_calls(conn) if u["receiver"] == "Global" and u["called_name"] == "notify"
    )
    assert shadowed["reason"] == "unknown_receiver"


def test_untyped_local_var_shadows_same_named_autoload_stays_unresolved(godot_project):
    """Regression test: same as the class_name case, but for an untyped
    local var whose name collides with an autoload singleton name."""
    godot_project.write("project.godot", '[application]\n\n[autoload]\nGlobal="*res://global.gd"\n')
    godot_project.write("global.gd", "extends Node\nfunc notify():\n    pass\n")
    godot_project.write("player.gd", """
extends Node

func do_thing() -> void:
    var Global = get_node(".")
    Global.notify()
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    assert not any(c["tgt_fn"] == "notify" for c in calls)
    shadowed = next(
        u for u in unresolved_calls(conn) if u["receiver"] == "Global" and u["called_name"] == "notify"
    )
    assert shadowed["reason"] == "unknown_receiver"
