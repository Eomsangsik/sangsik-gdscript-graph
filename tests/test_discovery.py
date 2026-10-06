from __future__ import annotations

import os

from gdscript_graph.discovery import discover, find_project_files


def _res_paths(project) -> list[str]:
    files = discover(project.root)
    return [files.to_res_path(f) for f in files.gd_files + files.scene_files]


def test_discovery_skips_what_godot_skips(godot_project):
    """Regression test: discovery walked `.git`/`.godot` (over 1 GB in one
    project, most of every rebuild's discovery time) and indexed scripts
    Godot itself never loads."""
    godot_project.write("main.gd", "extends Node\n")
    godot_project.write("main.tscn", "[gd_scene format=3]\n")
    godot_project.write(".godot/editor/cached.gd", "extends Node\n")
    godot_project.write(".git/hooks/x.gd", "extends Node\n")
    godot_project.write("ui/.hidden.gd", "extends Node\n")
    godot_project.write("tools/raw/.gdignore", "")
    godot_project.write("tools/raw/skip.gd", "extends Node\n")
    godot_project.write("tools/raw/deeper/skip_too.gd", "extends Node\n")
    godot_project.write("tools/keep.gd", "extends Node\n")
    godot_project.write("vendor/other_project/project.godot", "[application]\n")
    godot_project.write("vendor/other_project/theirs.gd", "extends Node\n")

    assert _res_paths(godot_project) == ["res://main.gd", "res://tools/keep.gd", "res://main.tscn"]


def test_symlinked_addon_is_indexed_under_its_godot_path(godot_project):
    """Regression test: an addon symlinked in from a hidden vendored
    checkout (`addons/gdUnit4 -> ../.plugged/gdUnit4/addons/gdUnit4`) was
    indexed under its real `res://.plugged/...` location -- 22% of one
    project's index, under a path no `res://addons/` filter matched."""
    godot_project.write(".plugged/gdUnit4/addons/gdUnit4/src/assert.gd", "class_name GdAssert\nextends Node\n")
    (godot_project.root / "addons").mkdir()
    os.symlink("../.plugged/gdUnit4/addons/gdUnit4", godot_project.root / "addons" / "gdUnit4")

    assert _res_paths(godot_project) == ["res://addons/gdUnit4/src/assert.gd"]


def test_symlink_cycle_does_not_hang_or_duplicate(godot_project):
    godot_project.write("a/x.gd", "extends Node\n")
    os.symlink("..", godot_project.root / "a" / "loop")

    assert [p.name for p in find_project_files(godot_project.root, (".gd",))] == ["x.gd"]
