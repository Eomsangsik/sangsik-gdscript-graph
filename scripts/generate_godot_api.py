"""Regenerate src/gdscript_graph/godot_api.json from a Godot build's API dump.

    godot --headless --dump-extension-api     # writes extension_api.json here
    python scripts/generate_godot_api.py extension_api.json

Keeps only names and types -- each engine class's parent, methods (with
return types) and signals, the built-in types' methods, the global
functions, the singletons -- which is all gdscript-graph needs to tell a
call into the engine apart from a call it failed to resolve.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Functions GDScript itself adds on top of the engine's global scope (the
# `@GDScript` class reference) -- not part of the extension API dump -- with
# their return types ("" = none/Variant).
GDSCRIPT_FUNCTIONS = {
    "Color8": "Color", "assert": "", "char": "String", "convert": "", "dict_to_inst": "Object",
    "get_stack": "Array", "inst_to_dict": "Dictionary", "is_instance_of": "bool", "len": "int",
    "load": "Resource", "ord": "int", "preload": "Resource", "print_debug": "", "print_stack": "",
    "range": "Array", "type_exists": "bool",
}


# Object methods every script has that the dump leaves out (documented in
# the Object class reference, but special-cased by the engine).
OBJECT_EXTRA_METHODS = {"free": ""}


def _type(return_value: dict | None) -> str:
    """A method's return type as a GDScript type name ("" = none/Variant)."""
    name = (return_value or {}).get("type", "")
    if name.startswith(("enum::", "bitfield::")):
        return "int"
    if name.startswith("typedarray::"):
        return "Array"
    if name.startswith("typeddictionary::"):
        return "Dictionary"
    return "" if name in ("Variant", "void") else name


def _methods(methods: list[dict]) -> dict[str, str]:
    return {m["name"]: _type(m.get("return_value") or ({"type": m["return_type"]} if "return_type" in m else None))
            for m in methods}

OUT = Path(__file__).resolve().parent.parent / "src" / "gdscript_graph" / "godot_api.json"


def main(dump_path: str) -> None:
    api = json.loads(Path(dump_path).read_text(encoding="utf-8"))
    header = api["header"]
    compact = {
        "godot_version": f"{header['version_major']}.{header['version_minor']}.{header['version_patch']}",
        "global_functions": {**_methods(api["utility_functions"]), **GDSCRIPT_FUNCTIONS},
        "builtin_types": {
            b["name"]: _methods(b.get("methods", [])) for b in api["builtin_classes"] if b["name"] != "Nil"
        },
        "classes": {
            c["name"]: [
                c.get("inherits"),
                {**_methods(c.get("methods", [])), **(OBJECT_EXTRA_METHODS if c["name"] == "Object" else {})},
                sorted({s["name"] for s in c.get("signals", [])}),
            ]
            for c in api["classes"]
        },
        "singletons": sorted({s["name"] for s in api["singletons"]}),
    }
    OUT.write_text(json.dumps(compact, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT} (Godot {compact['godot_version']}, {OUT.stat().st_size // 1024} KiB)")


if __name__ == "__main__":
    main(sys.argv[1])
