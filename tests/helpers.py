from __future__ import annotations

import sqlite3


def resolved_calls(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT s.res_path AS src_file, s.scope AS src_scope, s.name AS src_fn,
               t.res_path AS tgt_file, t.scope AS tgt_scope, t.name AS tgt_fn
        FROM calls c
        JOIN symbols s ON s.id = c.source_symbol_id
        JOIN symbols t ON t.id = c.target_symbol_id
    """).fetchall()
    return [dict(r) for r in rows]


def unresolved_calls(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT source_res_path, source_scope, source_function, receiver, called_name, reason
        FROM unresolved_calls
    """).fetchall()
    return [dict(r) for r in rows]


def signal_connections(conn: sqlite3.Connection) -> list[dict]:
    """Code `.connect()` connections. `signal_file`/`signal_scope` are None
    when the signal isn't project-declared (e.g. an engine built-in)."""
    rows = conn.execute("""
        SELECT s.res_path AS source_file, s.scope AS source_scope, s.name AS source_fn,
               sig.res_path AS signal_file, sig.scope AS signal_scope, c.signal_name AS signal_name,
               h.res_path AS handler_file, h.scope AS handler_scope, h.name AS handler_fn
        FROM signal_connections c
        JOIN symbols s ON s.id = c.source_symbol_id
        LEFT JOIN symbols sig ON sig.id = c.signal_symbol_id
        JOIN symbols h ON h.id = c.handler_symbol_id
    """).fetchall()
    return [dict(r) for r in rows]


def scene_connections(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT c.scene_res_path AS scene, c.signal_name, c.from_node, c.to_node,
               sig.res_path AS signal_file, h.res_path AS handler_file, h.name AS handler_fn
        FROM scene_connections c
        LEFT JOIN symbols sig ON sig.id = c.signal_symbol_id
        JOIN symbols h ON h.id = c.handler_symbol_id
    """).fetchall()
    return [dict(r) for r in rows]


def unresolved_scene_connections(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT scene_res_path AS scene, signal_name, from_node, to_node, method, reason
        FROM unresolved_scene_connections
    """).fetchall()
    return [dict(r) for r in rows]


def unresolved_connections(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT source_res_path, source_function, signal_receiver, signal_name,
               handler_receiver, handler_name, reason
        FROM unresolved_connections
    """).fetchall()
    return [dict(r) for r in rows]


def symbols(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT res_path, name, kind, scope, line FROM symbols").fetchall()
    return [dict(r) for r in rows]
