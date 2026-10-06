# sangsik-gdscript-graph

A function-level call-graph indexer for GDScript/Godot projects, exposed as an [MCP](https://modelcontextprotocol.io) server so AI coding assistants can answer questions like "what calls this function?" or "what would break if I change this signal?" without re-reading the whole codebase.

It parses every `.gd` file in a Godot project with [gdtoolkit](https://github.com/Scony/godot-gdscript-toolkit)'s GDScript grammar, resolves calls and signal connections across files (autoloads, `class_name`, inheritance, inner classes, typed fields, lambdas, property accessors, chained signal receivers, etc.), reads the signal connections saved in `.tscn` scenes, and stores the result in a SQLite database that the MCP server queries on demand.

## Features

- **Call resolution** across bare calls, `self.` calls, `super.` calls, autoloads, `class_name`s, `preload` consts, and typed receivers — locals, parameters, and member vars (including inherited ones), typed by annotation (`var menu: PauseMenu`) or by a `:=` whose type is certain (`:= $Menu as PauseMenu`, `:= PauseMenu.new()`, or `:= make_menu()` / `:= repo.find()` through the called function's `-> T` return type), plus calls on a call's result (`Repo.open().find()`), `(expr as T).f()` casts and field chains like `self.hud.menu.open()` — all following inheritance chains. `T.new(...)` and `super(...)` link to the `_init` they run.
- **Engine calls told apart from missed ones**: using the Godot 4.6 API (bundled), a call that doesn't resolve to project code is classified — a GDScript built-in (`int()`, `print()`, `Vector2()`), an engine method inherited by the caller (`queue_free()`), or a call on an engine-typed value (`some_dict.has()`, `get_tree().create_timer()`) — so `status`'s `unresolved_calls` counts only calls the graph may really have missed.
- **Signal handlers are never "dead code"**: a handler connected in the Godot editor (a `[connection]` saved in a `.tscn` — including through instanced, inherited, and editable-children scenes) or with `.connect(...)` in code (to a project signal or an engine one like `$Button.pressed`) shows up in `callers` and `impact`, so an assistant doesn't mistake it for unused code. Project-declared signals are linked on both sides (`GameManager.card_drawn.connect(handler)`, `signal_handlers`).
- **Scoping**: inner classes (with their own `extends`, and `extends Outer.Inner`), property accessors (`get:`/`set(value):`), lambdas, static functions are all tracked with correct scope boundaries.
- **Robust by design**: pathologically deep GDScript files are isolated (a parse/recursion failure in one file doesn't abort the whole build), builds are atomic (a failed rebuild never corrupts an existing database) and never overlap — not within one server, nor across servers or a CLI build sharing a database — and known ReDoS-prone `.tscn` parsing paths are hardened. A string with a line break in it (valid Godot, rejected by gdtoolkit's grammar) no longer drops its file, and a file the parser still rejects keeps its `class_name`/`extends`/functions/signals from a line scan.
- **Sees the project the way Godot does**: hidden directories (`.godot`, `.git`, ...) and ones with a `.gdignore` are skipped, and a symlinked addon is indexed under its `res://addons/...` path.
- **Transitive impact analysis**: BFS over the call graph to answer "everything that calls (or is called by) this function, up to N hops."
- **Auto-sync while the server runs**: an OS-level file watcher (FSEvents/inotify/ReadDirectoryChangesW) rebuilds the graph automatically a short debounce window after you edit a `.gd`/`.tscn`/`project.godot` file — no manual rebuild needed during a normal editing session.
- **Incremental rebuilds**: each build caches every file's extraction (symbols, calls, local types — keyed by content hash); a rebuild re-extracts only what changed and re-resolves everything, with output identical to a full rebuild either way. A file whose modification time and size are unchanged isn't even re-read. On a 2,127-file project a rebuild with nothing changed takes ~3 s (down from ~19 s with the previous parse-tree cache), peak memory ~200 MB (from ~1.1 GB), and the database is 23 MB (from 132 MB).
- **Reconciles offline edits on startup**: a live file watcher only sees changes made after it starts, so every server start checks whether any `.gd`/`.tscn`/`project.godot` file changed (modification time and size) since the database was built — ~1.3 s on the same project — and rebuilds in the background only if one did.
- **One rebuilding server per database**: several MCP servers on one database (one per editor session) elect a single leader that watches and rebuilds; the others just serve queries and take over if it exits.
- **Bounded responses**: list results come one page at a time (50 by default), grouped by file, with `total`/`next_offset` — a helper with 2,000+ call sites no longer overflows an MCP client's response limit. Any result produced while a rebuild is pending says `stale: true`.

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

The MCP response payload isn't reliably smaller than `grep`'s raw output for a full listing — `callers` pages large results (50 call sites per call, grouped by file), which keeps one response bounded but means the `log_debug` row below is the first page, not everything. The real savings come from three separate mechanisms: avoiding full-file reads (largest effect — a `file:line` result needs 10-20 targeted lines instead of a whole file), fewer round trips (a multi-hop question resolves in one call instead of repeated grep→read→re-grep cycles), and precision (no false positives from comments, strings, or similarly-named symbols).

### Measured results

| Scenario | Task type | MCP tokens | grep-only | grep + minimal read | grep + full-file read |
|---|---|---|---|---|---|
| `calculate_damage` callers | Moderately common function | 190 | 509 | 1,152 | 2,911 |
| `save_game` impact (2-hop facade chain) | Change-impact / call-chain tracing | 106 | 342 | 620 | 5,624 |
| `apply_critical_hit_multiplier_v2` callers | Rare, uniquely-named symbol | 55 | 79 | 190 | 636 |
| `PlayerStats` type usages | Type/field usage site | **finds nothing** (22: an empty result) | 199 | 374 | 1,822 |
| `log_debug` callers | Ultra-common generic helper | 1,087 (first 50 of 129) | 5,016 | 10,450 | 8,850 |

Raw query latency (9-run average, backend DB query vs. `grep -rn`, same fixture): **~0.05ms vs. ~5.2ms (~100x)** — though in practice, round-trip count and tokens processed dominate an agent's perceived speed far more than raw query time.

**Reading this honestly:**
- The facade-chain scenario is the biggest win (5.8x vs. a disciplined `grep`, up to 53x vs. a `grep`-and-read-the-whole-file agent) — `grep` genuinely cannot see through one manager delegating to another without reading the call site's body.
- The rare-symbol case still wins here (3.5x), but it's the *smallest* margin among the winning scenarios — the more disciplined the `grep`-based approach is, the more this gap narrows. (The response envelope — `total`, `next_offset`, grouping — costs a few tokens on a tiny result.)
- The type-usage scenario is a real, unambiguous loss: `callers("PlayerStats")` returns **zero results**, because the call graph only tracks calls and signal connections, not type annotations. `grep` is the only thing that works for "where is this class used as a type?"
- The ultra-common-helper row isn't like-for-like: the 1,087 tokens are the first page (50 of 129 call sites, grouped by file). Paging through all 129 costs 2,897 tokens — a bit over half of raw `grep` output, since each file path is sent once per page rather than once per call site.

### Honest limitations

- **Type/field usage sites are invisible to the call graph** — confirmed above, not just a hypothetical. `grep` is strictly better for "where is this type referenced?"
- **Reading and modifying a function's own body costs the same either way** — these tools only cut cost at the locate-and-trace stage.
- **Every match of an extremely common name takes several pages** — narrowing with `file`/`scope`, or asking a more specific question, matters once a name has hundreds of call sites.
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
| `search(query, limit=20, kind=None, path=None)` | Substring search over functions, signals, vars, consts, and enums — exact matches first, then prefix matches; `kind`/`path` narrow it. |
| `status()` | Index health/freshness: file/symbol/call/signal/scene-connection counts (unresolved calls split from calls into the engine), last build time, whether a rebuild is pending or in flight, and this server's `watch_role`. |
| `node(name, file=None, scope=None, kind=None, callers_limit=20)` | A symbol's verbatim source (re-read fresh from disk) plus its first callers/handlers, in one call. |
| `explore(names, max_path_depth=6, callers_limit=10)` | `node`'s detail for several symbols at once, plus the actual call path connecting each resolved pair, if one exists. |
| `files(prefix=None, limit=50, offset=0)` | List indexed files with `class_name`/`extends`/`parse_error` and a symbol-count breakdown; `prefix` narrows to a subdirectory or file, and a partial listing counts files per subdirectory. |
| `callers(function_name, file=None, scope=None, limit=50, offset=0)` | List call sites of the given function, plus its signal-handler registrations (`via: "connect"` for `.connect()` in code, `via: "scene"` for a `.tscn` connection). |
| `callees(function_name, file=None, scope=None, limit=50, offset=0)` | List functions called from within the given function, plus handlers it `.connect()`s (`via: "connect"`). |
| `signal_handlers(signal_name, file=None, scope=None, limit=50, offset=0)` | List handlers connected to a signal, via `.connect(...)` or in a `.tscn` scene. |
| `impact(function_name, file=None, scope=None, direction="callers", max_depth=5, limit=50, offset=0)` | Transitively walk the call graph (signal registrations included) to find everything affected by changing a function, nearest first. |

`file`/`scope` disambiguate when multiple declarations share a name (same-named function in different files or inner classes). `callers`/`callees` merge the results of every matching declaration, marking which one each entry belongs to; `impact` instead lists the candidates when the name is declared in more than one file. List results are `{total, next_offset, by_file}` pages — pass `offset=next_offset` for the next one.

### What it can't see

Resolution is static and deliberately conservative — a call it can't type stays *unresolved* (and is recorded as such) rather than guessed, so a missing edge is possible but a wrong one shouldn't be:

- **Untyped variables**: `var x = PauseMenu.new()` (no annotation, no `:=`) is Variant and can be reassigned, so calls through it aren't resolved. Neither are calls through a function with no `-> T` return type, `$Node` / `get_node()` without an `as` cast or typed variable, or an array element (`items[0].use()`).
- **Engine types and dynamic dispatch**: calls on engine classes (`Timer`, `Node`...) aren't project code (they're counted as `engine_calls`); a call through a base-class-typed variable resolves to the base's method, not to subclass overrides.
- **Scenes**: built-in (embedded) scripts and C# scripts aren't indexed, and binary `.scn` scenes aren't read. Godot 3's string-based `connect("signal", target, "method")` isn't tracked.
- **Type usages**: `callers("SomeClass")` doesn't list where a class is used as a type — `grep` is better for that (see Performance).

## Development

```bash
pip install -e . pytest
pytest -q
```

CI runs the suite on Linux and macOS (Python 3.10 and 3.13), plus once against the oldest supported dependency versions. See [CHANGELOG.md](CHANGELOG.md) for release notes.

The MCP server re-reads the DB file fresh on every query, so any rebuild while the server is running (whether triggered by the file watcher or run manually) is picked up immediately without a restart.
