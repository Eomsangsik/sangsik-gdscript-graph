from __future__ import annotations

from gdscript_graph import db as gdb
from helpers import scene_connections, signal_connections, unresolved_calls, unresolved_scene_connections

PAUSE_MENU_GD = """
class_name PauseMenu
extends Control

func close() -> void:
    pass

func _on_resume_button_pressed() -> void:
    close()
"""

PAUSE_MENU_TSCN = """[gd_scene format=3 uid="uid://mqs27wy1rtxk"]

[ext_resource type="Script" uid="uid://hiv0htsf7jd7" path="res://gui/pause_menu.gd" id="2"]

[node name="PauseMenu" type="Control" unique_id=1424075319]
script = ExtResource("2")

[node name="ColorRect" type="ColorRect" parent="." unique_id=1798084660]

[node name="ResumeButton" type="Button" parent="ColorRect" unique_id=1637699098]

[connection signal="pressed" from="ColorRect/ResumeButton" to="." method="_on_resume_button_pressed"]
"""


def test_editor_connected_handler_has_a_scene_caller(godot_project):
    """Regression test (the feedback's case): a handler connected only in
    the editor -- a `[connection]` saved in the .tscn -- used to show no
    callers at all, so it looked like dead code."""
    godot_project.write("gui/pause_menu.gd", PAUSE_MENU_GD)
    godot_project.write("gui/pause_menu.tscn", PAUSE_MENU_TSCN)
    conn = godot_project.build()

    assert scene_connections(conn) == [{
        "scene": "res://gui/pause_menu.tscn", "signal_name": "pressed",
        "from_node": "ColorRect/ResumeButton", "to_node": ".",
        "signal_file": None,  # Button.pressed is an engine built-in signal
        "handler_file": "res://gui/pause_menu.gd", "handler_fn": "_on_resume_button_pressed",
    }]
    assert gdb.get_callers(conn, "_on_resume_button_pressed") == [{
        "caller_file": "res://gui/pause_menu.tscn", "caller_scope": None, "caller_function": None,
        "call_line": 12, "via": "scene", "signal": "pressed",
        "from_node": "ColorRect/ResumeButton", "to_node": ".",
    }]


def test_impact_reaches_the_scene_connection_through_the_handler(godot_project):
    godot_project.write("gui/pause_menu.gd", PAUSE_MENU_GD)
    godot_project.write("gui/pause_menu.tscn", PAUSE_MENU_TSCN)
    conn = godot_project.build()

    impact = gdb.get_callers_transitive(conn, "close")
    assert [(r["depth"], r.get("via"), r["res_path"], r["name"]) for r in impact] == [
        (1, None, "res://gui/pause_menu.gd", "_on_resume_button_pressed"),
        (2, "scene", "res://gui/pause_menu.tscn", None),
    ]
    assert impact[1]["method"] == "_on_resume_button_pressed"
    assert impact[1]["signal"] == "pressed"

    shallow = gdb.get_callers_transitive(conn, "close", max_depth=1)
    assert [r["name"] for r in shallow] == ["_on_resume_button_pressed"]


def test_connection_to_a_child_node_uses_that_nodes_script(godot_project):
    godot_project.write("main.gd", "extends Node\nfunc _on_timeout():\n    pass\n")
    godot_project.write("hud.gd", "extends Control\nfunc _on_timeout():\n    pass\n")
    godot_project.write("main.tscn", """[gd_scene format=3]

[ext_resource type="Script" path="res://main.gd" id="1_a"]
[ext_resource type="Script" path="res://hud.gd" id="2_b"]

[node name="Main" type="Node"]
script = ExtResource("1_a")

[node name="Timer" type="Timer" parent="."]

[node name="UI" type="CanvasLayer" parent="."]

[node name="Hud" type="Control" parent="UI"]
script = ExtResource("2_b")

[connection signal="timeout" from="Timer" to="UI/Hud" method="_on_timeout"]
""")
    conn = godot_project.build()
    assert [c["handler_file"] for c in scene_connections(conn)] == ["res://hud.gd"]


def test_handler_inherited_from_the_scripts_parent_resolves(godot_project):
    godot_project.write("base_menu.gd", "class_name BaseMenu\nextends Control\nfunc _on_close_pressed():\n    pass\n")
    godot_project.write("options.gd", "extends BaseMenu\n")
    godot_project.write("options.tscn", """[gd_scene format=3]

[ext_resource type="Script" path="res://options.gd" id="1"]

[node name="Options" type="Control"]
script = ExtResource("1")

[node name="Close" type="Button" parent="."]

[connection signal="pressed" from="Close" to="." method="_on_close_pressed"]
""")
    conn = godot_project.build()
    assert [c["handler_file"] for c in scene_connections(conn)] == ["res://base_menu.gd"]


def test_target_inside_instanced_and_inherited_scenes_resolves(godot_project):
    """A connection target that isn't declared in the scene itself comes
    from an instanced scene: an instanced child's own root (no script of
    its own here), a node inside an instanced child with editable
    children, or -- in an inherited scene -- a base-scene node this scene
    only overrides properties of."""
    godot_project.write("enemy.gd", "extends Node2D\nfunc _on_hit():\n    pass\n")
    godot_project.write("skeleton.gd", "extends Node2D\nfunc _on_hit():\n    pass\n")
    godot_project.write("enemy.tscn", """[gd_scene format=3]

[ext_resource type="Script" path="res://enemy.gd" id="1"]
[ext_resource type="Script" path="res://skeleton.gd" id="2"]

[node name="Enemy" type="Node2D"]
script = ExtResource("1")

[node name="Skeleton" type="Node2D" parent="."]
script = ExtResource("2")
""")
    godot_project.write("level.tscn", """[gd_scene format=3]

[ext_resource type="PackedScene" path="res://enemy.tscn" id="1"]

[node name="Level" type="Node2D"]

[node name="Area" type="Area2D" parent="."]

[node name="Enemy" parent="." instance=ExtResource("1")]

[node name="Skeleton" parent="Enemy" index="0"]
position = Vector2(1, 2)

[editable path="Enemy"]

[connection signal="body_entered" from="Area" to="Enemy" method="_on_hit"]
[connection signal="body_exited" from="Area" to="Enemy/Skeleton" method="_on_hit"]
""")
    godot_project.write("boss.tscn", """[gd_scene format=3]

[ext_resource type="PackedScene" path="res://enemy.tscn" id="1"]

[node name="Boss" instance=ExtResource("1")]

[node name="Skeleton" parent="." index="0"]
scale = Vector2(2, 2)

[connection signal="ready" from="." to="Skeleton" method="_on_hit"]
""")
    conn = godot_project.build()
    by_scene = {(c["scene"], c["to_node"]): c["handler_file"] for c in scene_connections(conn)}
    assert by_scene == {
        ("res://level.tscn", "Enemy"): "res://enemy.gd",
        ("res://level.tscn", "Enemy/Skeleton"): "res://skeleton.gd",
        ("res://boss.tscn", "Skeleton"): "res://skeleton.gd",
    }


def test_project_declared_signal_on_the_emitting_node_is_linked(godot_project):
    godot_project.write("player.gd", "extends CharacterBody2D\nsignal died\n")
    godot_project.write("main.gd", "extends Node\nfunc _on_player_died():\n    pass\n")
    godot_project.write("main.tscn", """[gd_scene format=3]

[ext_resource type="Script" path="res://main.gd" id="1"]
[ext_resource type="Script" path="res://player.gd" id="2"]

[node name="Main" type="Node"]
script = ExtResource("1")

[node name="Player" type="CharacterBody2D" parent="."]
script = ExtResource("2")

[connection signal="died" from="Player" to="." method="_on_player_died"]
""")
    conn = godot_project.build()
    assert [c["signal_file"] for c in scene_connections(conn)] == ["res://player.gd"]

    handlers = gdb.get_signal_handlers(conn, "died", res_path="res://player.gd")
    assert [(h["handler_function"], h.get("via"), h["connected_in"]) for h in handlers] == [
        ("_on_player_died", "scene", "res://main.tscn"),
    ]


def test_engine_signal_handlers_are_listed_by_name_without_filters(godot_project):
    godot_project.write("gui/pause_menu.gd", PAUSE_MENU_GD)
    godot_project.write("gui/pause_menu.tscn", PAUSE_MENU_TSCN)
    conn = godot_project.build()
    assert [h["handler_function"] for h in gdb.get_signal_handlers(conn, "pressed")] == ["_on_resume_button_pressed"]
    assert gdb.get_signal_handlers(conn, "pressed", res_path="res://gui/pause_menu.gd") == []


def test_unresolvable_scene_connections_are_reported_with_a_reason(godot_project):
    godot_project.write("main.gd", "extends Node\nfunc real():\n    pass\n")
    godot_project.write("main.tscn", """[gd_scene format=3]

[ext_resource type="Script" path="res://main.gd" id="1"]
[ext_resource type="Script" path="res://Main.cs" id="2"]

[sub_resource type="GDScript" id="GDScript_x"]
script/source = "extends Node
func _on_inline(): pass"

[node name="Main" type="Node"]
script = ExtResource("1")

[node name="Inline" type="Node" parent="."]
script = SubResource("GDScript_x")

[node name="CSharp" type="Node" parent="."]
script = ExtResource("2")

[node name="Plain" type="Button" parent="."]

[connection signal="ready" from="." to="." method="no_such_method"]
[connection signal="ready" from="." to="Inline" method="_on_inline"]
[connection signal="ready" from="." to="CSharp" method="OnReady"]
[connection signal="pressed" from="Plain" to="Plain" method="hide"]
""")
    conn = godot_project.build()
    assert not scene_connections(conn)
    assert sorted((u["to_node"], u["method"], u["reason"]) for u in unresolved_scene_connections(conn)) == [
        (".", "no_such_method", "method_not_found_in_target"),
        ("CSharp", "OnReady", "unknown_target_script"),
        ("Inline", "_on_inline", "unknown_target_script"),
        ("Plain", "hide", "unknown_target_script"),
    ]


def test_godot3_scene_format_resolves(godot_project):
    godot_project.write("main.gd", "extends Node\nfunc _on_button_pressed():\n    pass\n")
    godot_project.write("widget.gd", "extends Control\nfunc _on_button_pressed():\n    pass\n")
    godot_project.write("widget.tscn", """[gd_scene load_steps=2 format=2]

[ext_resource path="res://widget.gd" type="Script" id=1]

[node name="Widget" type="Control"]
script = ExtResource( 1 )
""")
    godot_project.write("main.tscn", """[gd_scene load_steps=3 format=2]

[ext_resource path="res://main.gd" type="Script" id=1]
[ext_resource path="res://widget.tscn" type="PackedScene" id=2]

[node name="Main" type="Node"]
script = ExtResource( 1 )

[node name="Button" type="Button" parent="."]

[node name="Widget" parent="." instance=ExtResource( 2 )]

[connection signal="pressed" from="Button" to="." method="_on_button_pressed"]
[connection signal="pressed" from="Button" to="Widget" method="_on_button_pressed"]
""")
    conn = godot_project.build()
    assert sorted(c["handler_file"] for c in scene_connections(conn)) == ["res://main.gd", "res://widget.gd"]


def test_scene_instancing_cycle_does_not_hang(godot_project):
    godot_project.write("a.tscn", """[gd_scene format=3]

[ext_resource type="PackedScene" path="res://b.tscn" id="1"]

[node name="A" instance=ExtResource("1")]

[connection signal="ready" from="." to="X" method="f"]
""")
    godot_project.write("b.tscn", """[gd_scene format=3]

[ext_resource type="PackedScene" path="res://a.tscn" id="1"]

[node name="B" instance=ExtResource("1")]
""")
    conn = godot_project.build()
    assert [u["reason"] for u in unresolved_scene_connections(conn)] == ["unknown_target_script"]


def test_code_connection_to_an_engine_signal_registers_the_handler(godot_project):
    """Regression test: `$Timer.timeout.connect(_on_timeout)` (a node
    expression), `timer.timeout.connect(...)` through an engine-typed
    field, and a bare engine signal `tree_exited.connect(...)` used to be
    dropped entirely -- the signal side can't be resolved to a project
    declaration -- so their handlers looked like dead code."""
    godot_project.write("main.gd", """
extends Node

@onready var timer: Timer = $Timer

func _ready() -> void:
    $AnimationPlayer.animation_finished.connect(_on_animation_finished)
    timer.timeout.connect(_on_timeout)
    tree_exited.connect(_on_exit)

func _on_animation_finished(_name: String) -> void:
    pass

func _on_timeout() -> void:
    pass

func _on_exit() -> void:
    pass
""")
    conn = godot_project.build()
    conns = sorted((c["signal_name"], c["signal_file"], c["handler_fn"]) for c in signal_connections(conn))
    assert conns == [
        ("animation_finished", None, "_on_animation_finished"),
        ("timeout", None, "_on_timeout"),
        ("tree_exited", None, "_on_exit"),
    ]
    assert not any(u["called_name"] == "connect" for u in unresolved_calls(conn))
    assert gdb.get_callers(conn, "_on_exit") == [{
        "caller_file": "res://main.gd", "caller_scope": None, "caller_function": "_ready",
        "call_line": 9, "via": "connect", "signal": "tree_exited",
    }]


def test_object_connect_with_a_signal_name_string_is_not_a_signal_connection(godot_project):
    """`Object.connect("signal", callable)` / Godot 3's `connect("signal",
    target, "method")` name the signal with a string -- not the
    `<signal>.connect(<callable>)` shape, so not mistaken for one."""
    godot_project.write("main.gd", """
extends Node

var hud

func _ready() -> void:
    $Button.connect("pressed", self, "_on_pressed")
    hud.button.connect("pressed", _on_pressed)
    $Store.connect(&"purchased", _on_pressed)

func _on_pressed() -> void:
    pass
""")
    conn = godot_project.build()
    assert not signal_connections(conn)
    assert len([u for u in unresolved_calls(conn) if u["called_name"] == "connect"]) == 3


def test_impact_follows_connect_edges_to_the_function_that_wires_the_handler(godot_project):
    godot_project.write("main.gd", """
extends Node

func _ready() -> void:
    setup()

func setup() -> void:
    $Timer.timeout.connect(_on_timeout)

func _on_timeout() -> void:
    pass
""")
    conn = godot_project.build()
    impact = gdb.get_callers_transitive(conn, "_on_timeout")
    assert [(r["depth"], r.get("via"), r["name"]) for r in impact] == [(1, "connect", "setup"), (2, None, "_ready")]
    callees = gdb.get_callees(conn, "setup")
    assert [(c["callee_function"], c["via"], c["signal"]) for c in callees] == [("_on_timeout", "connect", "timeout")]
