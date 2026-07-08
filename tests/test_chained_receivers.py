from __future__ import annotations

from helpers import resolved_calls, unresolved_calls


def test_multi_segment_attribute_chain_does_not_falsely_resolve(godot_project):
    """Regression test: `self.child.foo()` must not be misattributed to a
    same-named `foo()` declared in the caller's own file."""
    godot_project.write("node.gd", """
extends Node

func run() -> void:
    self.child.foo()

func foo() -> void:
    pass
""")
    conn = godot_project.build()
    assert not any(c["tgt_fn"] == "foo" for c in resolved_calls(conn))
    assert any(
        u["called_name"] == "foo" and u["receiver"] == "<chained>" and u["reason"] == "unknown_receiver"
        for u in unresolved_calls(conn)
    )


def test_call_on_call_result_does_not_falsely_resolve(godot_project):
    """Regression test: `get_node("X").update_value()` must not be
    misattributed to a same-named function in the caller's own scope."""
    godot_project.write("node.gd", """
extends Node

func run() -> void:
    get_node("HealthBar").update_value(5)

func update_value(x) -> void:
    pass
""")
    conn = godot_project.build()
    assert not any(c["tgt_fn"] == "update_value" for c in resolved_calls(conn))


def test_call_in_default_argument_value_is_tracked(godot_project):
    """Regression test: a call made inside a default-argument-value
    expression (`func f(x = some_call()):`) must not vanish from the
    graph entirely -- the function header used to be skipped outright when
    walking a function for call sites."""
    godot_project.write("node.gd", """
extends Node

func some_call():
    return 42

func f(x = some_call()) -> void:
    return

func g() -> void:
    f()
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    assert any(c["src_fn"] == "f" and c["tgt_fn"] == "some_call" for c in calls)
