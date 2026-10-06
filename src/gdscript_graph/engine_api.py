from __future__ import annotations

import functools
import json
from dataclasses import dataclass
from importlib import resources


@dataclass(frozen=True)
class EngineApi:
    """Names (and return types, "" = none/Variant) of what the Godot engine
    and GDScript itself provide -- generated from a Godot build's API dump
    by scripts/generate_godot_api.py. Used to tell a call into the engine
    (`int()`, `queue_free()`, `some_dict.has()`) apart from a call into
    project code that couldn't be resolved."""
    godot_version: str
    global_functions: dict[str, str]
    builtin_types: dict[str, dict[str, str]]
    classes: dict[str, tuple[str | None, dict[str, str], frozenset[str]]]  # (parent, methods, signals)
    singletons: frozenset[str]

    def is_type(self, name: str) -> bool:
        return name in self.builtin_types or name in self.classes

    def method_return_type(self, type_name: str, method: str) -> str | None:
        """Return type of `method` on engine/built-in type `type_name`
        (inherited ones included), "" for none/Variant, or None if the type
        has no such method."""
        if type_name in self.builtin_types:
            return self.builtin_types[type_name].get(method)
        seen: set[str] = set()
        current: str | None = type_name
        while current is not None and current in self.classes and current not in seen:
            seen.add(current)
            parent, methods, _ = self.classes[current]
            if method in methods:
                return methods[method]
            current = parent
        return None

    def has_method(self, type_name: str, method: str) -> bool:
        return self.method_return_type(type_name, method) is not None


def base_type_name(type_name: str) -> str:
    """`Array[Node]` -> `Array`, `Dictionary[String, int]` -> `Dictionary`."""
    return type_name.split("[", 1)[0].strip()


@functools.cache
def engine_api() -> EngineApi:
    data = json.loads(resources.files("gdscript_graph").joinpath("godot_api.json").read_text(encoding="utf-8"))
    return EngineApi(
        godot_version=data["godot_version"],
        global_functions=data["global_functions"],
        builtin_types=data["builtin_types"],
        classes={name: (parent, methods, frozenset(signals)) for name, (parent, methods, signals) in data["classes"].items()},
        singletons=frozenset(data["singletons"]),
    )
