# Changelog

All notable changes to this project are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

