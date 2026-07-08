from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from gdscript_graph.db import build_database


class GodotProject:
    """Helper for writing a throwaway Godot project and building its graph."""

    def __init__(self, root: Path):
        self.root = root

    def write(self, relative_path: str, content: str) -> None:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def build(self) -> sqlite3.Connection:
        db_path = self.root.parent / "graph.db"
        build_database(self.root, db_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn


@pytest.fixture
def godot_project(tmp_path: Path) -> GodotProject:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "project.godot").write_text("[application]\n")
    return GodotProject(project_root)
