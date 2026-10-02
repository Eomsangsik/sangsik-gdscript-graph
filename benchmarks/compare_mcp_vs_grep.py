"""Reproducible benchmark: MCP tool calls vs. grep-based exploration, over
a small set of deliberately-constructed scenarios (see generate_fixture.py)
chosen to cover both the strong and weak cases honestly -- not just the
scenarios where this tool wins.

Usage:
    .venv/bin/python benchmarks/generate_fixture.py   # (re)build the fixture project
    .venv/bin/python -m gdscript_graph.cli build benchmarks/fixture_project -o benchmarks/fixture_project.db
    .venv/bin/python benchmarks/compare_mcp_vs_grep.py

Methodology:
- MCP-side numbers are the actual bytes of every `content` block returned by
  a real MCP stdio session calling the real tools -- i.e. what an agent's
  context window would actually receive, not a shortcut through the
  underlying Python functions.
- grep-side numbers use the real `grep` binary (not a text search
  approximation) in three postures an agent might realistically take:
    - "grep only": raw `grep -rn` output, no follow-up reads at all.
    - "grep + minimal read": for each match, read only a few lines of
      context around it (the DISCIPLINED case).
    - "grep + full-file read": for each file with >=1 match, read the
      whole file (the common, less disciplined default).
- Token counts use tiktoken's cl100k_base encoding as a reproducible
  stand-in for "a modern LLM tokenizer" -- not Claude's exact tokenizer
  (not public), but far more accurate than a bytes/4 heuristic and good
  enough to compare relative magnitudes.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import tiktoken
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from gdscript_graph import db as gdb  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_ROOT = Path(__file__).resolve().parent / "fixture_project"
DB_PATH = Path(__file__).resolve().parent / "fixture_project.db"

_ENC = tiktoken.get_encoding("cl100k_base")


def tokens(text: str) -> int:
    return len(_ENC.encode(text))


def run_grep(pattern: str) -> tuple[str, list[tuple[Path, int]]]:
    """Returns (raw grep output, [(file, line_number), ...] for each match)."""
    result = subprocess.run(
        ["grep", "-rn", "--", pattern, str(FIXTURE_ROOT)],
        capture_output=True, text=True,
    )
    matches = []
    for line in result.stdout.splitlines():
        file_part, _, rest = line.partition(":")
        line_no_part, _, _ = rest.partition(":")
        try:
            matches.append((Path(file_part), int(line_no_part)))
        except ValueError:
            continue
    return result.stdout, matches


def grep_plus_minimal_read(matches: list[tuple[Path, int]], context: int = 3) -> str:
    out = []
    for file_path, line_no in matches:
        lines = file_path.read_text().splitlines()
        start = max(0, line_no - 1 - context)
        end = min(len(lines), line_no + context)
        out.append(f"{file_path}:{line_no - context}-{line_no + context}\n" + "\n".join(lines[start:end]))
    return "\n---\n".join(out)


def grep_plus_full_read(matches: list[tuple[Path, int]]) -> str:
    seen_files = {}
    for file_path, _ in matches:
        if file_path not in seen_files:
            seen_files[file_path] = file_path.read_text()
    return "\n---\n".join(f"{p}\n{text}" for p, text in seen_files.items())


def measure_latency(runs: int = 9) -> dict:
    """Raw backend query latency, not full MCP stdio round-trip time (which
    is dominated by subprocess/protocol overhead, not the query itself) --
    a direct DB call for the `callers` query vs. a `grep -rn` subprocess,
    both against the same fixture project, averaged over `runs` runs."""
    conn = gdb.connect(DB_PATH)
    db_times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        gdb.get_callers(conn, "calculate_damage")
        db_times.append((time.perf_counter() - t0) * 1000)
    conn.close()

    grep_times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        subprocess.run(
            ["grep", "-rn", "--", "calculate_damage", str(FIXTURE_ROOT)],
            capture_output=True, text=True,
        )
        grep_times.append((time.perf_counter() - t0) * 1000)

    return {
        "db_query_ms_avg": sum(db_times) / len(db_times),
        "db_query_ms_min": min(db_times),
        "db_query_ms_max": max(db_times),
        "grep_ms_avg": sum(grep_times) / len(grep_times),
        "grep_ms_min": min(grep_times),
        "grep_ms_max": max(grep_times),
    }


class McpSession:
    def __init__(self):
        self._exit_stack = None
        self.session: ClientSession | None = None

    async def __aenter__(self):
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "gdscript_graph.cli", "mcp", str(DB_PATH), "--no-watch"],
            cwd=str(REPO_ROOT),
            env={"PYTHONPATH": str(REPO_ROOT / "src")},
        )
        self._client_cm = stdio_client(params)
        read, write = await self._client_cm.__aenter__()
        self._session_cm = ClientSession(read, write)
        self.session = await self._session_cm.__aenter__()
        await self.session.initialize()
        return self

    async def __aexit__(self, *exc):
        await self._session_cm.__aexit__(*exc)
        await self._client_cm.__aexit__(*exc)

    async def call(self, tool: str, args: dict) -> str:
        """Returns the concatenated text of every content block -- what an
        agent's context actually receives for a tool result, including the
        one-block-per-list-item split FastMCP does for list-returning
        tools."""
        result = await self.session.call_tool(tool, args)
        return "\n".join(block.text for block in result.content if hasattr(block, "text"))


async def run_benchmark() -> list[dict]:
    rows = []
    async with McpSession() as mcp:

        async def scenario(name: str, task_type: str, grep_patterns: list[str], mcp_calls: list[tuple[str, dict]]):
            # grep_patterns is a *sequence* of search rounds -- a multi-hop
            # question (e.g. tracing a facade chain) realistically needs
            # more than one grep, each informed by the previous round's
            # result, so its grep-side cost is the sum of all rounds, not
            # just the first.
            grep_raw_all, minimal_all, full_all = [], [], []
            total_matches = 0
            for pattern in grep_patterns:
                grep_raw, matches = run_grep(pattern)
                grep_raw_all.append(grep_raw)
                minimal_all.append(grep_plus_minimal_read(matches))
                full_all.append(grep_plus_full_read(matches))
                total_matches += len(matches)
            grep_raw = "\n".join(grep_raw_all)
            minimal = "\n".join(minimal_all)
            full = "\n".join(full_all)
            matches = [None] * total_matches  # only the count is used below

            mcp_text = ""
            for tool, args in mcp_calls:
                mcp_text += await mcp.call(tool, args)

            rows.append({
                "scenario": name,
                "task_type": task_type,
                "grep_matches": len(matches),
                "mcp_tokens": tokens(mcp_text),
                "grep_only_tokens": tokens(grep_raw),
                "grep_minimal_tokens": tokens(minimal),
                "grep_full_tokens": tokens(full),
            })

        await scenario(
            "calculate_damage callers",
            "moderately common function",
            ["calculate_damage"],
            [("callers", {"function_name": "calculate_damage"})],
        )
        await scenario(
            "save_game impact (facade chain, 2 grep hops)",
            "change-impact / call-chain tracing",
            # Round 1: find who serialize() (the real implementation) is
            # called by -- lands on equipment_manager.serialize_equipment.
            # Round 2: find who *that* is called by in turn -- lands on
            # game_manager.save_game. A real agent needs both rounds to
            # answer "what would break if I change serialize()".
            ["serialize", "serialize_equipment"],
            [("impact", {"function_name": "serialize", "direction": "callers", "max_depth": 5})],
        )
        await scenario(
            "apply_critical_hit_multiplier_v2 callers",
            "rare/unique symbol",
            ["apply_critical_hit_multiplier_v2"],
            [("callers", {"function_name": "apply_critical_hit_multiplier_v2"})],
        )
        await scenario(
            "PlayerStats type usages (blind spot)",
            "type/field usage site",
            ["PlayerStats"],
            [("callers", {"function_name": "PlayerStats"})],
        )
        await scenario(
            "log_debug callers (ultra-common helper)",
            "ultra-common generic helper",
            ["log_debug"],
            [("callers", {"function_name": "log_debug"})],
        )

    return rows


def format_table(rows: list[dict]) -> str:
    lines = []
    header = f"{'Scenario':<42} {'MCP tok':>8} {'grep-only':>10} {'grep+min':>9} {'grep+full':>10} {'MCP vs grep+min':>16}"
    lines.append(header)
    lines.append("-" * len(header))
    for r in rows:
        if r["mcp_tokens"] == 0:
            # Zero isn't "infinitely better" -- it means the MCP tool found
            # nothing usable at all (a real blind spot), so a ratio would
            # misleadingly imply a win. Report it as a loss instead.
            ratio_str = "N/A (0 results)"
        else:
            ratio_str = f"{r['grep_minimal_tokens'] / r['mcp_tokens']:.2f}x"
        lines.append(
            f"{r['scenario']:<42} {r['mcp_tokens']:>8} {r['grep_only_tokens']:>10} "
            f"{r['grep_minimal_tokens']:>9} {r['grep_full_tokens']:>10} {ratio_str:>16}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    rows = asyncio.run(run_benchmark())
    print(format_table(rows))
    print()

    latency = measure_latency()
    print(
        f"\nRaw query latency (9 runs, `callers` on a moderately common function):\n"
        f"  DB join query:  avg {latency['db_query_ms_avg']:.3f}ms "
        f"(min {latency['db_query_ms_min']:.3f}, max {latency['db_query_ms_max']:.3f})\n"
        f"  grep -rn:       avg {latency['grep_ms_avg']:.3f}ms "
        f"(min {latency['grep_ms_min']:.3f}, max {latency['grep_ms_max']:.3f})"
    )

    print()
    print(json.dumps({"scenarios": rows, "latency_ms": latency}, indent=2))
