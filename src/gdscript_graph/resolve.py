from __future__ import annotations

from dataclasses import dataclass

from gdscript_graph.calls import RawCall, RawConnection, Receiver
from gdscript_graph.scenes import Scene, SceneIndex
from gdscript_graph.symbols import FileSymbols

# A resolved static type: (res:// script path, inner class scope), where
# scope None means the script's own top-level class.
TypeRef = tuple[str, "str | None"]


@dataclass
class ResolvedCall:
    source_res_path: str
    source_scope: str | None
    source_function: str
    target_res_path: str
    target_scope: str | None
    target_function: str
    line: int


@dataclass
class UnresolvedCall:
    source_res_path: str
    source_scope: str | None
    source_function: str
    receiver: str | None
    called_name: str
    line: int
    reason: str  # "unknown_receiver" | "method_not_found_in_target"


@dataclass
class ResolvedConnection:
    source_res_path: str
    source_scope: str | None
    source_function: str
    # Where the signal is declared, when it's a project-declared signal;
    # None for an engine built-in (e.g. `$Button.pressed`, a bare
    # `tree_exited`) or a receiver whose type isn't known -- the handler
    # registration is real (and recorded) either way.
    signal_res_path: str | None
    signal_scope: str | None  # the signal's own scope -- equal to source_scope for a bare/
                               # self/inherited reference, else the receiver type's scope
    signal_name: str
    handler_res_path: str
    handler_scope: str | None
    handler_function: str
    line: int


@dataclass
class UnresolvedConnection:
    source_res_path: str
    source_function: str
    signal_receiver: str | None  # None = bare/self/inherited reference
    signal_name: str
    handler_receiver: str | None
    handler_name: str | None  # None = handler argument shape couldn't be parsed
    line: int
    reason: str  # "unknown_receiver" | "method_not_found_in_target" | "unsupported_handler_shape"


@dataclass
class ResolvedSceneConnection:
    scene_res_path: str
    line: int
    signal_name: str
    from_node: str
    to_node: str
    # The signal's declaration on the emitting node's script, when it's a
    # project-declared signal; None for an engine built-in signal (e.g.
    # `Button.pressed`) or one that can't be found.
    signal_res_path: str | None
    handler_res_path: str
    handler_function: str


@dataclass
class UnresolvedSceneConnection:
    scene_res_path: str
    line: int
    signal_name: str
    from_node: str
    to_node: str
    method: str
    reason: str  # "unknown_target_script" | "method_not_found_in_target"


def build_class_name_table(all_symbols: list[FileSymbols]) -> dict[str, str]:
    """Map declared class_name -> res:// path. Later files win on collision."""
    table: dict[str, str] = {}
    for fs in all_symbols:
        if fs.class_name:
            table[fs.class_name] = fs.res_path
    return table


def build_function_index(all_symbols: list[FileSymbols]) -> dict[tuple[str, str | None], set[str]]:
    """Map (res_path, scope) -> function names declared directly in that
    scope. `scope=None` means top-level (file-level) functions."""
    index: dict[tuple[str, str | None], set[str]] = {}
    for fs in all_symbols:
        for func in fs.functions:
            index.setdefault((fs.res_path, func.scope), set()).add(func.name)
    return index


def build_signal_index(all_symbols: list[FileSymbols]) -> dict[tuple[str, str | None], set[str]]:
    """Map (res_path, scope) -> signal names declared directly in that
    scope. `scope=None` means top-level (file-level) signals. Mirrors
    `build_function_index`, used to resolve a signal accessed through a
    chained receiver (`<receiver>.signal_name.connect(...)`) the same way
    a chained method call is resolved."""
    index: dict[tuple[str, str | None], set[str]] = {}
    for fs in all_symbols:
        for sig in fs.signals:
            index.setdefault((fs.res_path, sig.scope), set()).add(sig.name)
    return index


def build_inheritance_map(
    all_symbols: list[FileSymbols], class_name_table: dict[str, str]
) -> dict[str, str | None]:
    """Map res_path -> parent res_path, resolved from each file's top-level
    ``extends`` (a res:// path, a declared class_name, or an unresolvable
    engine built-in -> None). Inner classes' own ``extends`` isn't tracked."""
    known_paths = {fs.res_path for fs in all_symbols}
    parents: dict[str, str | None] = {}
    for fs in all_symbols:
        extends = fs.extends
        if extends is None:
            parents[fs.res_path] = None
        elif extends.startswith("res://"):
            parents[fs.res_path] = extends if extends in known_paths else None
        else:
            parents[fs.res_path] = class_name_table.get(extends)
    return parents


def find_function_in_chain(
    start_path: str | None,
    name: str,
    function_index: dict[tuple[str, str | None], set[str]],
    inheritance_map: dict[str, str | None],
) -> str | None:
    """Search for a top-level function `name` starting at `start_path`,
    then walking up the ``extends`` chain. Guards against cycles."""
    visited: set[str] = set()
    path = start_path
    while path is not None and path not in visited:
        visited.add(path)
        if name in function_index.get((path, None), ()):
            return path
        path = inheritance_map.get(path)
    return None


def find_signal_in_chain(
    start_path: str | None,
    name: str,
    signal_index: dict[tuple[str, str | None], set[str]],
    inheritance_map: dict[str, str | None],
) -> str | None:
    """Search for a top-level signal `name` starting at `start_path`, then
    walking up the ``extends`` chain. Guards against cycles. Mirrors
    `find_function_in_chain`, used for a signal accessed through a chained
    receiver (`<receiver>.signal_name.connect(...)`)."""
    visited: set[str] = set()
    path = start_path
    while path is not None and path not in visited:
        visited.add(path)
        if name in signal_index.get((path, None), ()):
            return path
        path = inheritance_map.get(path)
    return None


@dataclass
class ProjectIndex:
    """Every project-wide table resolution needs, built once per build."""
    class_name_table: dict[str, str]
    autoloads: dict[str, str]
    function_index: dict[tuple[str, str | None], set[str]]
    signal_index: dict[tuple[str, str | None], set[str]]
    inheritance_map: dict[str, str | None]
    # (res_path, scope) -> member var/const name -> its raw static type (a
    # type name as written in that file, a res:// path, or None) -- see
    # `FieldSymbol.type_name`.
    member_types: dict[tuple[str, str | None], dict[str, str | None]]
    # (res_path, scope) -> names of the consts among those members, the
    # only members that can stand in for a type (`const E = preload(...)`).
    const_names: dict[tuple[str, str | None], set[str]]
    # Every (res_path, inner class scope) that declares anything.
    inner_classes: set[tuple[str, str]]
    known_paths: set[str]


def build_project_index(all_symbols: list[FileSymbols], autoloads: dict[str, str]) -> ProjectIndex:
    class_name_table = build_class_name_table(all_symbols)
    member_types: dict[tuple[str, str | None], dict[str, str | None]] = {}
    const_names: dict[tuple[str, str | None], set[str]] = {}
    inner_classes: set[tuple[str, str]] = set()
    for fs in all_symbols:
        for fld in fs.fields:
            # First declaration wins, same as symbol ids in db.py.
            member_types.setdefault((fs.res_path, fld.scope), {}).setdefault(fld.name, fld.type_name)
            if fld.kind == "const":
                const_names.setdefault((fs.res_path, fld.scope), set()).add(fld.name)
        for decl in (*fs.functions, *fs.signals, *fs.fields, *fs.enums):
            scope = decl.scope
            while scope:
                inner_classes.add((fs.res_path, scope))
                scope = scope.rpartition(".")[0] or None
    return ProjectIndex(
        class_name_table=class_name_table,
        autoloads=autoloads,
        function_index=build_function_index(all_symbols),
        signal_index=build_signal_index(all_symbols),
        inheritance_map=build_inheritance_map(all_symbols, class_name_table),
        member_types=member_types,
        const_names=const_names,
        inner_classes=inner_classes,
        known_paths={fs.res_path for fs in all_symbols},
    )


def _enclosing_scopes(scope: str | None) -> list[str | None]:
    """`scope` and every scope enclosing it, innermost first, ending with
    None (top level): "A.B" -> ["A.B", "A", None]."""
    scopes: list[str | None] = []
    while scope:
        scopes.append(scope)
        scope = scope.rpartition(".")[0] or None
    scopes.append(None)
    return scopes


def _ancestor_paths(index: ProjectIndex, path: str | None) -> list[str]:
    """`path` and its top-level `extends` ancestors, nearest first."""
    paths: list[str] = []
    while path is not None and path not in paths:
        paths.append(path)
        path = index.inheritance_map.get(path)
    return paths


def resolve_type_name(index: ProjectIndex, name: str, res_path: str, scope: str | None) -> TypeRef | None:
    """Resolve a type as written in `res_path` at `scope` -- in a type
    annotation, an `as` cast, or a `T.new()` -- to the class it names, the
    way GDScript does: a res:// script path (from `preload`) as-is; then a
    const preload alias or inner class visible from `scope` (its own and
    enclosing classes, then consts inherited from the top-level `extends`
    chain); then a global `class_name`. `Outer.Inner` resolves `Outer`
    first. Engine/built-in types (and anything unknown) -> None."""
    if name.startswith("res://"):
        return (name, None) if name in index.known_paths else None

    if "." in name:
        outer_name, _, inner_name = name.partition(".")
        outer = resolve_type_name(index, outer_name, res_path, scope)
        if outer is None:
            return None
        inner_scope = f"{outer[1]}.{inner_name}" if outer[1] else inner_name
        return (outer[0], inner_scope) if (outer[0], inner_scope) in index.inner_classes else None

    for s in _enclosing_scopes(scope):
        if name in index.const_names.get((res_path, s), ()):
            return _const_alias(index, res_path, s, name)
        inner_scope = f"{s}.{name}" if s else name
        if (res_path, inner_scope) in index.inner_classes:
            return (res_path, inner_scope)
    for ancestor in _ancestor_paths(index, index.inheritance_map.get(res_path)):
        if name in index.const_names.get((ancestor, None), ()):
            return _const_alias(index, ancestor, None, name)

    path = index.class_name_table.get(name)
    return (path, None) if path is not None else None


def _const_alias(index: ProjectIndex, res_path: str, scope: str | None, name: str) -> TypeRef | None:
    """The script a `const <name> = preload("x.gd")` stands for; None for
    any other const (e.g. `const MAX = 5`), which isn't a type at all."""
    value_type = index.member_types[(res_path, scope)][name]
    if value_type is None or not value_type.startswith("res://"):
        return None
    return (value_type, None) if value_type in index.known_paths else None


def lookup_member(index: ProjectIndex, owner: TypeRef, name: str) -> tuple[bool, TypeRef | None]:
    """Find member var/const `name` on class `owner` -- for a top-level
    script, walking its `extends` chain (an inner class's own `extends`
    isn't tracked). Returns (found, the member's resolved static type),
    the type resolved in the *declaring* file's own scope."""
    path, scope = owner
    owners = [(p, None) for p in _ancestor_paths(index, path)] if scope is None else [owner]
    for owner_path, owner_scope in owners:
        members = index.member_types.get((owner_path, owner_scope), {})
        if name in members:
            type_name = members[name]
            if type_name is None:
                return True, None
            return True, resolve_type_name(index, type_name, owner_path, owner_scope)
    return False, None


def find_method(index: ProjectIndex, owner: TypeRef, name: str) -> TypeRef | None:
    """Where function `name` is declared, called on an instance of
    `owner`: (declaring path, declaring scope), or None."""
    path, scope = owner
    if scope is None:
        found = find_function_in_chain(path, name, index.function_index, index.inheritance_map)
        return (found, None) if found is not None else None
    return owner if name in index.function_index.get(owner, ()) else None


def find_signal(index: ProjectIndex, owner: TypeRef, name: str) -> TypeRef | None:
    """Like `find_method`, for a signal."""
    path, scope = owner
    if scope is None:
        found = find_signal_in_chain(path, name, index.signal_index, index.inheritance_map)
        return (found, None) if found is not None else None
    return owner if name in index.signal_index.get(owner, ()) else None


@dataclass
class _CallSite:
    """Where a receiver expression appears -- what its names can refer to."""
    res_path: str
    scope: str | None
    local_types: dict[str, str | None]
    lambda_shadowed: set[str] | frozenset[str]
    in_lambda: bool


def _resolve_base_name(index: ProjectIndex, site: _CallSite, name: str) -> tuple[TypeRef | None, bool]:
    """Type of a receiver chain's first name, in GDScript's own lookup
    order: `self`; a local var/param; a member var/const of the enclosing
    class (inherited ones included); an autoload; a type name (const
    preload alias, inner class, `class_name`). A local or member shadows
    any same-named global -- if its type can't be resolved, the receiver
    is unknown rather than falling through to the global.

    Inside a lambda, a name the lambda re-declares can't be typed from the
    enclosing function's locals/members (the lambda's own declaration may
    differ, and isn't tracked), so it skips straight to the globals.

    Returns (type, recognized): recognized is False when the name is
    unknown or its type isn't a project class (e.g. an engine type)."""
    if name == "self":
        return (site.res_path, site.scope), True
    if not (site.in_lambda and name in site.lambda_shadowed):
        if name in site.local_types:
            type_name = site.local_types[name]
            resolved = resolve_type_name(index, type_name, site.res_path, site.scope) if type_name else None
            return resolved, resolved is not None
        found, member_type = lookup_member(index, (site.res_path, site.scope), name)
        if found:
            return member_type, member_type is not None
    if name in index.autoloads:
        return (index.autoloads[name], None), True
    resolved = resolve_type_name(index, name, site.res_path, site.scope)
    return resolved, resolved is not None


def resolve_receiver(index: ProjectIndex, site: _CallSite, receiver: Receiver) -> tuple[TypeRef | None, bool]:
    """Static type of a receiver expression: its cast or first name (see
    `_resolve_base_name`), then each following field through the previous
    one's declared type. Returns (type, recognized) as `_resolve_base_name`
    does -- an untyped or undeclared field anywhere in the chain makes the
    whole receiver unknown."""
    if receiver.cast is not None:
        current = resolve_type_name(index, receiver.cast, site.res_path, site.scope)
        fields = receiver.chain
    else:
        current, recognized = _resolve_base_name(index, site, receiver.chain[0])
        if current is None:
            return None, recognized
        fields = receiver.chain[1:]
    for field_name in fields:
        if current is None:
            break
        _, current = lookup_member(index, current, field_name)
    return current, current is not None


def resolve_calls(
    file_symbols: FileSymbols,
    raw_calls: list[RawCall],
    index: ProjectIndex,
    local_var_types: dict[tuple[str | None, str], dict[str, str | None]],
    lambda_shadowed_names: dict[tuple[str | None, str], set[str]] | None = None,
) -> tuple[list[ResolvedCall], list[UnresolvedCall]]:
    """Resolve raw call sites in one file to (target file, target function).

    Resolution covers, in GDScript's actual scoping order: same-scope calls
    (bare or ``self.``, falling back up the top-level ``extends`` chain when
    the caller itself is top-level), ``super.`` calls (parent chain only,
    top-level callers only), and calls on a typed receiver (see
    `resolve_receiver`: a local var/param, member var/const, autoload, or
    type name -- in that order, so a local or member always shadows a
    same-named global -- possibly followed by a chain of typed fields, or
    an ``(<expr> as T)`` cast) -- walking the target's inheritance chain.
    Calls through an untyped variable/field, an engine-typed receiver, or a
    built-in Godot API are left unresolved. A call made inside a lambda,
    through a name that some lambda in the same function re-declares,
    doesn't trust the enclosing function's (possibly stale) type for that
    name -- it falls through to autoload/class_name resolution instead,
    same as if it weren't a known local/member at all.
    """
    lambda_shadowed_names = lambda_shadowed_names or {}
    resolved: list[ResolvedCall] = []
    unresolved: list[UnresolvedCall] = []
    source_path = file_symbols.res_path

    for call in raw_calls:
        site = _CallSite(
            res_path=source_path,
            scope=call.caller_scope,
            local_types=local_var_types.get((call.caller_scope, call.caller_function), {}),
            lambda_shadowed=lambda_shadowed_names.get((call.caller_scope, call.caller_function), frozenset()),
            in_lambda=call.in_lambda,
        )
        target_path: str | None = None
        target_scope: str | None = None
        receiver_recognized = True

        if call.receiver is None or call.receiver == "self":
            if call.called_name in index.function_index.get((source_path, call.caller_scope), ()):
                target_path, target_scope = source_path, call.caller_scope
            elif call.caller_scope is None:
                target_path = find_function_in_chain(
                    source_path, call.called_name, index.function_index, index.inheritance_map
                )
            # else: inner-class scope with no exact match -- no inheritance
            # fallback (inner classes' own extends isn't tracked).
        elif call.receiver == "super":
            if call.caller_scope is None:
                parent = index.inheritance_map.get(source_path)
                if parent is not None:
                    target_path = find_function_in_chain(
                        parent, call.called_name, index.function_index, index.inheritance_map
                    )
            else:
                receiver_recognized = False
        elif call.receiver_expr is not None:
            receiver_type, receiver_recognized = resolve_receiver(index, site, call.receiver_expr)
            if receiver_type is not None:
                target = find_method(index, receiver_type, call.called_name)
                if target is not None:
                    target_path, target_scope = target
        else:
            receiver_recognized = False

        if target_path is not None:
            resolved.append(ResolvedCall(
                source_res_path=source_path,
                source_scope=call.caller_scope,
                source_function=call.caller_function,
                target_res_path=target_path,
                target_scope=target_scope,
                target_function=call.called_name,
                line=call.line,
            ))
            continue

        reason = "method_not_found_in_target" if receiver_recognized else "unknown_receiver"
        unresolved.append(UnresolvedCall(
            source_res_path=source_path,
            source_scope=call.caller_scope,
            source_function=call.caller_function,
            receiver=call.receiver,
            called_name=call.called_name,
            line=call.line,
            reason=reason,
        ))

    return resolved, unresolved


def resolve_signal_connections(
    file_symbols: FileSymbols,
    raw_connections: list[RawConnection],
    index: ProjectIndex,
    local_var_types: dict[tuple[str | None, str], dict[str, str | None]] | None = None,
    lambda_shadowed_names: dict[tuple[str | None, str], set[str]] | None = None,
) -> tuple[list[ResolvedConnection], list[UnresolvedConnection]]:
    """Resolve ``<signal>.connect(<handler>)`` sites to the function that
    handles the signal. The handler reference is resolved exact-scope-first
    for a bare/``self.`` handler, otherwise through `resolve_receiver`
    exactly like a call receiver (a local or member shadows a same-named
    global, and a ``.connect()`` made inside a lambda doesn't trust the
    enclosing function's (possibly stale) type for a name some lambda
    re-declares).

    A connection counts as resolved whenever its handler does. The signal
    side is linked to its declaration when it's a project-declared signal:
    either in the same scope as the ``.connect()`` call (a bare/self/
    inherited reference -- `conn.signal_res_path` is already known at
    extraction time for these), or accessed through a receiver
    (`conn.signal_receiver` set, e.g. `GameManager.card_drawn.connect(...)`,
    `self.hud.closed.connect(...)`) whose type resolves the same way a call
    receiver's does, then searched for the signal (up its top-level
    inheritance chain). Anything else -- an engine built-in signal, or a
    receiver of unknown type -- leaves the signal unlinked, not the whole
    connection: the handler is registered all the same, and must not look
    like dead code."""
    local_var_types = local_var_types or {}
    lambda_shadowed_names = lambda_shadowed_names or {}
    resolved: list[ResolvedConnection] = []
    unresolved: list[UnresolvedConnection] = []
    source_path = file_symbols.res_path

    for conn in raw_connections:
        if conn.handler_name is None:
            unresolved.append(UnresolvedConnection(
                source_res_path=source_path,
                source_function=conn.caller_function,
                signal_receiver=conn.signal_receiver,
                signal_name=conn.signal_name,
                handler_receiver=conn.handler_receiver,
                handler_name=None,
                line=conn.line,
                reason="unsupported_handler_shape",
            ))
            continue

        site = _CallSite(
            res_path=source_path,
            scope=conn.caller_scope,
            local_types=local_var_types.get((conn.caller_scope, conn.caller_function), {}),
            lambda_shadowed=lambda_shadowed_names.get((conn.caller_scope, conn.caller_function), frozenset()),
            in_lambda=conn.in_lambda,
        )

        signal_res_path = conn.signal_res_path
        signal_scope: str | None = conn.caller_scope if signal_res_path is not None else None
        if conn.signal_receiver is not None:
            signal_ref = None
            if conn.signal_receiver_expr is not None:
                signal_owner, _ = resolve_receiver(index, site, conn.signal_receiver_expr)
                if signal_owner is not None:
                    signal_ref = find_signal(index, signal_owner, conn.signal_name)
            signal_res_path, signal_scope = signal_ref if signal_ref is not None else (None, None)

        target_path: str | None = None
        target_scope: str | None = None
        receiver_recognized = True

        if conn.handler_receiver is None or conn.handler_receiver == "self":
            if conn.handler_name in index.function_index.get((source_path, conn.caller_scope), ()):
                target_path, target_scope = source_path, conn.caller_scope
            elif conn.caller_scope is None:
                target_path = find_function_in_chain(
                    source_path, conn.handler_name, index.function_index, index.inheritance_map
                )
        elif conn.handler_receiver_expr is not None:
            handler_owner, receiver_recognized = resolve_receiver(index, site, conn.handler_receiver_expr)
            if handler_owner is not None:
                target = find_method(index, handler_owner, conn.handler_name)
                if target is not None:
                    target_path, target_scope = target
        else:
            receiver_recognized = False

        if target_path is not None:
            resolved.append(ResolvedConnection(
                source_res_path=source_path,
                source_scope=conn.caller_scope,
                source_function=conn.caller_function,
                signal_res_path=signal_res_path,
                signal_scope=signal_scope,
                signal_name=conn.signal_name,
                handler_res_path=target_path,
                handler_scope=target_scope,
                handler_function=conn.handler_name,
                line=conn.line,
            ))
            continue

        reason = "method_not_found_in_target" if receiver_recognized else "unknown_receiver"
        unresolved.append(UnresolvedConnection(
            source_res_path=source_path,
            source_function=conn.caller_function,
            signal_receiver=conn.signal_receiver,
            signal_name=conn.signal_name,
            handler_receiver=conn.handler_receiver,
            handler_name=conn.handler_name,
            line=conn.line,
            reason=reason,
        ))

    return resolved, unresolved


def resolve_scene_connections(
    scene: Scene, scenes: SceneIndex, index: ProjectIndex
) -> tuple[list[ResolvedSceneConnection], list[UnresolvedSceneConnection]]:
    """Resolve a scene's editor-made `[connection signal=... from=... to=...
    method=...]` entries to the handler function on the `to` node's script
    (up its inheritance chain), and -- when it's one -- the project-declared
    signal on the `from` node's script. Godot calls these handlers itself,
    so without them a handler connected only in the editor would look like
    it has no callers at all."""
    resolved: list[ResolvedSceneConnection] = []
    unresolved: list[UnresolvedSceneConnection] = []
    for conn in scene.connections:
        target_script = scenes.script_for_node(scene.res_path, conn.to_node)
        handler = None
        reason = "unknown_target_script"
        if target_script is not None and target_script in index.known_paths:
            handler = find_method(index, (target_script, None), conn.method)
            reason = "method_not_found_in_target"
        if handler is None:
            unresolved.append(UnresolvedSceneConnection(
                scene_res_path=scene.res_path,
                line=conn.line,
                signal_name=conn.signal,
                from_node=conn.from_node,
                to_node=conn.to_node,
                method=conn.method,
                reason=reason,
            ))
            continue

        source_script = scenes.script_for_node(scene.res_path, conn.from_node)
        signal_ref = None
        if source_script is not None and source_script in index.known_paths:
            signal_ref = find_signal(index, (source_script, None), conn.signal)
        resolved.append(ResolvedSceneConnection(
            scene_res_path=scene.res_path,
            line=conn.line,
            signal_name=conn.signal,
            from_node=conn.from_node,
            to_node=conn.to_node,
            signal_res_path=signal_ref[0] if signal_ref is not None else None,
            handler_res_path=handler[0],
            handler_function=conn.method,
        ))
    return resolved, unresolved
