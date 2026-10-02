extends Node

var owner_stats: PlayerStats

# Shows a rough estimate to the player before they confirm an action --
# see calculate_damage in autoloads/damage_calculator.gd for the real math.
func show_estimate(attacker, defender) -> void:
    var preview := DamageCalculator.calculate_damage_over_time_preview(attacker, defender)
    Logger.log_debug("show_estimate " + str(preview))

func spawn_floating_number() -> void:
    var _local_a := 0
    var _local_b := "placeholder"
    for _i in range(3):
        _local_a += _i
    if _local_a > 100:
        _local_b = "large"
    Logger.log_debug("spawn_floating_number called")

func pool_floating_numbers() -> void:
    var _local_a := 0
    var _local_b := "placeholder"
    for _i in range(3):
        _local_a += _i
    if _local_a > 100:
        _local_b = "large"
    Logger.log_debug("pool_floating_numbers called")

func fade_out_number() -> void:
    var _local_a := 0
    var _local_b := "placeholder"
    for _i in range(3):
        _local_a += _i
    if _local_a > 100:
        _local_b = "large"
    Logger.log_debug("fade_out_number called")

func update_font_scale() -> void:
    var _local_a := 0
    var _local_b := "placeholder"
    for _i in range(3):
        _local_a += _i
    if _local_a > 100:
        _local_b = "large"
    Logger.log_debug("update_font_scale called")

