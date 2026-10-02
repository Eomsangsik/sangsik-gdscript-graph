"""Deterministically generates the benchmark fixture project used by
compare_mcp_vs_grep.py. Re-run this to regenerate benchmarks/fixture_project/
from scratch (it's committed so the benchmark is runnable without
regenerating, but this is the source of truth for its exact contents).

Each scenario's ground-truth call/reference counts are hardcoded into
compare_mcp_vs_grep.py to match what this generator produces -- if you
change this file, update the counts there too.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parent / "fixture_project"


def write(rel_path: str, content: str) -> None:
    path = ROOT / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _padding_methods(names: list[str]) -> str:
    """Plausible-looking filler methods, to make files a realistic size
    (a few hundred lines) instead of suspiciously minimal -- this is what
    makes the "avoid reading the whole file" comparison meaningful."""
    out = []
    for name in names:
        out.append(f"""
func {name}() -> void:
    var _local_a := 0
    var _local_b := "placeholder"
    for _i in range(3):
        _local_a += _i
    if _local_a > 100:
        _local_b = "large"
    Logger.log_debug("{name} called")
""")
    return "".join(out)


def generate() -> None:
    write("project.godot", """[application]
config/name="Benchmark Fixture"

[autoload]
GameManager="*res://autoloads/game_manager.gd"
EquipmentManager="*res://autoloads/equipment_manager.gd"
InventoryManager="*res://autoloads/inventory_manager.gd"
DamageCalculator="*res://autoloads/damage_calculator.gd"
Logger="*res://autoloads/logger.gd"
""")

    # --- Facade/delegation chain: GameManager.save_game()
    #     -> EquipmentManager.serialize_equipment()
    #     -> InventoryManager.serialize() -> InventoryManager._item_to_dict()
    write("autoloads/game_manager.gd", f"""extends Node

signal game_saved
signal game_loaded

func save_game() -> void:
    var payload := {{"equipment": EquipmentManager.serialize_equipment()}}
    Logger.log_debug("save_game payload built")
    game_saved.emit()

func load_game() -> void:
    Logger.log_debug("load_game called")
    game_loaded.emit()
{_padding_methods([
    "pause_game", "resume_game", "quit_game", "restart_level",
    "get_save_path", "get_save_slot_count", "delete_save_slot",
    "change_scene", "preload_next_scene", "show_main_menu",
    "handle_window_focus", "handle_window_blur", "toggle_fullscreen",
    "apply_video_settings", "apply_audio_settings", "apply_input_settings",
    "get_platform_name", "check_for_updates", "report_analytics_event",
])}
""")

    write("autoloads/equipment_manager.gd", f"""extends Node

func serialize_equipment() -> Dictionary:
    Logger.log_debug("serialize_equipment called")
    return InventoryManager.serialize()

func equip_item(item_id: String) -> void:
    Logger.log_debug("equip_item " + item_id)

func unequip_item(slot: String) -> void:
    Logger.log_debug("unequip_item " + slot)
{_padding_methods([
    "get_equipped_weapon", "get_equipped_armor", "get_equipped_accessory",
    "swap_loadout", "save_loadout_preset", "load_loadout_preset",
    "compute_total_defense", "compute_total_attack", "list_equip_slots",
    "is_slot_empty", "get_upgrade_cost", "can_afford_upgrade",
])}
""")

    write("autoloads/inventory_manager.gd", f"""extends Node

var _items: Array = []

func serialize() -> Dictionary:
    var out := []
    for item in _items:
        out.append(_item_to_dict(item))
    Logger.log_debug("inventory serialize done")
    return {{"items": out}}

func _item_to_dict(item) -> Dictionary:
    return {{"id": item, "count": 1}}

func add_item(item_id: String) -> void:
    _items.append(item_id)
    Logger.log_debug("add_item " + item_id)

func remove_item(item_id: String) -> void:
    _items.erase(item_id)
    Logger.log_debug("remove_item " + item_id)
{_padding_methods([
    "has_item", "get_item_count", "sort_by_rarity", "sort_by_name",
    "clear_inventory", "get_capacity", "expand_capacity",
    "find_item_by_tag", "stack_duplicate_items", "get_total_weight",
])}
""")

    # --- Moderately common function: calculate_damage, ~8 real call sites
    #     across combat/entities/systems/ui, plus false-positive bait for
    #     the precision scenario (a comment, a string, and a differently
    #     named function that shares the substring).
    write("autoloads/damage_calculator.gd", f"""extends Node

# calculate_damage is the canonical entry point for all damage math --
# do not duplicate this logic elsewhere.
func calculate_damage(attacker, defender) -> float:
    var base := 10.0
    Logger.log_debug("calculate_damage invoked")
    return base

func calculate_damage_over_time_preview(attacker, defender) -> float:
    # Not the same function as calculate_damage -- a preview-only estimate
    # for the UI tooltip. Shares a name substring, which is exactly the
    # kind of false positive a plain text search can't filter out.
    return calculate_damage(attacker, defender) * 0.1
{_padding_methods([
    "get_damage_type_multiplier", "get_elemental_resistance",
    "roll_variance", "clamp_to_max_hp",
])}
""")

    write("autoloads/logger.gd", """extends Node

func log_debug(message: String) -> void:
    print(message)
""")

    # --- PlayerStats: used as a TYPE annotation in several places, but its
    #     own methods are never called from those sites -- the call-graph
    #     blind spot (callers() sees zero of these references).
    write("entities/player_stats.gd", f"""extends Resource
class_name PlayerStats

var hp: int = 100
var atk: int = 10
var def_: int = 5

func get_total_power() -> int:
    return atk + def_
{_padding_methods(["reset_to_defaults", "apply_level_up", "clone_stats"])}
""")

    write("entities/player.gd", f"""extends Node

var stats: PlayerStats
var _cached_power: int = 0

func _ready() -> void:
    stats = PlayerStats.new()
    Logger.log_debug("player ready")

func take_damage(attacker, amount: float) -> void:
    var dmg := DamageCalculator.calculate_damage(attacker, self)
    if randf() < 0.1:
        dmg = apply_critical_hit_multiplier_v2(dmg)
    Logger.log_debug("take_damage " + str(dmg))

func apply_critical_hit_multiplier_v2(dmg: float) -> float:
    return dmg * 2.5
{_padding_methods([
    "level_up", "respawn", "save_checkpoint", "handle_input",
    "update_animation_state", "play_hurt_sound", "update_ui_health_bar",
])}
""")

    write("entities/enemy.gd", f"""extends Node

var stats: PlayerStats

func _ready() -> void:
    stats = PlayerStats.new()
    Logger.log_debug("enemy ready")

func attack(target) -> void:
    var dmg := DamageCalculator.calculate_damage(self, target)
    Logger.log_debug("enemy attack " + str(dmg))
{_padding_methods([
    "die", "drop_loot", "play_spawn_animation", "update_ai_state",
    "check_aggro_range", "flee_if_low_health",
])}
""")

    # --- Combat/UI/systems: volume, more log_debug() call sites, the
    #     remaining calculate_damage() call sites, the one real call site
    #     for apply_critical_hit_multiplier_v2, and one more PlayerStats
    #     type-usage site.
    write("combat/combat_resolver.gd", f"""extends Node

func resolve_attack(attacker, defender) -> void:
    var dmg := DamageCalculator.calculate_damage(attacker, defender)
    Logger.log_debug("resolve_attack " + str(dmg))
{_padding_methods([
    "resolve_block", "resolve_dodge", "resolve_parry", "queue_hit_vfx",
    "apply_knockback", "apply_status_on_hit",
])}
""")

    write("combat/skill_executor.gd", f"""extends Node

func execute_skill(caster, target, skill_id: String) -> void:
    var dmg := DamageCalculator.calculate_damage(caster, target)
    Logger.log_debug("execute_skill " + skill_id + " " + str(dmg))
{_padding_methods([
    "can_cast", "get_skill_cooldown", "refresh_cooldowns",
    "interrupt_cast", "queue_skill_vfx",
])}
""")

    write("combat/status_effect_manager.gd", f"""extends Node

func apply_dot_tick(target) -> void:
    Logger.log_debug("apply_dot_tick")
{_padding_methods([
    "apply_buff", "apply_debuff", "clear_expired_effects",
    "get_active_effects", "stack_effect",
])}
""")

    write("ui/damage_number_view.gd", f"""extends Node

var owner_stats: PlayerStats

# Shows a rough estimate to the player before they confirm an action --
# see calculate_damage in autoloads/damage_calculator.gd for the real math.
func show_estimate(attacker, defender) -> void:
    var preview := DamageCalculator.calculate_damage_over_time_preview(attacker, defender)
    Logger.log_debug("show_estimate " + str(preview))
{_padding_methods([
    "spawn_floating_number", "pool_floating_numbers", "fade_out_number",
    "update_font_scale",
])}
""")

    write("ui/hud_view.gd", f"""extends Control

func _ready() -> void:
    Logger.log_debug("hud ready")

func update_health_bar(value: float) -> void:
    Logger.log_debug("update_health_bar " + str(value))
{_padding_methods([
    "update_mana_bar", "update_minimap", "show_toast", "hide_toast",
    "update_quest_tracker", "flash_low_health_warning",
])}
""")

    write("ui/inventory_view.gd", f"""extends Control

func _ready() -> void:
    Logger.log_debug("inventory view ready")

func refresh() -> void:
    Logger.log_debug("inventory view refresh")
{_padding_methods([
    "on_item_slot_pressed", "on_item_slot_hovered", "show_tooltip",
    "hide_tooltip", "sort_button_pressed",
])}
""")

    write("systems/save_system.gd", f"""extends Node

func write_save_file(data: Dictionary) -> void:
    Logger.log_debug("write_save_file")
{_padding_methods([
    "read_save_file", "validate_save_data", "migrate_old_save",
    "backup_save_file",
])}
""")

    write("systems/scene_loader.gd", f"""extends Node

func load_scene_async(path: String) -> void:
    Logger.log_debug("load_scene_async " + path)
{_padding_methods([
    "unload_current_scene", "show_loading_screen", "hide_loading_screen",
    "preload_common_assets",
])}
""")

    write("systems/audio_system.gd", f"""extends Node

func play_sfx(name: String) -> void:
    Logger.log_debug("play_sfx " + name)
{_padding_methods([
    "play_music", "stop_music", "set_master_volume", "set_sfx_volume",
    "duck_music_for_dialogue",
])}
""")


if __name__ == "__main__":
    generate()
    print(f"Generated fixture project at {ROOT}")
