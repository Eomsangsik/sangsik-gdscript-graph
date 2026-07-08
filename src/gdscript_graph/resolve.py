from __future__ import annotations

from dataclasses import dataclass

from gdscript_graph.calls import RawCall, RawConnection
from gdscript_graph.symbols import FileSymbols


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
    signal_res_path: str
    signal_scope: str | None  # the signal's own scope -- equal to source_scope for a bare/
                               # self/inherited reference, but always top-level (None) for a
                               # `<receiver>.signal_name.connect(...)` chain, independent of
                               # the connect() call's own scope
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


def resolve_calls(
    file_symbols: FileSymbols,
    raw_calls: list[RawCall],
    class_name_table: dict[str, str],
    autoloads: dict[str, str],
    function_index: dict[tuple[str, str | None], set[str]],
    inheritance_map: dict[str, str | None],
    local_var_types: dict[tuple[str | None, str], dict[str, str | None]],
    lambda_shadowed_names: dict[tuple[str | None, str], set[str]] | None = None,
) -> tuple[list[ResolvedCall], list[UnresolvedCall]]:
    """Resolve raw call sites in one file to (target file, target function).

    Resolution covers, in GDScript's actual scoping order: same-scope calls
    (bare or ``self.``, falling back up the top-level ``extends`` chain when
    the caller itself is top-level), ``super.`` calls (parent chain only,
    top-level callers only), calls through a local var/param with an
    explicit type annotation matching a declared ``class_name`` (checked
    *before* autoload/class_name below -- a local always shadows a
    same-named global), autoload singleton calls, and calls on a declared
    ``class_name`` -- the latter three all walk the target's inheritance
    chain. Calls through an untyped local variable/parameter, a chained
    receiver, or a built-in Godot API are left unresolved. A call made
    inside a lambda, through a variable name that some lambda in the same
    function re-declares, doesn't trust the enclosing function's (possibly
    stale) type for that name -- it falls through to autoload/class_name
    resolution instead, same as if it weren't a known local at all.
    """
    lambda_shadowed_names = lambda_shadowed_names or {}
    resolved: list[ResolvedCall] = []
    unresolved: list[UnresolvedCall] = []
    source_path = file_symbols.res_path

    for call in raw_calls:
        scope_locals = local_var_types.get((call.caller_scope, call.caller_function), {})
        shadowed = lambda_shadowed_names.get((call.caller_scope, call.caller_function), frozenset())
        target_path: str | None = None
        target_scope: str | None = None
        receiver_recognized = True

        if call.receiver is None or call.receiver == "self":
            if call.called_name in function_index.get((source_path, call.caller_scope), ()):
                target_path, target_scope = source_path, call.caller_scope
            elif call.caller_scope is None:
                target_path = find_function_in_chain(
                    source_path, call.called_name, function_index, inheritance_map
                )
            # else: inner-class scope with no exact match -- no inheritance
            # fallback (inner classes' own extends isn't tracked in v1).
        elif call.receiver == "super":
            if call.caller_scope is None:
                parent = inheritance_map.get(source_path)
                if parent is not None:
                    target_path = find_function_in_chain(
                        parent, call.called_name, function_index, inheritance_map
                    )
            else:
                receiver_recognized = False
        elif (
            call.receiver is not None
            and call.receiver in scope_locals
            and not (call.in_lambda and call.receiver in shadowed)
        ):
            # A local var/param always shadows a same-named autoload or
            # class_name -- GDScript resolves locals before globals, so a
            # receiver we know is a local must be resolved (or fail) via
            # its declared type rather than falling through to a
            # differently-scoped global that happens to share the name.
            if scope_locals[call.receiver] in class_name_table:
                target_path = find_function_in_chain(
                    class_name_table[scope_locals[call.receiver]],
                    call.called_name,
                    function_index,
                    inheritance_map,
                )
            else:
                receiver_recognized = False
        elif call.receiver in autoloads:
            target_path = find_function_in_chain(
                autoloads[call.receiver], call.called_name, function_index, inheritance_map
            )
        elif call.receiver in class_name_table:
            target_path = find_function_in_chain(
                class_name_table[call.receiver], call.called_name, function_index, inheritance_map
            )
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
    class_name_table: dict[str, str],
    autoloads: dict[str, str],
    function_index: dict[tuple[str, str | None], set[str]],
    inheritance_map: dict[str, str | None],
    local_var_types: dict[tuple[str | None, str], dict[str, str | None]] | None = None,
    lambda_shadowed_names: dict[tuple[str | None, str], set[str]] | None = None,
    signal_index: dict[tuple[str, str | None], set[str]] | None = None,
) -> tuple[list[ResolvedConnection], list[UnresolvedConnection]]:
    """Resolve ``<signal>.connect(<handler>)`` sites to the function that
    handles the signal. The handler reference is resolved with the same
    exact-scope-first, then local-var-typed, then self/autoload/class_name +
    inheritance-chain rules as a call receiver (see resolve_calls -- a local
    var/param always shadows a same-named autoload/class_name, and a
    ``.connect()`` made inside a lambda doesn't trust the enclosing
    function's (possibly stale) type for a name some lambda re-declares).

    The signal itself is either declared in the same scope as the
    ``.connect()`` call (a bare/self/inherited reference -- `conn.
    signal_res_path` is already known at extraction time for these), or
    accessed through a chained receiver (`conn.signal_receiver` set, e.g.
    `GameManager.card_drawn.connect(...)`), resolved here the same way a
    call receiver is (local-var-typed, autoload, class_name), then walking
    that target's *top-level* inheritance chain for a signal of the given
    name via `signal_index`/`find_signal_in_chain` -- inner-class-scoped
    signals aren't reachable this way, mirroring every other cross-file
    inheritance-chain lookup in this module. Any other chained receiver
    (e.g. `self.child.signal_name.connect(...)`) isn't tracked as a
    connection at all (see calls.py)."""
    local_var_types = local_var_types or {}
    lambda_shadowed_names = lambda_shadowed_names or {}
    signal_index = signal_index or {}
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

        scope_locals = local_var_types.get((conn.caller_scope, conn.caller_function), {})
        shadowed = lambda_shadowed_names.get((conn.caller_scope, conn.caller_function), frozenset())

        signal_res_path = conn.signal_res_path
        signal_scope: str | None = conn.caller_scope
        if conn.signal_receiver is not None:
            signal_scope = None  # chain resolution only ever finds a top-level signal
            signal_target_start: str | None = None
            signal_receiver_recognized = True
            if (
                conn.signal_receiver in scope_locals
                and not (conn.in_lambda and conn.signal_receiver in shadowed)
            ):
                if scope_locals[conn.signal_receiver] in class_name_table:
                    signal_target_start = class_name_table[scope_locals[conn.signal_receiver]]
                else:
                    signal_receiver_recognized = False
            elif conn.signal_receiver in autoloads:
                signal_target_start = autoloads[conn.signal_receiver]
            elif conn.signal_receiver in class_name_table:
                signal_target_start = class_name_table[conn.signal_receiver]
            else:
                signal_receiver_recognized = False

            signal_res_path = None
            if signal_target_start is not None:
                signal_res_path = find_signal_in_chain(
                    signal_target_start, conn.signal_name, signal_index, inheritance_map
                )

            if signal_res_path is None:
                reason = "method_not_found_in_target" if signal_receiver_recognized else "unknown_receiver"
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
                continue

        target_path: str | None = None
        target_scope: str | None = None
        receiver_recognized = True

        if conn.handler_receiver is None or conn.handler_receiver == "self":
            if conn.handler_name in function_index.get((source_path, conn.caller_scope), ()):
                target_path, target_scope = source_path, conn.caller_scope
            elif conn.caller_scope is None:
                target_path = find_function_in_chain(
                    source_path, conn.handler_name, function_index, inheritance_map
                )
        elif (
            conn.handler_receiver in scope_locals
            and not (conn.in_lambda and conn.handler_receiver in shadowed)
        ):
            # A local var/param always shadows a same-named autoload or
            # class_name -- see resolve_calls for the same rule.
            if scope_locals[conn.handler_receiver] in class_name_table:
                target_path = find_function_in_chain(
                    class_name_table[scope_locals[conn.handler_receiver]],
                    conn.handler_name,
                    function_index,
                    inheritance_map,
                )
            else:
                receiver_recognized = False
        elif conn.handler_receiver in autoloads:
            target_path = find_function_in_chain(
                autoloads[conn.handler_receiver], conn.handler_name, function_index, inheritance_map
            )
        elif conn.handler_receiver in class_name_table:
            target_path = find_function_in_chain(
                class_name_table[conn.handler_receiver], conn.handler_name, function_index, inheritance_map
            )
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
