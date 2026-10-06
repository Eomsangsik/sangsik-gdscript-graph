from __future__ import annotations

from helpers import resolved_calls, unresolved_calls

REPO = """class_name LootRepo
extends RefCounted

class Page:
    func rows() -> Array:
        return []

static func open() -> LootRepo:
    return LootRepo.new()

func box_grade_table_acts() -> void:
    pass

func page() -> Page:
    return Page.new()

func orchestrator() -> Orchestrator:
    return Orchestrator.new()
"""

ORCH = """class_name Orchestrator
extends RefCounted

func calibrate_curve() -> void:
    pass
"""


def _targets(conn, src_fn: str) -> set[tuple[str, str | None, str]]:
    return {(c["tgt_file"], c["tgt_scope"], c["tgt_fn"]) for c in resolved_calls(conn) if c["src_fn"] == src_fn}


def test_calls_through_a_variable_typed_by_a_call_result_resolve(godot_project):
    """Regression test: `var x := f()` (about 8,000 receivers in one
    project) left `x` untyped, so every call through it -- most of a
    port/repository architecture's calls -- was unresolved. The variable
    has the called function's declared `-> T` return type."""
    godot_project.write("loot_repo.gd", REPO)
    godot_project.write("orchestrator.gd", ORCH)
    godot_project.write("base_screen.gd", "class_name BaseScreen\nextends Node\nfunc repo() -> LootRepo:\n    return LootRepo.open()\n")
    godot_project.write("screen.gd", """extends BaseScreen

var _repo := LootRepo.open()
var _orch := _make_orch()

func _make_orch() -> Orchestrator:
    return Orchestrator.new()

func static_factory() -> void:
    var repo := LootRepo.open()
    repo.box_grade_table_acts()

func inherited_bare_call() -> void:
    var r := repo()
    r.box_grade_table_acts()

func through_fields() -> void:
    _repo.box_grade_table_acts()
    _orch.calibrate_curve()

func awaited() -> void:
    var r := await self.repo()
    r.box_grade_table_acts()

func chained() -> void:
    var r := LootRepo.open()
    var o := r.orchestrator()
    o.calibrate_curve()

func inner_class_return() -> void:
    var p := _repo.page()
    p.rows()
""")
    conn = godot_project.build()
    box = ("res://loot_repo.gd", None, "box_grade_table_acts")
    calibrate = ("res://orchestrator.gd", None, "calibrate_curve")
    assert box in _targets(conn, "static_factory")
    assert box in _targets(conn, "inherited_bare_call")
    assert {box, calibrate} <= _targets(conn, "through_fields")
    assert box in _targets(conn, "awaited")
    assert calibrate in _targets(conn, "chained")
    assert ("res://loot_repo.gd", "Page", "rows") in _targets(conn, "inner_class_return")


def test_call_results_without_a_project_return_type_stay_unresolved(godot_project):
    godot_project.write("x.gd", """extends Node

var loop := loop.next()

func untyped():
    return null

func engine() -> Node:
    return self

func run() -> void:
    var a := untyped()
    a.go()
    var b := engine()
    b.go()
    var c := missing()
    c.go()
    loop.go()
""")
    conn = godot_project.build()
    assert {t[2] for t in _targets(conn, "run")} == {"untyped", "engine"}
    unresolved_go = [u for u in unresolved_calls(conn) if u["called_name"] == "go"]
    assert len(unresolved_go) == 4


def test_calls_on_a_call_result_resolve_through_its_return_type(godot_project):
    """Regression test: a call made directly on another call's result --
    `Repo.open().box()`, gdUnit's `assert_bool(x).is_true()`,
    `get_tree().create_timer(1)` -- was the largest group of unresolved
    calls left (11,000 in one project): its receiver was never typed."""
    godot_project.write("loot_repo.gd", REPO)
    godot_project.write("orchestrator.gd", ORCH)
    godot_project.write("screen.gd", """extends Node

func repo() -> LootRepo:
    return LootRepo.open()

func run() -> void:
    LootRepo.open().box_grade_table_acts()
    repo().orchestrator().calibrate_curve()
    LootRepo.new().box_grade_table_acts()
    get_tree().create_timer(1.0).set_time_left(2.0)
    Node.new().add_child(self)
    untyped().go()

func untyped():
    return null
""")
    conn = godot_project.build()
    assert {
        ("res://loot_repo.gd", None, "box_grade_table_acts"),
        ("res://orchestrator.gd", None, "calibrate_curve"),
    } <= _targets(conn, "run")
    assert len([c for c in resolved_calls(conn) if c["src_fn"] == "run" and c["tgt_fn"] == "box_grade_table_acts"]) == 2
    reasons = {u["called_name"]: u["reason"] for u in unresolved_calls(conn) if u["source_function"] == "run"}
    assert reasons["set_time_left"] == "engine_receiver"
    assert reasons["add_child"] == "engine_receiver"
    assert reasons["go"] == "not_a_project_function"
