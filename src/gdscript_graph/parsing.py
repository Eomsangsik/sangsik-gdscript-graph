from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from gdtoolkit.parser import parser as gdparser
from lark import Tree

from gdscript_graph.lexical import triple_quote_multiline_strings

logger = logging.getLogger(__name__)


@dataclass
class ParseResult:
    file_path: Path | None
    res_path: str
    tree: Tree | None
    source: str
    error: str | None


def parse_file(file_path: Path, res_path: str) -> ParseResult:
    try:
        source = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        logger.warning("Failed to read %s: %s", file_path, exc)
        return ParseResult(file_path, res_path, None, "", str(exc))
    return parse_source(source, res_path, file_path)


def parse_source(source: str, res_path: str, file_path: Path | None = None) -> ParseResult:
    """Parse GDScript `source`. A file the grammar rejects is retried once
    with its multi-line "..." strings triple-quoted (valid Godot the grammar
    doesn't accept -- see `triple_quote_multiline_strings`); lines don't
    move, so the tree's positions still match `source`, which the result
    keeps as-is."""
    try:
        tree = gdparser.parse(source, gather_metadata=True)
        return ParseResult(file_path, res_path, tree, source, None)
    except Exception as exc:
        error = exc
    rewritten = triple_quote_multiline_strings(source)
    if rewritten != source:
        try:
            tree = gdparser.parse(rewritten, gather_metadata=True)
            return ParseResult(file_path, res_path, tree, source, None)
        except Exception as exc:
            error = exc
    logger.warning("Failed to parse %s: %s", file_path or res_path, error)
    return ParseResult(file_path, res_path, None, source, str(error))
