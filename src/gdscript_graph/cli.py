from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from gdscript_graph.db import build_database


def _cmd_build(args: argparse.Namespace) -> int:
    project_root = Path(args.project_dir).resolve()
    if not project_root.is_dir():
        print(f"error: project directory does not exist: {project_root}", file=sys.stderr)
        return 1

    db_path = Path(args.out).resolve() if args.out else project_root / ".gdscript_graph.db"

    if not (project_root / "project.godot").exists():
        print(f"warning: {project_root} has no project.godot", file=sys.stderr)

    try:
        stats = build_database(project_root, db_path)
    except (OSError, sqlite3.Error) as exc:
        print(f"error: could not write database to {db_path}: {exc}", file=sys.stderr)
        return 1
    print(f"files: {stats.file_count} (parse errors: {stats.parse_error_count})")
    print(
        f"functions: {stats.function_count}, signals: {stats.signal_count}, "
        f"fields: {stats.field_count}, enums: {stats.enum_count}"
    )
    print(f"resolved calls: {stats.resolved_call_count}, unresolved: {stats.unresolved_call_count}")
    print(
        f"resolved signal connections: {stats.resolved_connection_count}, "
        f"unresolved: {stats.unresolved_connection_count}"
    )
    for class_name, paths in sorted(stats.duplicate_class_names.items()):
        print(
            f"warning: class_name '{class_name}' is declared in {len(paths)} files "
            f"({', '.join(sorted(paths))}) -- resolution silently uses {sorted(paths)[-1]}",
            file=sys.stderr,
        )
    print(f"db written to: {db_path}")
    return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    from gdscript_graph.mcp_server import run_server

    db_path = Path(args.db).resolve()
    try:
        run_server(db_path)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="gdscript-graph")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="Index a Godot project into a graph DB")
    build_parser.add_argument("project_dir", help="Path to the Godot project root")
    build_parser.add_argument("-o", "--out", help="Output DB path (default: <project>/.gdscript_graph.db)")
    build_parser.set_defaults(func=_cmd_build)

    mcp_parser = subparsers.add_parser("mcp", help="Run the MCP server against a built graph DB")
    mcp_parser.add_argument("db", help="Path to the graph DB")
    mcp_parser.set_defaults(func=_cmd_mcp)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
