from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from gdscript_graph.scenes import SceneIndex, find_scene_files

_AUTOLOAD_LINE_RE = re.compile(r'^(\w+)\s*=\s*"\*?(res://[^"]+)"')


@dataclass
class ProjectFiles:
    root: Path
    gd_files: list[Path]
    autoloads: dict[str, str]  # AutoloadName -> res://path
    scene_files: list[Path] = field(default_factory=list)
    scenes: SceneIndex | None = None

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


def parse_autoloads(root: Path, scenes: SceneIndex | None = None) -> dict[str, str]:
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

    # An autoload registered as a .tscn (common when the singleton needs
    # child nodes) would otherwise never resolve -- its res:// path never
    # matches any parsed .gd file, so every call through it would silently
    # fail. It runs its root node's script (see SceneIndex.script_for_node
    # for a root that instances another scene).
    scenes = scenes or SceneIndex(root)
    for name, res_path in list(autoloads.items()):
        if res_path.endswith(".tscn"):
            script_path = scenes.script_for_node(res_path, ".")
            if script_path is not None:
                autoloads[name] = script_path

    return autoloads


def discover(root: Path) -> ProjectFiles:
    scenes = SceneIndex(root)
    return ProjectFiles(
        root=root,
        gd_files=find_gd_files(root),
        autoloads=parse_autoloads(root, scenes),
        scene_files=find_scene_files(root),
        scenes=scenes,
    )
