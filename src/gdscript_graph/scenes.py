from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_TSCN_TAG_RE = re.compile(r'^\[(\w+)')
_TSCN_ATTR_RE = re.compile(r'(\w+)=("[^"]*"|[^\s\]]+)')
_INSTANCE_ATTR_RE = re.compile(r'(?:^|\s)instance=')
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


def _instance_ref(attr_str: str) -> str | None:
    """The ExtResource id in a node header's `instance=ExtResource(...)`,
    matched in place (anchored right after `instance=`) rather than
    searched for, so it handles Godot 3's spaced `ExtResource( 2 )` too."""
    attr_match = _INSTANCE_ATTR_RE.search(attr_str)
    if attr_match is None:
        return None
    ref_match = _EXT_RESOURCE_REF_RE.match(attr_str, attr_match.end())
    return ref_match.group(1) if ref_match is not None else None


@dataclass
class SceneNode:
    script: str | None  # res:// path from the node's own `script = ExtResource(...)`
    instance: str | None  # res:// path of the scene this node instances, if any
    # False for a section that only overrides properties of a node that
    # really comes from an instanced scene (no `type=`/`instance=` of its
    # own) -- its script, unless overridden here, is that scene's.
    declared: bool


@dataclass
class SceneConnection:
    signal: str
    from_node: str  # node paths relative to the scene root ("." = the root)
    to_node: str
    method: str
    line: int


@dataclass
class Scene:
    res_path: str
    nodes: dict[str, SceneNode] = field(default_factory=dict)  # keyed by node path ("." = root)
    connections: list[SceneConnection] = field(default_factory=list)


def _node_path(name: str, parent: str | None) -> str:
    if parent is None:
        return "."
    if parent == ".":
        return name
    return f"{parent}/{name}"


def parse_scene(text: str, res_path: str) -> Scene:
    """Parse a text scene (Godot 3 or 4 `.tscn`) into its node tree -- each
    node's own script and instanced scene -- and its saved `[connection]`
    entries (signal connections made in the editor)."""
    scene = Scene(res_path=res_path)
    ext_resources: dict[str, str] = {}
    current_node: SceneNode | None = None
    for line_no, line in enumerate(text.splitlines(), start=1):
        section = _parse_tscn_section_header(line)
        if section is not None:
            tag, attr_str = section
            attrs = _parse_tscn_attrs(attr_str)
            current_node = None
            if tag == "ext_resource":
                res_id, path = attrs.get("id"), attrs.get("path")
                if res_id is not None and path is not None:
                    ext_resources[res_id] = path
            elif tag == "node" and "name" in attrs:
                instance_id = _instance_ref(attr_str)
                current_node = SceneNode(
                    script=None,
                    instance=ext_resources.get(instance_id) if instance_id is not None else None,
                    declared="type" in attrs or instance_id is not None,
                )
                scene.nodes[_node_path(attrs["name"], attrs.get("parent"))] = current_node
            elif tag == "connection":
                signal, from_node, to_node, method = (
                    attrs.get("signal"), attrs.get("from"), attrs.get("to"), attrs.get("method"),
                )
                if signal and from_node and to_node and method:
                    scene.connections.append(SceneConnection(signal, from_node, to_node, method, line_no))
            continue
        if current_node is not None:
            script_match = _TSCN_SCRIPT_PROP_RE.match(line.strip())
            if script_match is not None:
                current_node.script = ext_resources.get(script_match.group(1))
    return scene


class SceneIndex:
    """Lazily parsed scenes of one project, keyed by res:// path, answering
    which script a node runs -- following instanced and inherited scenes."""

    def __init__(self, project_root: Path) -> None:
        self._root = project_root
        self._scenes: dict[str, Scene | None] = {}

    def get(self, res_path: str) -> Scene | None:
        """The parsed scene at `res_path`, or None if it isn't a readable
        text scene (missing, binary `.scn`, not UTF-8...)."""
        if res_path not in self._scenes:
            scene = None
            if res_path.startswith("res://") and res_path.endswith(".tscn"):
                try:
                    text = (self._root / res_path.removeprefix("res://")).read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    text = None
                if text is not None:
                    scene = parse_scene(text, res_path)
            self._scenes[res_path] = scene
        return self._scenes[res_path]

    def script_for_node(
        self, scene_res_path: str, node_path: str, _visited: frozenset[str] = frozenset()
    ) -> str | None:
        """res:// path of the script attached to the node at `node_path` in
        a scene, or None if it has none / can't be determined.

        A node with no script of its own that instances another scene runs
        that scene's root script. A node this scene doesn't declare itself
        -- or only overrides properties of -- comes from an instanced scene:
        this scene's own root, for an inherited scene, or an instanced child
        whose children are editable. It's looked up in that scene, relative
        to the instancing node. `_visited` guards against a (malformed)
        instancing cycle."""
        if scene_res_path in _visited:
            return None
        scene = self.get(scene_res_path)
        if scene is None:
            return None
        visited = _visited | {scene_res_path}

        node = scene.nodes.get(node_path)
        if node is not None:
            if node.script is not None:
                return node.script
            if node.instance is not None:
                return self.script_for_node(node.instance, ".", visited)
            if node.declared:
                return None

        parts = [] if node_path == "." else node_path.split("/")
        for depth in range(len(parts) - 1, -1, -1):
            owner = scene.nodes.get("/".join(parts[:depth]) or ".")
            if owner is not None and owner.instance is not None:
                return self.script_for_node(owner.instance, "/".join(parts[depth:]), visited)
        return None

