from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_AUTOLOAD_LINE_RE = re.compile(r'^(\w+)\s*=\s*"\*?(res://[^"]+)"')
_TSCN_TAG_RE = re.compile(r'^\[(\w+)')
_TSCN_ATTR_RE = re.compile(r'(\w+)=("[^"]*"|[^\s\]]+)')
# The value character class excludes "(" too, not just '"'/")'/whitespace --
# without it, `.search()`/`.match()` against a string containing many
# repeated "ExtResource(" substrings with no closing ")" (a fully
# well-formed, closed .tscn `instance=` attribute value can still contain
# this as ordinary text) backtracks the "+" across the *entire* remaining
# string at every occurrence, an O(n^2) blowup that can hang a build for
# minutes on a real, reachable input -- not just a malformed/truncated
# file, unlike the .tscn section-header ReDoS this mirrors. A real
# ExtResource id/path never contains "(", so excluding it changes nothing
# for valid input while bounding backtracking to the (short) gap between
# consecutive "ExtResource(" occurrences.
_TSCN_SCRIPT_PROP_RE = re.compile(r'^script\s*=\s*ExtResource\(\s*"?([^")\s(]+)"?\s*\)')
_EXT_RESOURCE_REF_RE = re.compile(r'ExtResource\(\s*"?([^")\s(]+)"?\s*\)')


def _parse_tscn_attrs(attr_str: str) -> dict[str, str]:
    return {k: v.strip('"') for k, v in _TSCN_ATTR_RE.findall(attr_str)}


def _parse_tscn_section_header(line: str) -> tuple[str, str] | None:
    """Return (tag, attr_str) for a `[tag attr="val" ...]` section header
    line, or None if the line isn't a well-formed, closed section header.

    Deliberately checks for the closing `]` with a plain string operation
    *before* running any regex over the attribute run, instead of matching
    the whole thing with one regex (`\\[(\\w+)((?:\\s+\\w+=(?:"[^"]*"|
    [^\\s\\]]+))*)\\s*\\]`, the original approach). That single regex's
    repeated group has no anchor to stop backtracking when the line starts
    with `[` but the terminating `]` is missing (e.g. a truncated/corrupted
    .tscn save) -- catastrophic backtracking, hanging the whole build
    indefinitely with no exception to catch and no way to time it out.
    `_TSCN_ATTR_RE.findall` below has no such ambiguity (each match is
    independent, not wrapped in a repeated group with backtracking
    choices), so it's used for the attribute run instead."""
    stripped = line.rstrip()
    if not (stripped.startswith("[") and stripped.endswith("]")):
        return None
    tag_match = _TSCN_TAG_RE.match(stripped)
    if tag_match is None:
        return None
    return tag_match.group(1), stripped[tag_match.end():-1]


def _resolve_scene_root_script(
    tscn_path: Path, project_root: Path, _visited: frozenset[Path] = frozenset()
) -> str | None:
    """Return the res:// path of the script attached to a scene's root
    node, or None if it has none / can't be read. An autoload registered
    as a .tscn (common when the singleton needs child nodes) would
    otherwise never resolve -- its res:// path never matches any parsed
    .gd file, so every call through it silently fails to resolve.

    If the root node has no script of its own but is an *instanced*
    sub-scene (`instance=ExtResource(...)` referencing another .tscn --
    a common way to compose a reusable base scene as a singleton without
    adding autoload-specific script logic on top), recurse into that
    sub-scene's own root to find its script. `_visited` guards against a
    (malformed) instancing cycle."""
    try:
        lines = tscn_path.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, OSError):
        return None

    ext_resources: dict[str, str] = {}
    root_instance_ref: str | None = None
    root_node_seen = False
    in_root_node = False
    for line in lines:
        section = _parse_tscn_section_header(line)
        if section is not None:
            tag, attr_str = section
            attrs = _parse_tscn_attrs(attr_str)
            if tag == "ext_resource":
                res_id, path = attrs.get("id"), attrs.get("path")
                if res_id is not None and path is not None:
                    ext_resources[res_id] = path
                in_root_node = False
            elif tag == "node":
                if root_node_seen:
                    break
                in_root_node = "parent" not in attrs
                if in_root_node:
                    root_node_seen = True
                    instance_attr = attrs.get("instance")
                    if instance_attr is not None:
                        ref_match = _EXT_RESOURCE_REF_RE.search(instance_attr)
                        if ref_match is not None:
                            root_instance_ref = ref_match.group(1)
            else:
                in_root_node = False
            continue
        if in_root_node:
            script_match = _TSCN_SCRIPT_PROP_RE.match(line.strip())
            if script_match is not None:
                return ext_resources.get(script_match.group(1))

    if root_instance_ref is not None:
        sub_scene_path = ext_resources.get(root_instance_ref)
        if sub_scene_path is not None and sub_scene_path.endswith(".tscn"):
            sub_scene_file = project_root / sub_scene_path.removeprefix("res://")
            if sub_scene_file not in _visited and sub_scene_file.exists():
                return _resolve_scene_root_script(sub_scene_file, project_root, _visited | {tscn_path})
    return None


@dataclass
class ProjectFiles:
    root: Path
    gd_files: list[Path]
    autoloads: dict[str, str]  # AutoloadName -> res://path

    def to_res_path(self, file_path: Path) -> str:
        rel = file_path.relative_to(self.root)
        return "res://" + rel.as_posix()


def find_gd_files(root: Path) -> list[Path]:
    # Sort by the POSIX-style relative path string, not by raw Path
    # comparison -- pathlib's own ordering is platform-flavor-dependent
    # (PurePosixPath compares case-sensitively, PureWindowsPath case-
    # insensitively), so building the identical, unchanged project on
    # different host OSes could discover files in a different order and
    # silently flip which file wins a duplicate `class_name` collision
    # (`resolve.build_class_name_table`: "later file wins"). Sorting by
    # the string form both functions will later produce anyway (see
    # `ProjectFiles.to_res_path`) makes that outcome deterministic
    # regardless of build host.
    return sorted(root.rglob("*.gd"), key=lambda p: p.relative_to(root).as_posix())


def parse_autoloads(root: Path) -> dict[str, str]:
    project_file = root / "project.godot"
    if not project_file.exists():
        return {}

    try:
        text = project_file.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return {}

    autoloads: dict[str, str] = {}
    in_autoload_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_autoload_section = stripped == "[autoload]"
            continue
        if not in_autoload_section or not stripped:
            continue
        match = _AUTOLOAD_LINE_RE.match(stripped)
        if match:
            autoloads[match.group(1)] = match.group(2)

    for name, res_path in list(autoloads.items()):
        if res_path.endswith(".tscn"):
            script_path = _resolve_scene_root_script(root / res_path.removeprefix("res://"), root)
            if script_path is not None:
                autoloads[name] = script_path

    return autoloads


def discover(root: Path) -> ProjectFiles:
    return ProjectFiles(
        root=root,
        gd_files=find_gd_files(root),
        autoloads=parse_autoloads(root),
    )
