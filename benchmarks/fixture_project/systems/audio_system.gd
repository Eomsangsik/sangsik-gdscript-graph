extends Node

func play_sfx(name: String) -> void:
    Logger.log_debug("play_sfx " + name)

func play_music() -> void:
    var _local_a := 0
    var _local_b := "placeholder"
    for _i in range(3):
        _local_a += _i
    if _local_a > 100:
        _local_b = "large"
    Logger.log_debug("play_music called")

func stop_music() -> void:
    var _local_a := 0
    var _local_b := "placeholder"
    for _i in range(3):
        _local_a += _i
    if _local_a > 100:
        _local_b = "large"
    Logger.log_debug("stop_music called")

func set_master_volume() -> void:
    var _local_a := 0
    var _local_b := "placeholder"
    for _i in range(3):
        _local_a += _i
    if _local_a > 100:
        _local_b = "large"
    Logger.log_debug("set_master_volume called")

func set_sfx_volume() -> void:
    var _local_a := 0
    var _local_b := "placeholder"
    for _i in range(3):
        _local_a += _i
    if _local_a > 100:
        _local_b = "large"
    Logger.log_debug("set_sfx_volume called")

func duck_music_for_dialogue() -> void:
    var _local_a := 0
    var _local_b := "placeholder"
    for _i in range(3):
        _local_a += _i
    if _local_a > 100:
        _local_b = "large"
    Logger.log_debug("duck_music_for_dialogue called")

