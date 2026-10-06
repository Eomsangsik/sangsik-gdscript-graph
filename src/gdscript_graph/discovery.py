from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from gdscript_graph.scenes import SceneIndex

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


def find_project_files(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    """Every file under `root` ending in one of `suffixes`, as Godot's own
    filesystem scan sees the project: a file or directory whose name starts
    with "." is skipped (`.godot`, `.git`, ...), as is a directory holding a
    `.gdignore` file or a nested project (`project.godot`). A symlinked
    directory is followed, and its files keep their path *through the
    link* -- the `res://` path Godot gives them (e.g. `addons/gdUnit4`
    linked from a vendored checkout).

    Without these rules a large project's `.git` and `.godot` (often well
    over 1 GB) made up most of every rebuild's discovery time, and a
    symlinked addon's files were indexed under their hidden real location
    instead of their `res://addons/...` path.

    Sorted by the POSIX-style relative path string, not by raw Path
    comparison -- pathlib's own ordering is platform-flavor-dependent
    (PurePosixPath compares case-sensitively, PureWindowsPath case-
    insensitively), so building the identical, unchanged project on
    different host OSes could discover files in a different order and
    silently flip which file wins a duplicate `class_name` collision
    (`resolve.build_class_name_table`: "later file wins")."""
    found: list[Path] = []
    link_targets: set[str] = set()

    def walk(directory: str, real: str, is_root: bool) -> None:
        # `real` is `directory` with every symlink resolved, tracked along
        # the way instead of calling realpath() on each of what can be
        # 10,000+ directories -- only a symlink needs resolving.
        try:
            with os.scandir(directory) as it:
                entries = list(it)
        except OSError:
            return
        if not is_root and any(e.name in (".gdignore", "project.godot") for e in entries):
            return
        subdirs: list[os.DirEntry] = []
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir():
                    subdirs.append(entry)
                elif entry.name.endswith(suffixes) and entry.is_file():
                    found.append(Path(entry.path))
            except OSError:
                continue
        # Sorted so which of two links to one directory wins is stable.
        for entry in sorted(subdirs, key=lambda e: e.name):
            try:
                is_link = entry.is_symlink()
            except OSError:
                continue
            if not is_link:
                walk(entry.path, os.path.join(real, entry.name), is_root=False)
                continue
            target = os.path.realpath(entry.path)
            # A link back into the directories being walked is a cycle; a
            # second link to one already walked would only duplicate it.
            if target in link_targets or real == target or real.startswith(target + os.sep):
                continue
            link_targets.add(target)
            walk(entry.path, target, is_root=False)

    walk(str(root), os.path.realpath(root), is_root=True)
    return sorted(found, key=lambda p: p.relative_to(root).as_posix())


def find_gd_files(root: Path) -> list[Path]:
    return find_project_files(root, (".gd",))


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
    files = find_project_files(root, (".gd", ".tscn"))
    return ProjectFiles(
        root=root,
        gd_files=[f for f in files if f.suffix == ".gd"],
        autoloads=parse_autoloads(root, scenes),
        scene_files=[f for f in files if f.suffix == ".tscn"],
        scenes=scenes,
    )
