# sangsik-gdscript-graph

A function-level call-graph indexer for GDScript/Godot projects, exposed as an [MCP](https://modelcontextprotocol.io) server so AI coding assistants can answer questions like "what calls this function?" or "what would break if I change this signal?" without re-reading the whole codebase.

It parses every `.gd` file in a Godot project with [gdtoolkit](https://github.com/Scony/godot-gdscript-toolkit)'s GDScript grammar, resolves calls and signal connections across files (autoloads, `class_name`, inheritance, inner classes, typed fields, lambdas, property accessors, chained signal receivers, etc.), reads the signal connections saved in `.tscn` scenes, and stores the result in a SQLite database that the MCP server queries on demand.

## Features

- **Call resolution** across bare calls, `self.` calls, `super.` calls, autoloads, `class_name`s, `preload` consts, and typed receivers — locals, parameters, and member vars (including inherited ones), typed by annotation (`var menu: PauseMenu`) or by a `:=` whose type is certain (`:= $Menu as PauseMenu`, `:= PauseMenu.new()`), plus `(expr as T).f()` casts and field chains like `self.hud.menu.open()` — all following inheritance chains.
- **Signal handlers are never "dead code"**: a handler connected in the Godot editor (a `[connection]` saved in a `.tscn` — including through instanced, inherited, and editable-children scenes) or with `.connect(...)` in code (to a project signal or an engine one like `$Button.pressed`) shows up in `callers` and `impact`, so an assistant doesn't mistake it for unused code. Project-declared signals are linked on both sides (`GameManager.card_drawn.connect(handler)`, `signal_handlers`).
- **Scoping**: inner classes, property accessors (`get:`/`set(value):`), lambdas, static functions are all tracked with correct scope boundaries.
- **Robust by design**: pathologically deep GDScript files are isolated (a parse/recursion failure in one file doesn't abort the whole build), builds are atomic (a failed rebuild never corrupts an existing database), and known ReDoS-prone `.tscn` parsing paths are hardened.
- **Transitive impact analysis**: BFS over the call graph to answer "everything that calls (or is called by) this function, up to N hops."
- **Auto-sync while the server runs**: an OS-level file watcher (FSEvents/inotify/ReadDirectoryChangesW) rebuilds the graph automatically a short debounce window after you edit a `.gd`/`.tscn`/`project.godot` file — no manual rebuild needed during a normal editing session.
- **Incremental rebuilds**: each build caches every file's parsed tree (keyed by content hash); a rebuild reuses the cache for every unchanged file and only re-parses what actually changed, with output identical to a full rebuild either way. On a 433-file test project this cut rebuild time by ~3x overall (parsing itself, which dominates build time, got ~4.5x faster).
- **Reconciles offline edits on startup**: a live file watcher only sees changes made after it starts, so every server start also runs one reconciliation rebuild in the background (cheap thanks to the incremental cache) to catch up on anything edited while no server was running — e.g. an MCP client spawning a fresh server each session after you edited the project in the Godot editor in between.

## Performance

[`benchmarks/`](benchmarks/) has a reproducible benchmark comparing the MCP tools against `grep`-based exploration, run against a small purpose-built fixture project (not a real game — a synthetic one deliberately shaped to include both the strong *and* weak cases for this tool, e.g. a fact it can't see at all). Reproduce it yourself:

```bash
pip install -e . tiktoken
.venv/bin/python benchmarks/generate_fixture.py
.venv/bin/python -m gdscript_graph.cli build benchmarks/fixture_project -o benchmarks/fixture_project.db
.venv/bin/python benchmarks/compare_mcp_vs_grep.py
```

Methodology: MCP-side numbers are the actual bytes returned by a real MCP stdio session calling the real tools (what an agent's context would actually receive). `grep`-side numbers use the real `grep` binary in three postures: raw output only, output plus a few lines of context read around each match (disciplined), and output plus the whole containing file read for each match (a common, less disciplined agent default). Token counts use `tiktoken`'s `cl100k_base` encoding — not Claude's exact tokenizer (not public), but a reproducible, order-of-magnitude-accurate stand-in.

### Where the savings actually come from

The MCP response payload isn't reliably smaller than `grep`'s raw output — see the `log_debug` row below, where it's larger. The real savings come from three separate mechanisms: avoiding full-file reads (largest effect — a `file:line` result needs 10-20 targeted lines instead of a whole file), fewer round trips (a multi-hop question resolves in one call instead of repeated grep→read→re-grep cycles), and precision (no false positives from comments, strings, or similarly-named symbols).

### Measured results

| Scenario | Task type | MCP tokens | grep-only | grep + minimal read | grep + full-file read |
|---|---|---|---|---|---|
| `calculate_damage` callers | Moderately common function | 206 | 509 | 1,152 | 2,911 |
| `save_game` impact (2-hop facade chain) | Change-impact / call-chain tracing | 92 | 342 | 620 | 5,624 |
| `apply_critical_hit_multiplier_v2` callers | Rare, uniquely-named symbol | 39 | 79 | 190 | 636 |
| `PlayerStats` type usages | Type/field usage site | **0 — finds nothing** | 199 | 374 | 1,822 |
| `log_debug` callers | Ultra-common generic helper | 5,413 | 5,016 | 10,450 | 8,850 |

Raw query latency (9-run average, backend DB query vs. `grep -rn`, same fixture): **~0.05ms vs. ~5.2ms (~100x)** — though in practice, round-trip count and tokens processed dominate an agent's perceived speed far more than raw query time.

**Reading this honestly:**
- The facade-chain scenario is the biggest win (6.7x vs. a disciplined `grep`, up to 61x vs. a `grep`-and-read-the-whole-file agent) — `grep` genuinely cannot see through one manager delegating to another without reading the call site's body.
- The rare-symbol case still wins here (4.9x), but it's the *smallest* margin among the winning scenarios — the more disciplined the `grep`-based approach is, the more this gap narrows.
- The type-usage scenario is a real, unambiguous loss: `callers("PlayerStats")` returns **zero results**, because the call graph only tracks calls and signal connections, not type annotations. `grep` is the only thing that works for "where is this class used as a type?"
- The ultra-common-helper case is the weakest win, and by one measure (vs. the leanest possible `grep`, no reads at all) it's actually a **narrow loss**: 5,413 tokens for the full structured dump of 129 call sites vs. 5,016 for raw `grep` output. Once results run into the hundreds, per-entry JSON field-name overhead adds up.

### Honest limitations

- **Type/field usage sites are invisible to the call graph** — confirmed above, not just a hypothetical. `grep` is strictly better for "where is this type referenced?"
- **Reading and modifying a function's own body costs the same either way** — these tools only cut cost at the locate-and-trace stage.
- **Dumping every match of an extremely common name has real overhead** — narrowing with `file`/`scope`, or asking a more specific question, matters once a name has hundreds of call sites.
- **The index depends on freshness** — a change needs a rebuild to show up. The file watcher (see Features) makes this automatic, but there's still the debounce window's worth of latency before an edit is reflected.

One fixture project, one moment in time — treat these as illustrative of the mechanism (and reproducible, unlike a one-off number), not a guaranteed multiplier for any given codebase.

## Installation

```bash
pip install -e .
```

Requires Python 3.10+.

## Usage

### 1. Build the graph

```bash
gdscript-graph build /path/to/godot/project -o graph.db
```

This scans every `.gd` file (plus `project.godot` for autoloads, and every `.tscn` scene for editor-made signal connections and autoload scenes), resolves calls and signal connections, and writes a SQLite database to `graph.db` (defaults to `<project>/.gdscript_graph.db` if `-o` is omitted). A database built by an older version is rejected by the server with a message to rebuild it.

### 2. Run the MCP server

```bash
gdscript-graph mcp graph.db
```

This starts an MCP server over stdio and, by default, watches the project directory (recorded in the database at build time) for changes -- editing a `.gd`/`.tscn`/`project.godot` file triggers an automatic rebuild ~2 seconds after your last edit, with no restart needed. Pass `--no-watch` to disable this, or `--debounce-ms <n>` to change the delay.

Point your MCP client (Claude Code, Claude Desktop, etc.) at this command, e.g. in a Claude Code MCP config:

```json
{
  "mcpServers": {
    "gdscript-graph": {
      "command": "gdscript-graph",
      "args": ["mcp", "/path/to/graph.db"]
    }
  }
}
```

### Available MCP tools

| Tool | Description |
|---|---|
| `search(query, limit=20)` | Substring search over functions, signals, vars, consts, and enums. |
| `status()` | Index health/freshness: file/symbol/call/signal/scene-connection counts, last build time, and whether a watcher rebuild is currently pending or in flight. |
| `node(name, file=None, scope=None, kind=None)` | A symbol's verbatim source (re-read fresh from disk) plus its callers/handlers, in one call. |
| `explore(names, max_path_depth=6)` | `node`'s detail for several symbols at once, plus the actual call path connecting each resolved pair, if one exists. |
| `files(prefix=None)` | List indexed files with `class_name`/`extends`/`parse_error` and a symbol-count breakdown; `prefix` narrows to a subdirectory or file. |
| `callers(function_name, file=None, scope=None)` | List call sites of the given function, plus its signal-handler registrations (`via: "connect"` for `.connect()` in code, `via: "scene"` for a `.tscn` connection). |
| `callees(function_name, file=None, scope=None)` | List functions called from within the given function, plus handlers it `.connect()`s (`via: "connect"`). |
| `signal_handlers(signal_name, file=None, scope=None)` | List handlers connected to a signal, via `.connect(...)` or in a `.tscn` scene. |
| `impact(function_name, file=None, scope=None, direction="callers", max_depth=5)` | Transitively walk the call graph (signal registrations included) to find everything affected by changing a function. |

`file`/`scope` disambiguate when multiple declarations share a name (same-named function in different files or inner classes).

### What it can't see

Resolution is static and deliberately conservative — a call it can't type stays *unresolved* (and is recorded as such) rather than guessed, so a missing edge is possible but a wrong one shouldn't be:

- **Untyped variables**: `var x = PauseMenu.new()` (no annotation, no `:=`) is Variant and can be reassigned, so calls through it aren't resolved. Neither are calls through a function's return value, or `$Node` / `get_node()` without an `as` cast or typed variable.
- **Engine types and dynamic dispatch**: calls on engine classes (`Timer`, `Node`...) aren't project code; a call through a base-class-typed variable resolves to the base's method, not to subclass overrides.
- **Scenes**: built-in (embedded) scripts and C# scripts aren't indexed, and binary `.scn` scenes aren't read. Godot 3's string-based `connect("signal", target, "method")` isn't tracked.
- **Type usages**: `callers("SomeClass")` doesn't list where a class is used as a type — `grep` is better for that (see Performance).

## Development

```bash
pip install -e . pytest
pytest -q
```

CI runs the suite on Linux and macOS (Python 3.10 and 3.13), plus once against the oldest supported dependency versions. See [CHANGELOG.md](CHANGELOG.md) for release notes.

The MCP server re-reads the DB file fresh on every query, so any rebuild while the server is running (whether triggered by the file watcher or run manually) is picked up immediately without a restart.
