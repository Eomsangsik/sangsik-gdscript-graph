from __future__ import annotations

import subprocess
import sys

from helpers import resolved_calls, unresolved_calls


def test_same_file_bare_and_self_calls_resolve(godot_project):
    godot_project.write("player.gd", """
extends Node

func take_damage(amount: int) -> void:
    die()
    self.log_hit()

func die() -> void:
    pass

func log_hit() -> void:
    pass
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    targets = {(c["src_fn"], c["tgt_fn"]) for c in calls}
    assert ("take_damage", "die") in targets
    assert ("take_damage", "log_hit") in targets


def test_autoload_call_resolves(godot_project):
    godot_project.write("project.godot", '[application]\n\n[autoload]\nGameState="*res://game_state.gd"\n')
    godot_project.write("game_state.gd", """
extends Node

func add_score(amount: int) -> void:
    pass
""")
    godot_project.write("player.gd", """
extends Node

func take_damage() -> void:
    GameState.add_score(1)
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    assert any(
        c["src_fn"] == "take_damage" and c["tgt_fn"] == "add_score" and c["tgt_file"] == "res://game_state.gd"
        for c in calls
    )


def test_scene_based_autoload_call_resolves(godot_project):
    """Regression test: an autoload registered as a .tscn (common when the
    singleton needs child nodes) must resolve through to the script
    attached to the scene's root node, not stay permanently unresolved."""
    godot_project.write("project.godot", '[application]\n\n[autoload]\nGameManager="*res://game_manager.tscn"\n')
    godot_project.write("game_manager.gd", """
extends Node
class_name GameManagerScript

func add_score(amount: int) -> void:
    pass
""")
    godot_project.write(
        "game_manager.tscn",
        '[gd_scene load_steps=2 format=3]\n'
        '[ext_resource type="Script" path="res://game_manager.gd" id="1"]\n'
        '[node name="GameManager" type="Node"]\n'
        'script = ExtResource("1")\n',
    )
    godot_project.write("player.gd", """
extends Node

func take_damage() -> void:
    GameManager.add_score(1)
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    assert any(
        c["src_fn"] == "take_damage" and c["tgt_fn"] == "add_score" and c["tgt_file"] == "res://game_manager.gd"
        for c in calls
    )


def test_instanced_sub_scene_autoload_call_resolves(godot_project):
    """Regression test: an autoload registered as a .tscn whose root node
    has no script of its own but *instances* another scene (a common way
    to compose a reusable base scene as a singleton) must resolve through
    to the sub-scene's own root script, not stay permanently unresolved."""
    godot_project.write("project.godot", '[application]\n\n[autoload]\nPlayer="*res://player_wrapper.tscn"\n')
    godot_project.write(
        "player_wrapper.tscn",
        '[gd_scene load_steps=2 format=3]\n'
        '[ext_resource type="PackedScene" path="res://player_base.tscn" id="1"]\n'
        '[node name="Player" type="Node" instance=ExtResource("1")]\n',
    )
    godot_project.write(
        "player_base.tscn",
        '[gd_scene load_steps=2 format=3]\n'
        '[ext_resource type="Script" path="res://player.gd" id="1"]\n'
        '[node name="PlayerBase" type="Node2D"]\n'
        'script = ExtResource("1")\n',
    )
    godot_project.write("player.gd", "extends Node2D\nfunc heal(amount: int) -> void:\n    pass\n")
    godot_project.write("hud.gd", """
extends Node

func on_heal_pressed() -> void:
    Player.heal(5)
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    assert any(
        c["src_fn"] == "on_heal_pressed" and c["tgt_fn"] == "heal" and c["tgt_file"] == "res://player.gd"
        for c in calls
    )


def _run_build_with_hard_timeout(project_root, timeout_s=10):
    """Run `gdscript-graph build` as a real subprocess with an OS-level
    timeout, instead of calling `build_database` in-process with only a
    wall-clock assertion afterward -- if a regression ever reintroduces
    genuinely exponential (not just quadratic) backtracking, an in-process
    call would never return at all, hanging this test (and the whole
    suite/CI run) forever rather than failing fast. `subprocess.run`'s
    `timeout` kills the child process and raises `TimeoutExpired`, which
    pytest reports as a normal (bounded) test failure."""
    return subprocess.run(
        [sys.executable, "-m", "gdscript_graph.cli", "build", str(project_root)],
        capture_output=True, text=True, timeout=timeout_s,
    )


def test_tscn_section_header_regex_does_not_hang_on_unclosed_bracket(godot_project):
    """Regression test: the original `.tscn` section-header parsing used a
    single regex (`\\[(\\w+)((?:\\s+\\w+=(?:"[^"]*"|[^\\s\\]]+))*)\\s*\\]`)
    whose repeated group had no anchor to stop backtracking once a line
    starts with `[` but the terminating `]` is missing (e.g. a truncated/
    corrupted .tscn save) -- catastrophic backtracking that hung the whole
    build indefinitely, with no exception to catch. Fixed by checking for
    the closing `]` with a plain string operation before running any regex
    over the attribute run."""
    godot_project.write("project.godot", '[application]\n\n[autoload]\nBad="*res://bad.tscn"\n')
    unclosed = "[node " + " ".join(f'a{i}="v{i}"' for i in range(60))
    godot_project.write("bad.tscn", unclosed + "\n")
    godot_project.write("dummy.gd", "extends Node\nfunc a():\n    pass\n")

    result = _run_build_with_hard_timeout(godot_project.root)
    assert result.returncode == 0, result.stderr


def test_ext_resource_ref_regex_does_not_hang_on_crafted_input(godot_project):
    """Regression test: `_EXT_RESOURCE_REF_RE`'s value character class used
    to exclude only '"'/')'/whitespace, not '(' -- so a string containing
    many repeated "ExtResource(" substrings with no closing ")" backtracked
    the "+" across the entire remaining string at every occurrence, an
    O(n^2) blowup. Unlike the .tscn section-header ReDoS this mirrors, no
    malformed/truncated file is needed: a real, fully well-formed, CLOSED
    `[node ... instance="..."]` line whose `instance` attribute value just
    happens to contain that repeated substring (e.g. from a bad merge or a
    hand-edited default value) reaches this regex through the ordinary
    public API and could hang a build for minutes on a large enough
    payload."""
    godot_project.write("project.godot", '[application]\n\n[autoload]\nPlayer="*res://wrapper.tscn"\n')
    payload = "ExtResource(" * 3000
    godot_project.write(
        "wrapper.tscn",
        '[gd_scene load_steps=2 format=3]\n'
        f'[node name="Player" type="Node" instance="{payload}"]\n',
    )
    godot_project.write("dummy.gd", "extends Node\nfunc a():\n    pass\n")

    result = _run_build_with_hard_timeout(godot_project.root)
    assert result.returncode == 0, result.stderr


def test_unreadable_scene_autoload_does_not_crash_build(godot_project):
    """Regression test: a non-UTF-8 or otherwise unreadable .tscn autoload
    target must degrade gracefully (autoload stays unresolved) rather than
    crashing the whole build."""
    godot_project.write("project.godot", '[application]\n\n[autoload]\nBad="*res://bad.tscn"\n')
    (godot_project.root / "bad.tscn").write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")
    godot_project.write("player.gd", """
extends Node

func take_damage() -> void:
    Bad.whatever()
""")
    conn = godot_project.build()
    assert any(u["called_name"] == "whatever" for u in unresolved_calls(conn))


def test_class_name_static_call_resolves(godot_project):
    godot_project.write("player.gd", """
class_name Player
extends Node

static func spawn() -> void:
    pass
""")
    godot_project.write("world.gd", """
extends Node

func setup() -> void:
    Player.spawn()
""")
    conn = godot_project.build()
    calls = resolved_calls(conn)
    assert any(
        c["src_fn"] == "setup" and c["tgt_fn"] == "spawn" and c["tgt_file"] == "res://player.gd"
        for c in calls
    )


def test_untyped_receiver_stays_unresolved(godot_project):
    godot_project.write("player.gd", """
class_name Player
extends Node

func heal() -> void:
    pass
""")
    godot_project.write("enemy.gd", """
extends Node

func attack(p) -> void:
    p.heal()
""")
    conn = godot_project.build()
    unresolved = unresolved_calls(conn)
    assert any(u["called_name"] == "heal" and u["reason"] == "unknown_receiver" for u in unresolved)


def test_get_set_calls_are_captured(godot_project):
    godot_project.write("data.gd", """
extends Node

var data: Dictionary = {}

func read_it() -> void:
    var v = data.get("key")
    data.set("key", 5)
""")
    conn = godot_project.build()
    called_names = {u["called_name"] for u in unresolved_calls(conn)}
    assert {"get", "set"} <= called_names
