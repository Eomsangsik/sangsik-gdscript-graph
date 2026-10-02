extends Node

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

func level_up() -> void:
    var _local_a := 0
    var _local_b := "placeholder"
    for _i in range(3):
        _local_a += _i
    if _local_a > 100:
        _local_b = "large"
    Logger.log_debug("level_up called")

func respawn() -> void:
    var _local_a := 0
    var _local_b := "placeholder"
    for _i in range(3):
        _local_a += _i
    if _local_a > 100:
        _local_b = "large"
    Logger.log_debug("respawn called")

func save_checkpoint() -> void:
    var _local_a := 0
    var _local_b := "placeholder"
    for _i in range(3):
        _local_a += _i
    if _local_a > 100:
        _local_b = "large"
    Logger.log_debug("save_checkpoint called")

func handle_input() -> void:
    var _local_a := 0
    var _local_b := "placeholder"
    for _i in range(3):
        _local_a += _i
    if _local_a > 100:
        _local_b = "large"
    Logger.log_debug("handle_input called")

func update_animation_state() -> void:
    var _local_a := 0
    var _local_b := "placeholder"
    for _i in range(3):
        _local_a += _i
    if _local_a > 100:
        _local_b = "large"
    Logger.log_debug("update_animation_state called")

func play_hurt_sound() -> void:
    var _local_a := 0
    var _local_b := "placeholder"
    for _i in range(3):
        _local_a += _i
    if _local_a > 100:
        _local_b = "large"
    Logger.log_debug("play_hurt_sound called")

func update_ui_health_bar() -> void:
    var _local_a := 0
    var _local_b := "placeholder"
    for _i in range(3):
        _local_a += _i
    if _local_a > 100:
        _local_b = "large"
    Logger.log_debug("update_ui_health_bar called")

