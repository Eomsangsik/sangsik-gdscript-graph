from __future__ import annotations

import json

from helpers import resolved_calls, unresolved_calls
from test_mcp_server import _run_session


def test_calls_into_the_engine_are_told_apart_from_missed_project_calls(godot_project):
    """Regression test: every call that didn't resolve to project code was
    "unresolved" -- 90,817 of them in one project, nearly all `int()`,
    `str()`, `queue_free()`, `some_dict.has()`: a number that said nothing
    about what the graph actually missed. Calls into the engine get their
    own reasons, leaving `unknown_receiver`/`method_not_found_in_target`
    for the calls that may really be missed project calls."""
    godot_project.write("player.gd", """class_name Player
extends CharacterBody2D

var stats: Dictionary = {}
var label: Label
@onready var timer := $Timer as Timer

func make():
    return null

func _ready() -> void:
    var n := int("3")
    print(n)
    var v := Vector2(1, 2)
    queue_free()
    move_and_slide()
    stats.has("x")
    label.set_text("a")
    timer.start()
    var tree := get_tree()
    tree.create_timer(1.0)
    Input.is_action_pressed("x")
    Time.get_ticks_msec()
    v.normalized()
    var p := get_parent()
    p.add_child(self)
    mystery.frobnicate()
    var x = make()
    x.heal()
""")
    godot_project.write("other.gd", """class_name Other
extends Node

func heal() -> void:
    pass

func use(pl: Player) -> void:
    pl.queue_free()
    pl.fly()
    pl.free()
""")
    conn = godot_project.build()
    reasons = {(u["receiver"], u["called_name"]): u["reason"] for u in unresolved_calls(conn)}
    assert reasons == {
        (None, "int"): "builtin_function",
        (None, "print"): "builtin_function",
        (None, "Vector2"): "builtin_function",
        (None, "queue_free"): "engine_method",
        (None, "move_and_slide"): "engine_method",
        (None, "get_tree"): "engine_method",
        (None, "get_parent"): "engine_method",
        ("stats", "has"): "engine_receiver",
        ("label", "set_text"): "engine_receiver",
        ("timer", "start"): "engine_receiver",
        ("tree", "create_timer"): "engine_receiver",
        ("Input", "is_action_pressed"): "engine_receiver",
        ("Time", "get_ticks_msec"): "engine_receiver",
        ("v", "normalized"): "engine_receiver",
        ("p", "add_child"): "engine_receiver",
        ("mystery", "frobnicate"): "not_a_project_function",
        ("x", "heal"): "unknown_receiver",
        ("pl", "queue_free"): "engine_method",
        ("pl", "fly"): "method_not_found_in_target",
        ("pl", "free"): "engine_method",
    }

    db_path = godot_project.root.parent / "graph.db"
    result = _run_session(db_path, lambda session: session.call_tool("status", {}), ["--no-watch"])
    status = json.loads(result.content[0].text)
    assert (status["unresolved_calls"], status["engine_calls"]) == (2, 18)
    assert status["unresolved_calls_by_reason"]["engine_receiver"] == 8


def test_constructor_calls_link_to_the_init_they_run(godot_project):
    """Regression test: `T.new(...)` was recorded as a missing method `new`
    on T (thousands of them in one project), so `_init` looked uncalled --
    yet those are exactly the call sites a signature change breaks."""
    godot_project.write("base.gd", "class_name Base\nextends RefCounted\nfunc _init(a):\n    pass\n")
    godot_project.write("item.gd", """class_name Item
extends Base

const Plain = preload("res://plain.gd")

func _init(a, b):
    super(a)

static func make() -> Item:
    return Item.new(1, 2)

func copy():
    var p := Plain.new()
    var n := Node.new()
    return Base.new(1)
""")
    godot_project.write("plain.gd", "extends RefCounted\n")
    conn = godot_project.build()
    edges = {(c["src_fn"], c["tgt_file"], c["tgt_fn"]) for c in resolved_calls(conn)}
    assert {
        ("make", "res://item.gd", "_init"),
        ("_init", "res://base.gd", "_init"),
        ("copy", "res://base.gd", "_init"),
    } <= edges
    reasons = {(u["receiver"], u["called_name"]): u["reason"] for u in unresolved_calls(conn)}
    assert reasons == {("Plain", "new"): "builtin_function", ("Node", "new"): "builtin_function"}
