# Changelog

All notable changes to this project are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

Measured on a 2,127-file Godot 4 project throughout.

### Fixed

- **Overlapping rebuilds no longer empty the database.** A save during a rebuild started a second rebuild alongside it; both wrote the same temp file, and the first to finish swapped the other's still-empty database into place — `callers` returned `[]`, with no error, for the length of a rebuild. Rebuilds now run one at a time on one worker thread (edits during one add exactly one more), each in its own temp file, and builds of one database exclude each other across processes (other servers, a CLI build).
- **Only one server per database rebuilds.** Every editor session started its own server, and every save made each of them rebuild the same database. Servers now elect a leader (a lock next to the database) that watches and rebuilds; the others serve queries and take over when it exits, crashes included. `status` reports `watch_role`.
- **Temp files left by interrupted builds are cleaned up** (34 had piled up in one project).
- **Responses are bounded.** List tools returned every row: a common helper's ~2,300 call sites came to ~100k tokens, past what MCP clients accept (Claude Code truncates around 25k). `callers`, `callees`, `signal_handlers`, `impact` and `files` now return a page — `{total, next_offset, by_file}`, 50 entries by default, grouped by file — and `node`/`explore` inline only the first callers. `impact` on a name declared in several files (e.g. `_ready`) lists the candidates instead of merging every walk; `callers`/`callees` mark which declaration each merged entry belongs to.
- **A line break inside a string no longer drops the file.** Godot accepts one in an ordinary `"..."` string; gdtoolkit's grammar didn't, so the whole file — 13 in that project — went unindexed. A file the parser still rejects keeps its `class_name`, `extends`, functions and signals, from a line scan.
- **Discovery follows Godot's rules**: hidden directories (`.git`, `.godot` — over 1 GB there) and directories with a `.gdignore` or a nested `project.godot` are skipped, and a symlinked addon is indexed under its `res://addons/...` path rather than its hidden real location.
- `search` ranks an exact name first, then prefix matches — `get` no longer sinks below every `get_*`.

### Added

- Calls through a variable typed by a function's return value (`var repo := LootRepo.open()`, `var o := repo.orchestrator()`, `await`ed too) and calls on a call's result (`LootRepo.open().find()`, `assert_bool(x).is_true()`) resolve, via the called function's `-> T`. `T.new(...)` and `super(...)` link to the `_init` they run. Resolved calls in that project went from 62,197 to 84,400, and calls to a sample of functions found by `grep` from 93.9% to 98.6% resolved.
- Calls into the engine are told apart from missed ones, using the bundled Godot 4.6 API (`scripts/generate_godot_api.py` regenerates it): an unresolved call's reason is `builtin_function`, `engine_method`, `engine_receiver` or `not_a_project_function` when it's into the engine or GDScript itself, leaving `unknown_receiver` / `method_not_found_in_target` for calls that may really be missed. `status`'s `unresolved_calls` counts only the latter (85,821 → 4,271 in that project), next to `engine_calls` and a per-reason breakdown.
- Inner classes' own `extends` (`class Fancy extends Panel:`, or `extends X` as the class's first statement) and a script's `extends Outer.Inner` are followed: inherited methods, `super.`/`super()`, inherited signals and members resolve, and engine methods of an inner class's base (`draw_rect()` in a Control) are recognized.
- Results answered while a rebuild is pending or running carry `stale: true`.
- `search` takes `kind` and `path` filters and reports `total`.

### Changed

- **Rebuilds cache each file's extraction instead of its parse tree**, and skip reading a file whose modification time and size are unchanged. A rebuild with nothing changed went from ~19 s to ~3 s, peak memory from ~1.1 GB to ~200 MB, and the database from 132 MB to 23 MB. A cache written by a different version of the extractor is ignored.
- **A server start rebuilds only if something changed** since the database was built (any `.gd`/`.tscn`/`project.godot` file's modification time or size, or the gdscript-graph version) — a ~1.3 s check instead of an unconditional rebuild.
- List tools return a page object instead of a bare list (see Fixed), and entries drop keys whose value is null.

## [0.2.0] - 2026-10-02

### Fixed

- **Installing now works out of the box.** The `mcp` dependency was unpinned (`mcp>=1.0.0`), so a fresh install pulled mcp 2.x, which removed `FastMCP` — the server failed to start at all. It's now `mcp>=1.14.0,<2` (releases before 1.14 also fail to start this server).
- **No more endless rebuilds on Linux.** inotify reports plain file opens and read-only closes, and every rebuild reads every `.gd` file — so each rebuild triggered the next, forever, with nothing edited (15–16 rebuilds in 5 seconds). The watcher now only reacts to create/modify/delete/move events.
- **Signal handlers no longer look like dead code.** A handler connected in the Godot editor (a `[connection]` saved in a `.tscn`) was invisible to the index, and so was one connected in code to a signal that isn't project-declared (`$Button.pressed.connect(...)`, `timer.timeout.connect(...)`, `tree_exited.connect(...)`) — both showed no callers. Across the 139 projects in godot-demo-projects, 513 of the 555 functions named as a `.tscn` connection's handler showed no callers; now 3 do (all called only through a `Node`-typed variable, which static resolution can't follow).
- `gdtoolkit` lower bound raised to 4.5.0: 4.3.x can't parse Godot 4.5 abstract methods, and 4.3.0 doesn't import at all without `setuptools`.

### Added

- `.tscn` scene connections are indexed: the handler is resolved on the target node's script (following instanced scenes, inherited scenes, and editable children; Godot 3 and 4 formats), and the signal is linked when it's declared on the emitting node's script. Unresolvable ones (C#/built-in scripts, missing methods) are recorded with a reason.
- `callers`, `node`, and `impact` include signal-handler registrations, marked `via: "connect"` (a `.connect()` in code) or `via: "scene"` (a `.tscn` connection, with `signal`/`from_node`/`to_node`); `callees` includes the handlers a function `.connect()`s. Direct-call rows are unchanged.
- `signal_handlers` includes scene connections, and — without a `file`/`scope` filter — connections to engine signals by name (e.g. `pressed`). Each row now has `connected_in`.
- Type-aware resolution through member vars (explicitly typed, or `:=` with `as T` / `T.new()` / `preload(...)`), inherited members, `(expr as T)` casts, field chains (`self.hud.menu.open()`), `const X = preload("x.gd")` aliases, and inner-class types. Calls through untyped variables are still deliberately left unresolved. Resolved calls across godot-demo-projects went from 1,297 to 1,416, with no previously resolved call lost.
- `status` reports resolved/unresolved scene connection counts; `gdscript-graph build` prints them.
- CI (Linux + macOS, Python 3.10/3.13, plus oldest supported dependencies) and a tag-triggered PyPI publishing workflow.

### Changed

- The database schema changed (new `scene_connections`/`unresolved_scene_connections` tables; `signal_connections` gained `signal_name` and a nullable `signal_symbol_id`). Rebuild existing databases with `gdscript-graph build`; the server rejects an old one with a message saying so.
- A `.connect()` whose signal can't be linked to a project declaration is now recorded as a connection with no signal symbol instead of an unresolved connection — the handler registration is real either way. `unresolved_connections` now only holds connections whose handler can't be resolved.

## [0.1.0] - 2026-07-08

Initial release: function-level call graph for GDScript (calls, `.connect()` signal connections, inheritance, inner classes, autoloads, `class_name`, typed locals) in SQLite, served over MCP (`search`, `status`, `node`, `explore`, `files`, `callers`, `callees`, `signal_handlers`, `impact`), with a file watcher for auto-rebuilds and an incremental parse cache.

