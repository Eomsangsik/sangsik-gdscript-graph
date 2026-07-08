from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from gdtoolkit.parser import parser as gdparser
from lark import Tree

logger = logging.getLogger(__name__)


@dataclass
class ParseResult:
    file_path: Path
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

    try:
        tree = gdparser.parse(source, gather_metadata=True)
        return ParseResult(file_path, res_path, tree, source, None)
    except Exception as exc:
        logger.warning("Failed to parse %s: %s", file_path, exc)
        return ParseResult(file_path, res_path, None, source, str(exc))


def parse_all(gd_files: list[Path], to_res_path) -> list[ParseResult]:
    return [parse_file(f, to_res_path(f)) for f in gd_files]
