extends Node

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

func get_damage_type_multiplier() -> void:
    var _local_a := 0
    var _local_b := "placeholder"
    for _i in range(3):
        _local_a += _i
    if _local_a > 100:
        _local_b = "large"
    Logger.log_debug("get_damage_type_multiplier called")

func get_elemental_resistance() -> void:
    var _local_a := 0
    var _local_b := "placeholder"
    for _i in range(3):
        _local_a += _i
    if _local_a > 100:
        _local_b = "large"
    Logger.log_debug("get_elemental_resistance called")

func roll_variance() -> void:
    var _local_a := 0
    var _local_b := "placeholder"
    for _i in range(3):
        _local_a += _i
    if _local_a > 100:
        _local_b = "large"
    Logger.log_debug("roll_variance called")

func clamp_to_max_hp() -> void:
    var _local_a := 0
    var _local_b := "placeholder"
    for _i in range(3):
        _local_a += _i
    if _local_a > 100:
        _local_b = "large"
    Logger.log_debug("clamp_to_max_hp called")

