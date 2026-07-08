# sangsik-gdscript-graph

A function-level call-graph indexer for GDScript/Godot projects, exposed as an [MCP](https://modelcontextprotocol.io) server so AI coding assistants can answer questions like "what calls this function?" or "what would break if I change this signal?" without re-reading the whole codebase.

It parses every `.gd` file in a Godot project with [gdtoolkit](https://github.com/Scony/godot-gdscript-toolkit)'s GDScript grammar, resolves calls and signal connections across files (autoloads, `class_name`, inheritance, inner classes, lambdas, property accessors, chained signal receivers, etc.), and stores the result in a SQLite database that the MCP server queries on demand.

## Features

- **Call resolution** across bare calls, `self.` calls, `super.` calls, autoloads, `class_name`-typed receivers, and locally-typed variables — including inheritance chains.
- **Signal resolution** for `.connect(...)` on both the signal side and the handler side: bare/self/inherited signals, and chained receivers (`GameManager.card_drawn.connect(handler)`).
- **Scoping**: inner classes, property accessors (`get:`/`set(value):`), lambdas, static functions are all tracked with correct scope boundaries.
- **Robust by design**: pathologically deep GDScript files are isolated (a parse/recursion failure in one file doesn't abort the whole build), builds are atomic (a failed rebuild never corrupts an existing database), and known ReDoS-prone `.tscn` parsing paths are hardened.
- **Transitive impact analysis**: BFS over the call graph to answer "everything that calls (or is called by) this function, up to N hops."
- **Auto-sync while the server runs**: an OS-level file watcher (FSEvents/inotify/ReadDirectoryChangesW) rebuilds the graph automatically a short debounce window after you edit a `.gd`/`.tscn`/`project.godot` file — no manual rebuild needed during a normal editing session.
- **Incremental rebuilds**: each build caches every file's parsed tree (keyed by content hash); a rebuild reuses the cache for every unchanged file and only re-parses what actually changed, with output identical to a full rebuild either way. On a 433-file test project this cut rebuild time by ~3x overall (parsing itself, which dominates build time, got ~4.5x faster).
- **Reconciles offline edits on startup**: a live file watcher only sees changes made after it starts, so every server start also runs one reconciliation rebuild in the background (cheap thanks to the incremental cache) to catch up on anything edited while no server was running — e.g. an MCP client spawning a fresh server each session after you edited the project in the Godot editor in between.

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

This scans every `.gd` file (plus `project.godot` for autoloads and `.tscn` files for autoload scenes), resolves calls and signal connections, and writes a SQLite database to `graph.db` (defaults to `<project>/.gdscript_graph.db` if `-o` is omitted).

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
| `status()` | Index health/freshness: file/symbol/call/signal counts, last build time, and whether a watcher rebuild is currently pending or in flight. |
| `node(name, file=None, scope=None, kind=None)` | A symbol's verbatim source (re-read fresh from disk) plus its callers/handlers, in one call. |
| `explore(names, max_path_depth=6)` | `node`'s detail for several symbols at once, plus the actual call path connecting each resolved pair, if one exists. |
| `files(prefix=None)` | List indexed files with `class_name`/`extends`/`parse_error` and a symbol-count breakdown; `prefix` narrows to a subdirectory or file. |
| `callers(function_name, file=None, scope=None)` | List call sites that call the given function. |
| `callees(function_name, file=None, scope=None)` | List functions called from within the given function. |
| `signal_handlers(signal_name, file=None, scope=None)` | List handlers connected to a signal via `.connect(...)`. |
| `impact(function_name, file=None, scope=None, direction="callers", max_depth=5)` | Transitively walk the call graph to find everything affected by changing a function. |

`file`/`scope` disambiguate when multiple declarations share a name (same-named function in different files or inner classes).

## Development

```bash
pip install -e . pytest
pytest -q
```

The MCP server re-reads the DB file fresh on every query, so any rebuild while the server is running (whether triggered by the file watcher or run manually) is picked up immediately without a restart.
