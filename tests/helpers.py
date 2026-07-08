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
    rows = conn.execute("""
        SELECT s.res_path AS source_file, s.scope AS source_scope, s.name AS source_fn,
               sig.res_path AS signal_file, sig.scope AS signal_scope, sig.name AS signal_name,
               h.res_path AS handler_file, h.scope AS handler_scope, h.name AS handler_fn
        FROM signal_connections c
        JOIN symbols s ON s.id = c.source_symbol_id
        JOIN symbols sig ON sig.id = c.signal_symbol_id
        JOIN symbols h ON h.id = c.handler_symbol_id
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
